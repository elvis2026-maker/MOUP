#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V24
==============================
V24 修正 V22 的三個問題：
  1. TaiwanStockWarrant 全量查詢 422 → fallback 977 支 → 粗篩 402 超限
  2. 402 發生時直接放棄已有結果 → 改為繼續精篩已存活標的
  3. 粗篩無硬性上限 → 加 SCAN_HARD_LIMIT=450 截斷保護

  V21 的問題：
    電子股 ∩ 有認購權證 = 974 支，第一階段粗篩每支打 1 req，
    974 req 就超過 FinMind 免費帳號 600/hr 的上限，
    第二階段根本沒機會跑，每天都是空結果。

  V24 解法：三階段架構，大幅減少 API 請求數
    ① TaiwanStockInfoWithWarrant  → 電子股 meta（名稱/市場）  1 req
    ② TaiwanStockWarrant 近15天   → 真正有活躍認購交易的電子標的  1 req
       （這一步直接把候選池從 974 縮減到約 150~250 支，
         同時快取完整權證明細，最後直接查表不用再打API）
    ③ TaiwanStockPrice 近4天      → 量價粗篩（存活約 50~80 支）  ~150~250 req
    ④ TaiwanStockPrice 近35天     → 完整歷史精篩                 ~50~80 req
    ⑤ 三大法人 + 融資（Top20×2）                                  ~40 req
    合計：~242~372 req，完全在 600/hr 內，且有大量餘裕

  電子 8 大產業類別（同 V20/V21）：
    半導體業、電腦及週邊設備業、光電業、通信網路業、
    電子零組件業、電子通路業、資訊服務業、其他電子業
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone
from collections import Counter

TZ_TW       = timezone(timedelta(hours=8))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N       = 15
FM_URL      = "https://api.finmindtrade.com/api/v4/data"
TOKEN       = os.environ.get("FINMIND_TOKEN", "")

ELECTRONICS_CATEGORIES = {
    "半導體業", "電腦及週邊設備業", "光電業", "通信網路業",
    "電子零組件業", "電子通路業", "資訊服務業", "其他電子業",
}
EXCLUDE_SIDS = {"9999", "0000"}

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

# ── Step①：電子股 meta（名稱/市場別）1 req ──────────────────
def fetch_electronics_meta():
    """
    從 TaiwanStockInfoWithWarrant 取得電子股的 meta（名稱/市場別）。
    同時過濾掉非電子股，回傳 dict {sid: {name, market}}。
    """
    data, _ = fm1("TaiwanStockInfoWithWarrant")
    # 先建 TaiwanStockInfo 的產業類別對照（需再打一次）
    elec_meta = {}
    all_meta  = {}
    for row in data:
        sid = str(row.get("stock_id","")).strip()
        t   = str(row.get("type","")).strip()
        if not (sid.isdigit() and len(sid) == 4): continue
        if t not in ("twse", "tpex"): continue
        all_meta[sid] = {
            "name":   str(row.get("stock_name", sid)).strip(),
            "market": "tse" if t == "twse" else "otc",
        }
    return all_meta

def fetch_electronics_sids():
    data, _ = fm1("TaiwanStockInfo")
    elec_sids = set()
    cat_count = {}
    for row in data:
        sid = str(row.get("stock_id","")).strip()
        if not (sid.isdigit() and len(sid) == 4): continue
        cat = str(row.get("industry_category","")).strip()
        t   = str(row.get("type","")).strip()
        if t not in ("twse", "tpex"): continue
        cat_count[cat] = cat_count.get(cat, 0) + 1
        if cat in ELECTRONICS_CATEGORIES:
            elec_sids.add(sid)
    print(f"  → 電子股（上市+上櫃）：{len(elec_sids)} 支")
    for cat in ELECTRONICS_CATEGORIES:
        n = cat_count.get(cat, 0)
        if n > 0:
            print(f"     {cat}：{n} 支")
    return elec_sids

# ── Step②：活躍認購權證（1 req，同時快取明細）───────────────
WARRANT_DETAIL_CACHE = {}  # sid -> {w_code: {...}}

def fetch_active_warrant_targets(elec_sids, today_dt):
    """
    V24 修正 V22 的根本問題：
      FinMind TaiwanStockWarrant 不支援全量查詢（不帶 stock_id），
      會回傳 422 → fallback 到 977 支 → 粗篩超過 600 req → 402 超限。

    V24 解法：
      ① 改查 TaiwanStockWarrantDetail（支援全量，1 req）取得認購標的清單
      ② 若仍失敗，對熱門電子股批次查詢（30 支，30 req）取交集
      ③ 最終 fallback：限量 400 支（硬性保護）
    """
    end   = today_dt.strftime("%Y-%m-%d")
    start = date_back(15)

    def build_cache_and_set(data_rows, elec_sids):
        vol_map    = {}
        active_set = set()
        for row in data_rows:
            call_put = str(row.get("PutCall",""))
            if "C" not in call_put and "認購" not in call_put: continue
            sid = str(row.get("UnderlyingSymbol", row.get("underlying_stock",""))).strip()
            if not (sid.isdigit() and len(sid) == 4): continue
            if sid not in elec_sids: continue
            vol = si(row.get("TradingVolume", 0))
            vol_map[sid] = vol_map.get(sid, 0) + vol
            active_set.add(sid)
            try:
                w_code     = str(row.get("stock_id","")).strip()
                expire_str = str(row.get("ExpirationDate","")).strip()
                leverage   = sf(row.get("EffectiveLeverage", 0))
                delta      = sf(row.get("Delta", 0))
                row_date   = str(row.get("date","")).strip()
                if not w_code or not expire_str: continue
                expire_dt  = datetime.strptime(expire_str[:10], "%Y-%m-%d")
                WARRANT_DETAIL_CACHE.setdefault(sid, {})
                prev = WARRANT_DETAIL_CACHE[sid].get(w_code)
                if prev is None or row_date > prev.get("_row_date",""):
                    WARRANT_DETAIL_CACHE[sid][w_code] = {
                        "code": w_code, "expire_dt": expire_dt,
                        "leverage": leverage, "delta": delta,
                        "bid": sf(row.get("BidPrice",0)),
                        "ask": sf(row.get("AskPrice",0)),
                        "volume": si(row.get("TradingVolume",0)),
                        "issuer": str(row.get("Issuer",""))[:3],
                        "_row_date": row_date,
                    }
            except: pass
        return vol_map, active_set

    # ── 方法一：TaiwanStockWarrantDetail 全量查（1 req）──────
    data, hit = fm1("TaiwanStockWarrantDetail", start_date=start, end_date=end)
    if not hit and data:
        vol_map, active_set = build_cache_and_set(data, elec_sids)
        if active_set:
            sorted_sids  = sorted(active_set, key=lambda s: -vol_map.get(s,0))
            cached_count = sum(len(v) for v in WARRANT_DETAIL_CACHE.values())
            print(f"  → [TaiwanStockWarrantDetail] 電子股有活躍認購：{len(sorted_sids)} 支（快取 {cached_count} 檔）")
            return sorted_sids, active_set

    # ── 方法二：對 30 支代表性熱門電子股批次查 TaiwanStockWarrant──
    print("  ! TaiwanStockWarrantDetail 無資料，改用批次查詢（30 支代表股）")
    SAMPLE_SIDS = [
        "2330","2454","2317","2308","2382","3711","2357","2379",
        "2395","3034","3008","2327","6770","2603","2881","2882",
        "2886","2891","2892","2884","5274","2337","2376","2408",
        "3481","2301","2303","2313","3045","6446",
    ]
    sample      = [s for s in SAMPLE_SIDS if s in elec_sids]
    all_rows    = []
    for sid in sample:
        d, h = fm1("TaiwanStockWarrant", sid, start, end)
        time.sleep(0.08)
        if h: break
        all_rows.extend(d)
    if all_rows:
        vol_map, active_set = build_cache_and_set(all_rows, elec_sids)
        if active_set:
            # 補充其他電子股（排在活躍標的後面）
            rest = [s for s in elec_sids if s not in active_set and s not in EXCLUDE_SIDS]
            combined = sorted(active_set, key=lambda s: -vol_map.get(s,0)) + rest
            print(f"  → [批次查詢] 活躍 {len(active_set)} 支 + 其他 {len(rest)} 支，限 400")
            return combined[:400], active_set

    # ── 最終 fallback：直接取電子股限量 400 支 ──────────────
    print("  ! 所有方法失敗，fallback 用限量電子股（max 400）")
    fallback = [s for s in elec_sids if s not in EXCLUDE_SIDS]
    return fallback[:400], set(fallback[:400])

# ── 第一階段：快速粗篩 ──────────────────────────────────────
def quick_filter(scan_sids, stock_meta, quick_start, end_date):
    survivors = []
    stop = False
    req  = 0
    for idx, sid in enumerate(scan_sids):
        if stop: break
        if (idx + 1) % 100 == 0:
            print(f"  ... 粗篩 {idx+1}/{len(scan_sids)}  存活:{len(survivors)}  req:{req}")

        data, hit = fm1("TaiwanStockPrice", sid, quick_start, end_date)
        req += 1
        if hit: stop = True; break
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

        last_dt = datetime.strptime(today_p["date"], "%Y-%m-%d").date()
        ref_dt  = datetime.strptime(end_date, "%Y-%m-%d").date()
        if (ref_dt - last_dt).days > 5: continue

        info   = stock_meta.get(sid, {})
        market = info.get("market", "tse")
        close  = today_p["close"]
        volume = today_p["volume"]
        spread = today_p["spread"]

        if close < 50 or close > 3000:                        continue
        if volume < (50000 if market == "otc" else 100000):   continue
        if spread < 0:                                         continue

        survivors.append({
            "sid":       sid,
            "info":      info,
            "today_p":   today_p,
            "data_date": today_p["date"],
        })

    print(f"  → 粗篩完成：{len(survivors)} 支存活（掃 {len(scan_sids)} 支，用 {req} req）")
    return survivors, req, stop

# ── 第二階段：完整歷史 ──────────────────────────────────────
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

# ── 三大法人 & 融資 ─────────────────────────────────────────
def fetch_inst(sid, start_date, end_date):
    data, hit = fm1("TaiwanStockInstitutionalInvestorsBuySell", sid, start_date, end_date)
    if hit: return {}, True
    by_date = {}
    for row in data:
        date = str(row.get("date",""))
        name = str(row.get("name",""))
        net  = si(row.get("buy",0)) - si(row.get("sell",0))
        if date not in by_date:
            by_date[date] = {"foreign_net":0,"trust_net":0,"dealer_net":0}
        if name == "Foreign_Investor":
            by_date[date]["foreign_net"] += net
        elif name == "Foreign_Dealer_Self":
            by_date[date]["foreign_net"] += net
        elif name == "Investment_Trust":
            by_date[date]["trust_net"] += net
        elif name in ("Dealer_self", "Dealer_Hedging"):
            by_date[date]["dealer_net"] += net
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
    bal = si(latest.get("MarginPurchaseTodayBalance",1)) or 1
    return {
        "margin_buy": si(latest.get("MarginPurchaseBuy",0)),
        "margin_bal": bal,
    }, False

# ── 權證明細查表（優先快取，fallback 個股查詢）──────────────
def get_warrant_detail(sid, data_date_str):
    """
    V24：優先從 WARRANT_DETAIL_CACHE 查表（Step② 已抓過全市場近15天明細）。
    快取命中率預期 90%+ （因為 Step② 已抓電子股全量），完全不需要再打 API。
    只有極少數情況才 fallback 到個股查詢。
    """
    dt = datetime.strptime(data_date_str, "%Y-%m-%d")
    warrants = []

    cached = WARRANT_DETAIL_CACHE.get(sid)
    if cached:
        for w_code, w in cached.items():
            try:
                days_left = (w["expire_dt"] - dt).days
                if days_left < 20:                              continue
                if w["leverage"] <= 0 or w["leverage"] > 15:   continue
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
                    "bid":         w["bid"],
                    "ask":         w["ask"],
                    "volume":      w["volume"],
                    "leverage_ok": 4 < w["leverage"] < 12,
                })
            except: continue

    if not warrants:
        # Fallback：個股查詢（窗口15天）
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
                if days_left < 20:                 continue
                if leverage <= 0 or leverage > 15: continue
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

    # 排序：成交量優先，再依槓桿合格、delta 距離 0.55 最近
    warrants.sort(key=lambda x: (-x["volume"], 0 if x["leverage_ok"] else 1, abs(x["delta"]-0.55)))
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

    print(f"[{now.strftime('%H:%M:%S')} 台灣時間] fetch_twse V24 開始 {today}")
    print(f"  FinMind token: {'已設定（600req/hr）' if TOKEN else '未設定（匿名300req/hr）'}")
    print(f"  V24 架構：電子股 meta + 活躍權證快取 → 粗篩 → 精篩（預估總 req < 400）")

    # ── ① 電子股 meta（2 req：WithWarrant + TaiwanStockInfo）──
    print("  ► ① 取電子股基本資料...")
    stock_meta  = fetch_electronics_meta();    req += 1
    elec_sids   = fetch_electronics_sids();    req += 1
    if not stock_meta or not elec_sids:
        print("  ! ① 失敗，中止"); return
    print(f"  → 股票 meta：{len(stock_meta)} 支；電子股：{len(elec_sids)} 支")
    time.sleep(0.3)

    # ── ② 活躍認購權證（1 req，縮減候選池 + 快取明細）──────────
    print("  ► ② 取電子股活躍認購權證（近15天）...")
    scan_sids, active_set = fetch_active_warrant_targets(elec_sids, now)
    req += 1
    if not scan_sids:
        print("  ! ② 無任何電子股有認購權證交易，中止")
        _write_empty(now, today8, req); return
    print(f"  → 掃描候選：{len(scan_sids)} 支（電子股 ∩ 近15天有認購交易）")
    time.sleep(0.3)

    # ── ③ 第一階段：快速粗篩（近4天，只掃候選池）──────────────
    # V24：硬性上限保護，確保不論 fallback 結果多少都不超過 450 支
    SCAN_HARD_LIMIT = 450
    if len(scan_sids) > SCAN_HARD_LIMIT:
        print(f"  ⚠ 候選池 {len(scan_sids)} 支超過上限，截斷為 {SCAN_HARD_LIMIT} 支")
        scan_sids = scan_sids[:SCAN_HARD_LIMIT]
    quick_start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
    print(f"  ► ③ 第一階段粗篩（{quick_start}~{end_date}，共 {len(scan_sids)} 支）...")
    survivors, q_req, stop = quick_filter(scan_sids, stock_meta, quick_start, end_date)
    req += q_req

    if not survivors:
        print("  ! 粗篩後無存活標的，可能資料尚未更新")
        _write_empty(now, today8, req); return
    # V24：402 提前停止時，survivors 已有部分結果，繼續往下跑
    if stop:
        print(f"  ⚠ 粗篩因 402 提前停止，但已存活 {len(survivors)} 支，繼續精篩")
        stop = False  # 重置 stop，讓精篩繼續跑（此時 req 跨小時，API 已重置）

    date_votes  = Counter(s["data_date"] for s in survivors)
    actual_date = date_votes.most_common(1)[0][0]
    print(f"  → 實際資料日期：{actual_date}")

    # ── ④ 第二階段：完整歷史精篩 ──────────────────────────────
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

    print(f"  → 精篩完成：{len(candidates)} 支候選（req={req}）")

    if not candidates:
        print("  ! 無候選，可能資料尚未更新")
        _write_empty(now, today8, req); return

    # ── ⑤ Top30：三大法人 + 融資 ─────────────────────────────
    candidates.sort(key=lambda x: x["chg"], reverse=True)
    top30      = candidates[:30]
    inst_start = (datetime.strptime(actual_date, "%Y-%m-%d") - timedelta(days=20)).strftime("%Y-%m-%d")

    print(f"  ► ⑤ 三大法人 + 融資（{len(top30)} 支）...")
    inst_map   = {}
    margin_map = {}
    for c in top30:
        if stop: break
        sid = c["sid"]
        res, hit = fetch_inst(sid, inst_start, actual_date);   req += 1
        if hit: stop = True; break
        inst_map[sid] = res;   time.sleep(0.12)
        res, hit = fetch_margin(sid, inst_start, actual_date); req += 1
        if hit: stop = True; break
        margin_map[sid] = res; time.sleep(0.10)

    print(f"  → ⑤ 完成（req={req}）")

    # ── 完整評分 + 取 TopN ───────────────────────────────────
    scored = []
    for c in top30:
        sid  = c["sid"]
        hist = c["hist"]
        if len(hist) < 5: continue
        score, reasons, warnings, chg = calc_score(
            c["today_p"], hist, inst_map.get(sid,{}), margin_map.get(sid,{})
        )
        # 有認購權證加分
        if sid in active_set: score = min(100, score + 2)
        if score < 25: continue

        ma_c = [h["close"] for h in hist]
        # V24：從快取取權證明細（不用再打 API）
        warrants = get_warrant_detail(sid, actual_date)

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
            "has_warrant": sid in active_set,
            "warrants":   warrants,   # V24：真實明細，從快取取
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top_n = scored[:TOP_N]

    for c in top_n:
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
        "stocks":           top_n,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成！資料:{actual_date}  掃:{len(scan_sids)}  存活:{len(survivors)}  候選:{len(scored)}  精選:{len(top_n)}  API:{req}")
    for s in top_n:
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
