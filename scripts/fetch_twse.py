#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V17
==============================
V17 修正 V16 的權證查詢 bug：精選清單常顯示「0 支認購權證」。

  根因：
    第一階段 fetch_active_warrant_targets() 已經抓過近7天「全市場」
    權證明細（TaiwanStockWarrant 不帶 data_id），但只拿來算活躍度排序，
    完整明細直接丟棄。
    最後選股完成後，fetch_warrant_detail() 又對「單一個股」重新查一次，
    且窗口只有3天 —— 中小型股、上櫃股的權證成交本來就不熱絡，
    3天內常常剛好0筆，導致明明在 warrant_targets 名單裡的股票，
    最後權證推薦卻顯示0支。

  V17 解法：
    ① fetch_active_warrant_targets() 抓取窗口從 7 天延長到 15 天，
       並把完整權證明細（代號/槓桿/Delta/到期日/買賣價）快取到
       WARRANT_DETAIL_CACHE，依標的分組。
    ② fetch_warrant_detail() 優先查這份快取，不用重打 API；
       只有快取沒命中的標的才 fallback 個股查詢（窗口也延長到15天）。
    → 大幅降低「0支」的誤判機率，同時減少 API 呼叫次數。

  沿用 V13 的兩階段掃描策略（避免母體只掃 200 支的問題）：
    ① 第一階段（快速粗篩）：只取「近 2 天」資料，快速淘汰收跌/無量股
       → 粗篩上限：有 TOKEN → 420 支；無 TOKEN → 220 支
    ② 第二階段（精細評分）：存活候選取「近 35 天」歷史資料
       → 計算 MA / 法人 / 融資完整評分，選出 Top10

  API 預算（有 TOKEN 600 req/hr，V17 因權證快取化而降低）：
    ① TaiwanStockInfoWithWarrant      1 req
    ② TaiwanStockWarrant（近15天，快取）1 req
    ③ 粗篩 420 支                     420 req
    ④ 精篩存活 ~120 支                ~120 req
    ⑤ 法人 + 融資 Top30 × 2           60 req
    ⑥ 權證細節（多數查快取，僅少數fallback）  ~0-10 req
    合計：~602-612 req → 跨小時執行，第二小時剩餘 300 req 絕對夠用
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone
from collections import Counter

TZ_TW       = timezone(timedelta(hours=8))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N       = 15
FM_URL      = "https://api.finmindtrade.com/api/v4/data"
TOKEN       = os.environ.get("FINMIND_TOKEN", "")

# V13 掃描上限（依 TOKEN 決定）
SCAN_LIMIT_TOKEN    = 420   # 有 TOKEN：420 支粗篩，總 req ≈ 612，安全跨兩個小時
SCAN_LIMIT_NO_TOKEN = 220   # 無 TOKEN：220 支，總 req ≈ 343

def tw_now():
    return datetime.now(TZ_TW)

def date_back(days):
    return (tw_now() - timedelta(days=days)).strftime("%Y-%m-%d")

def sf(v, d=0.0):
    try:    return float(str(v).replace(",","").strip())
    except: return d

def si(v, d=0):
    try:    return int(str(v).replace(",","").strip())
    except: return d

def hdrs():
    h = {}
    if TOKEN: h["Authorization"] = f"Bearer {TOKEN}"
    return h

# ── 共用請求 ─────────────────────────────────────────────────
def fm(dataset, data_id=None, start_date=None, end_date=None, retries=3):
    params = {"dataset": dataset}
    if data_id:    params["data_id"]    = data_id
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"]   = end_date
    for i in range(retries):
        try:
            r = requests.get(FM_URL, params=params, headers=hdrs(), timeout=25)
            if r.status_code == 402:
                print("  ! FinMind 402：API 次數超限！停止後續請求")
                return [], True
            if r.status_code in (400, 404, 422):
                if r.status_code == 422:
                    print(f"  ! fm {dataset}/{data_id} → 422（無此資料，跳過）")
                return [], False
            r.raise_for_status()
            d = r.json()
            return (d.get("data", []) if d.get("status") == 200 else []), False
        except Exception as e:
            if i < retries - 1: time.sleep(1.5 * (i + 1))
            else: print(f"  ! fm {dataset}/{data_id} → {e}")
    return [], False

def fm1(dataset, data_id=None, start_date=None, end_date=None):
    return fm(dataset, data_id, start_date, end_date)

# ── Step①：有認購權證的全部標的（1 req）────────────────────────
def fetch_warrant_targets():
    data, _ = fm1("TaiwanStockInfoWithWarrant")
    result = {}
    for row in data:
        sid = str(row.get("stock_id","")).strip()
        t   = str(row.get("type","")).strip()
        if not (sid.isdigit() and len(sid) == 4): continue
        if t not in ("twse", "tpex"):             continue
        result[sid] = {
            "name":   str(row.get("stock_name", sid)).strip(),
            "market": "tse" if t == "twse" else "otc",
        }
    return result

EXCLUDE_SIDS = {"9999", "0000"}

# ── Step②：近期活躍認購權證標的（1 req）─────────────────────────
# V17修正：抓取窗口從 7 天延長到 15 天，並把完整權證明細快取下來
# （V16 bug：最後 fetch_warrant_detail() 對單一個股只查 3 天，
#  中小型股、上櫃股權證成交不熱絡，3天內常常0筆，導致「精選清單」
#  裡明明在 warrant_targets 名單內的股票，最後權證推薦卻顯示0支。
#  其實這支函式已經抓過全市場權證明細了，V17 把它快取起來，
#  最後直接查表，不用再對每支個股重打一次 API，
#  同時用更長的窗口（15天）大幅降低「剛好沒交易」的機率。）
WARRANT_DETAIL_CACHE = {}   # underlying_sid -> [warrant_dict, ...]（V17 新增）

def fetch_active_warrant_targets():
    start = date_back(15)   # V17: 7天 → 15天，降低中小型股查無資料的機率
    today = tw_now().strftime("%Y-%m-%d")
    data, _ = fm1("TaiwanStockWarrant", start_date=start, end_date=today)
    if not data:
        print("  ! TaiwanStockWarrant 無資料（可能是假日或 422），使用全部標的")
        return [], set()
    vol_map  = {}
    active_set = set()
    for row in data:
        call_put = str(row.get("PutCall",""))
        if "C" not in call_put and "認購" not in call_put: continue
        sid = str(row.get("underlying_stock","")).strip()
        if not (sid.isdigit() and len(sid) == 4): continue
        vol = si(row.get("TradingVolume", 0))
        vol_map[sid] = vol_map.get(sid, 0) + vol
        active_set.add(sid)

        # V17：同時把這筆權證的完整明細存進快取，最後選股時直接查表
        try:
            w_code     = str(row.get("stock_id","")).strip()
            expire_str = str(row.get("ExpirationDate","")).strip()
            leverage   = sf(row.get("EffectiveLeverage",0))
            delta      = sf(row.get("Delta",0))
            bid        = sf(row.get("BidPrice",0))
            ask        = sf(row.get("AskPrice",0))
            row_date   = str(row.get("date","")).strip()
            if not w_code or not expire_str: continue
            expire_dt = datetime.strptime(expire_str[:10], "%Y-%m-%d")
            WARRANT_DETAIL_CACHE.setdefault(sid, {})
            prev = WARRANT_DETAIL_CACHE[sid].get(w_code)
            # 同一檔權證可能在窗口內出現多天資料，保留「日期最新」的一筆
            if prev is None or row_date > prev.get("_row_date", ""):
                WARRANT_DETAIL_CACHE[sid][w_code] = {
                    "code": w_code, "expire_dt": expire_dt,
                    "leverage": leverage, "delta": delta,
                    "bid": bid, "ask": ask, "volume": vol,
                    "issuer": str(row.get("Issuer",""))[:3],
                    "_row_date": row_date,
                }
        except: pass

    sorted_sids = sorted(vol_map.keys(), key=lambda s: -vol_map[s])
    print(f"  → TaiwanStockWarrant 取得 {len(sorted_sids)} 支活躍標的（近 15 天有認購交易，快取 {sum(len(v) for v in WARRANT_DETAIL_CACHE.values())} 檔權證明細）")
    return sorted_sids, active_set

# ── V13：建立全量掃描清單────────────────────────────────────────
def build_scan_list(warrant_targets, active_sids_sorted, active_set):
    """
    排序：
      1. 近期活躍（認購交易量大的優先） → 優先掃描
      2. 有認購權證但近期不活躍         → 次要掃描
    上限：有 TOKEN → 420 支；無 TOKEN → 220 支
    """
    limit = SCAN_LIMIT_TOKEN if TOKEN else SCAN_LIMIT_NO_TOKEN

    priority = [s for s in active_sids_sorted if s in warrant_targets and s not in EXCLUDE_SIDS]
    rest     = [s for s in warrant_targets if s not in active_set and s not in EXCLUDE_SIDS]

    combined = priority + rest
    print(f"  → 全量標的：{len(warrant_targets)} 支")
    print(f"     活躍優先（近 7 天有認購交易）：{len(priority)} 支")
    print(f"     次要（有權證但近期不活躍）：{len(rest)} 支")
    print(f"     V13 掃描上限：{limit} 支（{'有 TOKEN 600req/hr' if TOKEN else '匿名 300req/hr'}）")
    return combined[:limit]

# ── V13 第一階段：快速粗篩（只取近 2 天，速度快）──────────────────
def quick_filter(scan_sids, warrant_targets, quick_start, end_date):
    """
    每 req 只取一支股票的近 2 天資料。
    淘汰條件（保守，寧可放行也不誤殺）：
      - 最新資料距 end_date > 5 天（停牌/下市）
      - 今日收跌（spread < 0）
      - 成交量嚴重不足
      - 股價 <50 或 >3000（V16：低價股無認購權證，下限提高至50元）
    通常淘汰 60~70%，回傳 list[dict]
    """
    survivors = []
    stop = False
    req  = 0
    for idx, sid in enumerate(scan_sids):
        if stop: break
        if (idx + 1) % 100 == 0:
            print(f"  ... 粗篩 {idx+1}/{len(scan_sids)}  存活:{len(survivors)}  req:{req}")

        data, hit = fm1("TaiwanStockPrice", sid, quick_start, end_date)
        req += 1
        if hit:
            stop = True; break
        time.sleep(0.05)

        if not data: continue

        rows = []
        for row in data:
            try:
                c = sf(row.get("close", 0))
                if c <= 0: continue
                rows.append({
                    "date":   str(row["date"]),
                    "open":   sf(row.get("open",  c)),
                    "high":   sf(row.get("max",   c)),
                    "low":    sf(row.get("min",   c)),
                    "close":  c,
                    "spread": sf(row.get("spread", 0)),
                    "volume": si(row.get("Trading_Volume", 0)),
                })
            except: continue
        if not rows: continue
        rows.sort(key=lambda x: x["date"])
        today_p = rows[-1]

        # 資料新鮮度檢查
        last_dt = datetime.strptime(today_p["date"], "%Y-%m-%d").date()
        ref_dt  = datetime.strptime(end_date, "%Y-%m-%d").date()
        if (ref_dt - last_dt).days > 5: continue

        info   = warrant_targets.get(sid, {})
        market = info.get("market", "tse")
        close  = today_p["close"]
        volume = today_p["volume"]
        spread = today_p["spread"]

        # V16：股價需 ≥50 元，低價股幾乎沒有認購權證
        if close < 50 or close > 3000:                         continue
        # 成交量門檻：股價高的股票成交量自然較低，但需有一定流動性
        if volume < (50000 if market == "otc" else 100000):    continue
        if spread < 0:                                          continue  # 收跌淘汰

        survivors.append({
            "sid":       sid,
            "info":      info,
            "today_p":   today_p,
            "data_date": today_p["date"],
        })

    print(f"  → 粗篩完成：{len(survivors)} 支存活（掃 {len(scan_sids)} 支，用 {req} req）")
    return survivors, req, stop

# ── 第二階段：對粗篩存活者取完整歷史────────────────────────────────
def fetch_price_full(sid, start_date, end_date):
    data, hit = fm1("TaiwanStockPrice", sid, start_date, end_date)
    if hit: return None, True
    result = []
    for row in data:
        try:
            c = sf(row.get("close", 0))
            if c <= 0: continue
            result.append({
                "date":   str(row["date"]),
                "open":   sf(row.get("open",  c)),
                "high":   sf(row.get("max",   c)),
                "low":    sf(row.get("min",   c)),
                "close":  c,
                "spread": sf(row.get("spread", 0)),
                "volume": si(row.get("Trading_Volume", 0)),
            })
        except: continue
    return sorted(result, key=lambda x: x["date"]), False

# ── 三大法人 & 融資 ────────────────────────────────────────────
def fetch_inst(sid, start_date, end_date):
    data, hit = fm1("TaiwanStockInstitutionalInvestors", sid, start_date, end_date)
    if hit: return {}, True
    by_date = {}
    for row in data:
        date = str(row.get("date",""))
        name = str(row.get("name",""))
        net  = si(row.get("buy",0)) - si(row.get("sell",0))
        if date not in by_date:
            by_date[date] = {"foreign_net":0,"trust_net":0,"dealer_net":0}
        if   "外資" in name: by_date[date]["foreign_net"] += net
        elif "投信" in name: by_date[date]["trust_net"]   += net
        elif "自營" in name: by_date[date]["dealer_net"]  += net
    if not by_date: return {}, False
    latest = sorted(by_date.keys())[-1]
    v = by_date[latest]
    return {
        "foreign_net": v["foreign_net"]//1000,
        "trust_net":   v["trust_net"]//1000,
        "dealer_net":  v["dealer_net"]//1000,
        "total_net":   (v["foreign_net"]+v["trust_net"]+v["dealer_net"])//1000,
    }, False

def fetch_margin(sid, start_date, end_date):
    data, hit = fm1("TaiwanStockMarginPurchaseShortSale", sid, start_date, end_date)
    if hit: return {}, True
    if not data: return {}, False
    latest = sorted(data, key=lambda x: x.get("date",""))[-1]
    bal = si(latest.get("MarginPurchaseRemainAmount",1)) or 1
    return {
        "margin_buy": si(latest.get("MarginPurchaseBuy",0)),
        "margin_bal": bal,
    }, False

# ── Top10 權證細節 ──────────────────────────────────────────────
def fetch_warrant_detail(sid, data_date_str):
    """
    V17修正：優先從 WARRANT_DETAIL_CACHE 查表（fetch_active_warrant_targets
    已經抓過近15天全市場權證明細，直接用，不必重打 API）。
    只有快取沒有該標的資料時，才 fallback 用個股查詢（窗口也從3天延長到15天）。
    """
    dt = datetime.strptime(data_date_str, "%Y-%m-%d")
    warrants = []

    cached = WARRANT_DETAIL_CACHE.get(sid)
    if cached:
        for w_code, w in cached.items():
            try:
                days_left = (w["expire_dt"] - dt).days
                if days_left < 20:                          continue
                if w["leverage"] <= 0 or w["leverage"] > 15: continue
                delta = w["delta"]
                if delta >= 0.70:    moneyness = "深度價內"
                elif delta >= 0.55:  moneyness = "輕度價內"
                elif delta >= 0.45:  moneyness = "價平"
                elif delta >= 0.30:  moneyness = "輕度價外"
                else:                moneyness = "價外"
                warrants.append({
                    "code":        w["code"],
                    "issuer":      w["issuer"],
                    "type":        "call",
                    "expire":      w["expire_dt"].strftime("%Y/%m/%d"),
                    "days_left":   days_left,
                    "leverage":    round(w["leverage"], 1),
                    "delta":       round(delta, 2),
                    "moneyness":   moneyness,
                    "bid":         w["bid"], "ask": w["ask"], "volume": w["volume"],
                    "leverage_ok": 4 < w["leverage"] < 12,
                })
            except: continue

    if not warrants:
        # V17：快取沒命中時的 fallback，窗口從3天延長到15天
        start = (dt - timedelta(days=15)).strftime("%Y-%m-%d")
        data, _ = fm1("TaiwanStockWarrant", sid, start, data_date_str)
        for row in data:
            try:
                call_put = str(row.get("PutCall",""))
                if "C" not in call_put and "認購" not in call_put: continue
                w_code     = str(row.get("stock_id","")).strip()
                expire_str = str(row.get("ExpirationDate","")).strip()
                leverage   = sf(row.get("EffectiveLeverage",0))
                delta      = sf(row.get("Delta",0))
                bid        = sf(row.get("BidPrice",0))
                ask        = sf(row.get("AskPrice",0))
                vol        = si(row.get("TradingVolume",0))
                if not w_code: continue
                try: expire_dt = datetime.strptime(expire_str[:10], "%Y-%m-%d")
                except: continue
                days_left = (expire_dt - dt).days
                if days_left < 20:                  continue
                if leverage <= 0 or leverage > 15:  continue
                if delta >= 0.70:    moneyness = "深度價內"
                elif delta >= 0.55:  moneyness = "輕度價內"
                elif delta >= 0.45:  moneyness = "價平"
                elif delta >= 0.30:  moneyness = "輕度價外"
                else:                moneyness = "價外"
                warrants.append({
                    "code":        w_code,
                    "issuer":      str(row.get("Issuer",""))[:3],
                    "type":        "call",
                    "expire":      expire_dt.strftime("%Y/%m/%d"),
                    "days_left":   days_left,
                    "leverage":    round(leverage,1),
                    "delta":       round(delta,2),
                    "moneyness":   moneyness,
                    "bid":         bid, "ask": ask, "volume": vol,
                    "leverage_ok": 4 < leverage < 12,
                })
            except: continue

    warrants.sort(key=lambda x:(0 if x["leverage_ok"] else 1,-x["volume"],abs(x["delta"]-0.55)))
    return warrants[:3]

# ── 評分 ──────────────────────────────────────────────────────
def calc_ma(closes, n):
    if len(closes) < n: return None
    return round(statistics.mean(closes[-n:]), 2)

def calc_score(today_p, hist, inst, margin):
    score, reasons, warnings = 0, [], []
    close  = today_p["close"]
    high   = today_p["high"]
    low_p  = today_p["low"]
    spread = today_p.get("spread", 0)
    prev_c = round(close - spread, 2) if spread else (hist[-1]["close"] if hist else close)
    chg    = round(spread / prev_c * 100, 2) if prev_c > 0 else 0

    if chg >= 5:     score += 16; reasons.append("強勢大漲 ≥5%")
    elif chg >= 3:   score += 12; reasons.append("大漲 ≥3%")
    elif chg >= 1:   score += 7;  reasons.append("溫和上漲 ≥1%")
    elif chg >= 0.5: score += 4;  reasons.append("小漲 0.5~1%")
    elif chg >= 0:   score += 1
    elif chg < 0:    score -= 10; warnings.append("今日收跌")

    if high > low_p:
        cp = (close - low_p) / (high - low_p)
        if cp >= 0.8:   score += 14; reasons.append("收盤靠近最高點（買盤強）")
        elif cp >= 0.6: score += 8
        elif cp < 0.3:  score -= 8;  warnings.append("長上影線（賣壓重）")

    closes  = [h["close"]  for h in hist]
    volumes = [h["volume"] for h in hist]
    if len(volumes) >= 5:
        avg_vol = statistics.mean(volumes[-5:])
        vr = today_p["volume"] / avg_vol if avg_vol > 0 else 0
        if 1.5 <= vr <= 4:  score += 10; reasons.append(f"量能放大 {vr:.1f}x")
        elif vr > 4:        score += 5;  warnings.append("量能過度放大")
        elif vr < 0.7:      score -= 5;  warnings.append("量能萎縮")

    if len(closes) >= 20:
        ma5  = calc_ma(closes, 5)
        ma10 = calc_ma(closes, 10)
        ma20 = calc_ma(closes, 20)
        if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
            score += 10; reasons.append("均線多頭排列")
        elif ma5 and ma10 and ma5 > ma10: score += 5
        if ma5  and close > ma5:  score += 4
        if ma20 and close > ma20: score += 6; reasons.append(f"站上月線 MA20={ma20}")
        elif ma20: score -= 5; warnings.append("跌破月線")
        rh = max(closes[-10:]) if len(closes)>=10 else closes[-1]
        if close >= rh * 0.99: score += 10; reasons.append("突破近10日高點")

    if inst:
        tn = inst.get("total_net",0)
        tr = inst.get("trust_net",0)
        fn = inst.get("foreign_net",0)
        if tn > 5000:    score += 15; reasons.append(f"三大法人買超 {tn}張")
        elif tn > 1000:  score += 10; reasons.append(f"三大法人買超 {tn}張")
        elif tn > 0:     score += 5
        elif tn < -3000: score -= 10; warnings.append("三大法人賣超")
        if tr > 500:     score += 5;  reasons.append("投信積極買超")
        if fn > 3000:    score += 5;  reasons.append("外資積極買超")

    if margin:
        mb = margin.get("margin_buy",0)
        bl = margin.get("margin_bal",1)
        if bl > 0 and mb/bl > 0.15:
            score -= 5; warnings.append("融資追價明顯")

    return max(0, min(100, score)), reasons, warnings, chg

# ── 主程式 ─────────────────────────────────────────────────────
def main():
    now    = tw_now()
    today  = now.strftime("%Y-%m-%d")
    today8 = now.strftime("%Y%m%d")
    req    = 0

    # 盤中/盤後模式判斷
    hour_min = now.hour * 60 + now.minute
    if hour_min < 17 * 60 + 30:
        d = now - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        end_date = d.strftime("%Y-%m-%d")
        print(f"  ► 盤中模式：使用上個交易日資料（end_date={end_date}）")
    else:
        end_date = today
        print(f"  ► 盤後模式：使用今日收盤資料（end_date={end_date}）")

    print(f"[{now.strftime('%H:%M:%S')} 台灣時間] fetch_twse V17 開始 {today}")
    print(f"  FinMind token: {'已設定（600req/hr）' if TOKEN else '未設定（匿名300req/hr）'}")
    print(f"  V13 掃描策略：兩階段（粗篩 {SCAN_LIMIT_TOKEN if TOKEN else SCAN_LIMIT_NO_TOKEN} 支 → 精篩存活）")

    # ── ① 有認購權證的全部標的（1 req）──────────────────────────
    print("  ► ① 取有認購權證標的（全量）...")
    warrant_targets = fetch_warrant_targets(); req += 1
    if not warrant_targets: print("  ! ① 失敗，中止"); return
    print(f"  → {len(warrant_targets)} 支")
    time.sleep(0.3)

    # ── ② 近期活躍認購權證標的（1 req）──────────────────────────
    print("  ► ② 取近期活躍認購權證...")
    active_sids_sorted, active_set = fetch_active_warrant_targets(); req += 1
    time.sleep(0.3)

    # ── 建立掃描清單（V13：依 TOKEN 設上限）──────────────────────
    scan_sids = build_scan_list(warrant_targets, active_sids_sorted, active_set)

    # ── ③ V13 第一階段：快速粗篩（近 2 天資料）─────────────────
    quick_start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
    print(f"  ► ③ 第一階段粗篩（{quick_start}~{end_date}，共 {len(scan_sids)} 支）...")
    survivors, q_req, stop = quick_filter(scan_sids, warrant_targets, quick_start, end_date)
    req += q_req

    if not survivors:
        print("  ! 粗篩後無存活標的，可能資料尚未更新")
        _write_empty(now, today8, req); return

    # 判斷主流資料日期
    date_votes  = Counter(s["data_date"] for s in survivors)
    actual_date = date_votes.most_common(1)[0][0]
    print(f"  → 實際資料日期：{actual_date}")

    # ── ④ 第二階段：對存活候選取完整歷史 ─────────────────────────
    full_start = (datetime.strptime(actual_date, "%Y-%m-%d") - timedelta(days=35)).strftime("%Y-%m-%d")
    print(f"  ► ④ 第二階段精篩（{full_start}~{actual_date}，共 {len(survivors)} 支）...")

    candidates = []
    for idx, s in enumerate(survivors):
        if stop: break
        sid = s["sid"]
        if (idx + 1) % 50 == 0:
            print(f"  ... 精篩 {idx+1}/{len(survivors)}  候選:{len(candidates)}  req:{req}")

        hist_rows, hit = fetch_price_full(sid, full_start, actual_date)
        req += 1
        if hit:
            print(f"  ! 觸發 API 限額（req={req}），停止精篩")
            stop = True; break
        time.sleep(0.07)

        if not hist_rows or len(hist_rows) < 6: continue

        today_p = hist_rows[-1]
        last_dt = datetime.strptime(today_p["date"], "%Y-%m-%d").date()
        ref_dt  = datetime.strptime(actual_date, "%Y-%m-%d").date()
        if (ref_dt - last_dt).days > 5: continue

        hist = hist_rows[:-1]
        if len(hist) < 5: continue

        _, _, _, chg = calc_score(today_p, hist, {}, {})
        if chg < 0.1: continue

        candidates.append({
            "sid":       sid,
            "info":      s["info"],
            "today_p":   today_p,
            "hist":      hist,
            "chg":       chg,
            "data_date": today_p["date"],
        })

    print(f"  → ④ 完成：{len(candidates)} 支候選（req={req}）")

    if not candidates:
        print("  ! 無候選，可能資料尚未更新")
        _write_empty(now, today8, req); return

    # ── ⑤ Top30：查三大法人 + 融資 ─────────────────────────────
    candidates.sort(key=lambda x: x["chg"], reverse=True)
    top30      = candidates[:30]
    inst_start = (datetime.strptime(actual_date, "%Y-%m-%d") - timedelta(days=20)).strftime("%Y-%m-%d")

    print(f"  ► ⑤ 三大法人 + 融資（{len(top30)} 支，{inst_start}~{actual_date}）...")
    inst_map   = {}
    margin_map = {}
    for c in top30:
        if stop: break
        sid = c["sid"]
        res, hit = fetch_inst(sid, inst_start, actual_date); req += 1
        if hit: stop = True; break
        inst_map[sid] = res; time.sleep(0.12)

        res, hit = fetch_margin(sid, inst_start, actual_date); req += 1
        if hit: stop = True; break
        margin_map[sid] = res; time.sleep(0.10)

    print(f"  → ⑤ 完成（req={req}）")

    # ── 完整評分，取 Top10 ───────────────────────────────────────
    scored = []
    for c in top30:
        sid  = c["sid"]
        hist = c["hist"]
        if len(hist) < 5: continue
        score, reasons, warnings, chg = calc_score(
            c["today_p"], hist,
            inst_map.get(sid,{}), margin_map.get(sid,{})
        )
        score = min(100, score + 2)
        if score < 25: continue
        ma_c = [h["close"] for h in hist]
        scored.append({
            "sid":        sid,
            "name":       c["info"].get("name", sid),
            "close":      c["today_p"]["close"],
            "change_pct": chg,
            "volume":     c["today_p"]["volume"],
            "market":     c["info"].get("market","tse"),
            "score":      score,
            "reasons":    reasons,
            "warnings":   warnings,
            "inst":       inst_map.get(sid,{}),
            "ma5":        calc_ma(ma_c, 5),
            "ma10":       calc_ma(ma_c, 10),
            "ma20":       calc_ma(ma_c, 20),
            "warrants":   [],
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top15 = scored[:TOP_N]

    # ── ⑥ Top10 查權證細節 ────────────────────────────────────
    if top15 and not stop:
        print(f"  ► ⑥ 權證細節（{len(top15)} 支）...")
        for c in top15:
            c["warrants"] = fetch_warrant_detail(c["sid"], actual_date)
            req += 1; time.sleep(0.15)
        print(f"  → ⑥ 完成（req={req}）")

    # ── 機率標籤 ─────────────────────────────────────────────
    for c in top15:
        s = c["score"]
        if s >= 85:
            c["prob"] = f"高（{min(82,60+(s-85)*2+15)}%）"; c["prob_level"] = "high"
        elif s >= 70:
            c["prob"] = f"中高（{62+(s-70)}%）";             c["prob_level"] = "medium-high"
        elif s >= 55:
            c["prob"] = f"中（{48+(s-55)}%）";               c["prob_level"] = "medium"
        else:
            c["prob"] = "偏低（<48%）";                       c["prob_level"] = "low"

    output = {
        "updated_at":       now.strftime("%Y/%m/%d %H:%M"),
        "trade_date":       actual_date.replace("-",""),
        "data_date":        actual_date,
        "total_scanned":    len(scan_sids),
        "candidates_count": len(scored),
        "total_api_req":    req,
        "stocks":           top15,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成！資料:{actual_date}  掃:{len(scan_sids)}  存活:{len(survivors)}  候選:{len(scored)}  精選:{len(top15)}  API:{req}")
    for s in top15:
        wc = len(s.get("warrants",[]))
        print(f"  [{s['score']:3d}] {s['sid']} {s['name'][:8]:8s} {s['change_pct']:+.2f}%  {s['prob']}  權證:{wc}支")

def _write_empty(now, today8, req):
    output = {
        "updated_at":       now.strftime("%Y/%m/%d %H:%M"),
        "trade_date":       today8,
        "data_date":        now.strftime("%Y-%m-%d"),
        "total_scanned":    0,
        "candidates_count": 0,
        "total_api_req":    req,
        "stocks":           [],
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"  💾 stocks.json 輸出空結果（API 已用 {req} 次）")

if __name__ == "__main__":
    main()
