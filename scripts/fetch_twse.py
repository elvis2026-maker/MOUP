#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V21
==============================
V20 核心改動：只掃電子股

  動機：
    V19 依股票代碼數字排序取前 420 支，1000~2000 號傳產股先掃，
    電子股（代碼多在 2300~6700）部分根本排不進前 420，造成遺漏。
    使用者只操作電子股，掃傳產股完全是浪費 API 請求數。

  V20 改法：
    ① fetch_warrant_targets() 取得有認購權證的全部標的（同 V19）
    ② 新增 fetch_electronics_sids()：
       查 FinMind TaiwanStockInfo，取 industry_category 屬於電子 8 大類的股票代碼集合
    ③ build_scan_list() 改為：warrant_targets ∩ 電子股 → 全掃，無人工上限
       預估有認購權證且屬電子股約 200~280 支，API 請求數反而更少

  電子 8 大產業類別（TWSE 官方分類）：
    半導體業、電腦及週邊設備業、光電業、通信網路業、
    電子零組件業、電子通路業、資訊服務業、其他電子業

  沿用 V19 的兩階段掃描策略：
    ① 第一階段（快速粗篩）：只取近 4 天資料，快速淘汰收跌/無量股
    ② 第二階段（精細評分）：存活候選取近 35 天歷史，計算完整評分

  API 預算（有 TOKEN 600 req/hr）：
    ① TaiwanStockInfoWithWarrant   1 req
    ② TaiwanStockInfo（電子股篩選）1 req
    ③ 粗篩 ~250 支電子股           ~250 req
    ④ 精篩存活 ~80 支              ~80 req
    ⑤ 法人 + 融資 Top15 × 2        30 req
    合計：~362 req，完全在 600/hr 上限內
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone
from collections import Counter

TZ_TW       = timezone(timedelta(hours=8))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N       = 15
FM_URL      = "https://api.finmindtrade.com/api/v4/data"
TOKEN       = os.environ.get("FINMIND_TOKEN", "")

# V20：電子 8 大產業類別（TWSE 官方 industry_category 值）
ELECTRONICS_CATEGORIES = {
    "半導體業",
    "電腦及週邊設備業",
    "光電業",
    "通信網路業",
    "電子零組件業",
    "電子通路業",
    "資訊服務業",
    "其他電子業",
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

# ── V20新增：電子股代碼集合（1 req）────────────────────────────
def fetch_electronics_sids():
    """
    查 FinMind TaiwanStockInfo，取 industry_category 屬電子 8 大類的代碼。
    回傳 set[str]
    """
    data, _ = fm1("TaiwanStockInfo")
    elec_sids = set()
    cat_count = {}
    for row in data:
        sid = str(row.get("stock_id", "")).strip()
        if not (sid.isdigit() and len(sid) == 4): continue
        cat = str(row.get("industry_category", "")).strip()
        t   = str(row.get("type", "")).strip()
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

WARRANT_DETAIL_CACHE = {}

def fetch_active_warrant_targets():
    return [], set()

# ── V20：建立掃描清單（電子股 ∩ 有認購權證）────────────────────
def build_scan_list(warrant_targets, elec_sids):
    """
    V20：warrant_targets（有認購權證）與 elec_sids（電子股）取交集，全掃，不設人工上限。
    預估約 200~280 支，API 請求數比 V19 的 420 支更省。
    """
    all_sids = sorted([
        s for s in warrant_targets
        if s not in EXCLUDE_SIDS and s in elec_sids
    ])
    print(f"  → 電子股 ∩ 有認購權證：{len(all_sids)} 支（全部掃描，無上限）")
    return all_sids

# ── 第一階段：快速粗篩（只取近 4 天，速度快）──────────────────────
def quick_filter(scan_sids, warrant_targets, quick_start, end_date):
    """
    每 req 只取一支股票的近 4 天資料。
    淘汰條件（保守，寧可放行也不誤殺）：
      - 最新資料距 end_date > 5 天（停牌/下市）
      - 今日收跌（spread < 0）
      - 成交量嚴重不足
      - 股價 <50 或 >3000（低價股很少有認購權證）
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

        last_dt = datetime.strptime(today_p["date"], "%Y-%m-%d").date()
        ref_dt  = datetime.strptime(end_date, "%Y-%m-%d").date()
        if (ref_dt - last_dt).days > 5: continue

        info   = warrant_targets.get(sid, {})
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
# V19 修正最關鍵的 bug：name 欄位比對邏輯完全錯誤！
#   V19 用 if "外資" in name 比對，但 FinMind 回傳的 name 欄位是英文：
#     Dealer_Hedging, Dealer_self, Foreign_Dealer_Self,
#     Foreign_Investor, Investment_Trust
#   中文關鍵字「外資/投信/自營」永遠比對不到任何英文字串，
#   所以加總結果永遠是初始值 0 —— 這就是截圖「外資0 投信0 自營0 合計0」
#   的真正原因：不是沒資料，是比對條件寫錯，迴圈跑了但什麼都沒加進去。
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
        # 正確的英文 name 值比對
        if name == "Foreign_Investor":
            by_date[date]["foreign_net"] += net
        elif name == "Foreign_Dealer_Self":
            by_date[date]["foreign_net"] += net  # 外資自營商併入外資
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
    # V19 修正：欄位名稱錯誤！
    #   舊：MarginPurchaseRemainAmount  ← 不存在
    #   新：MarginPurchaseTodayBalance  ← 正確欄位（融資今日餘額）
    bal = si(latest.get("MarginPurchaseTodayBalance",1)) or 1
    return {
        "margin_buy": si(latest.get("MarginPurchaseBuy",0)),
        "margin_bal": bal,
    }, False

# ── 權證標記（誠實版：僅標記有/無，0 req）───────────────────────
def has_warrant(sid, warrant_targets):
    """
    V19：誠實標記「這支股票名下是否有發行過認購權證」。
    FinMind 免費版沒有提供權證代碼/履約價/到期日等明細表
    （這些都在付費 Backer/Sponsor tier），所以不再產生假的明細列表。
    回傳 True/False。
    """
    return sid in warrant_targets

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

    print(f"[{now.strftime('%H:%M:%S')} 台灣時間] fetch_twse V20 開始 {today}")
    print(f"  FinMind token: {'已設定（600req/hr）' if TOKEN else '未設定（匿名300req/hr）'}")
    print(f"  掃描策略：V20 兩階段（電子股 ∩ 有認購權證 全掃，無人工上限）")

    # ── ① 有認購權證的全部標的（1 req，免費）─────────────────────
    print("  ► ① 取有認購權證標的（全量，免費 dataset）...")
    warrant_targets = fetch_warrant_targets(); req += 1
    if not warrant_targets: print("  ! ① 失敗，中止"); return
    print(f"  → {len(warrant_targets)} 支")
    time.sleep(0.3)

    # ── V20新增：取電子股代碼集合（1 req）──────────────────────
    print("  ► V20：取電子股代碼（TaiwanStockInfo）...")
    elec_sids = fetch_electronics_sids(); req += 1
    if not elec_sids:
        print("  ! 電子股資料取得失敗，fallback 全量掃描")
        elec_sids = set(warrant_targets.keys())
    time.sleep(0.3)

    # ── 建立掃描清單（V20：電子股 ∩ 有認購權證，全掃無上限）──
    scan_sids = build_scan_list(warrant_targets, elec_sids)

    # ── ② 第一階段：快速粗篩（近 4 天資料）──────────────────────
    quick_start = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
    print(f"  ► ② 第一階段粗篩（{quick_start}~{end_date}，共 {len(scan_sids)} 支）...")
    survivors, q_req, stop = quick_filter(scan_sids, warrant_targets, quick_start, end_date)
    req += q_req

    if not survivors:
        print("  ! 粗篩後無存活標的，可能資料尚未更新")
        _write_empty(now, today8, req); return

    date_votes  = Counter(s["data_date"] for s in survivors)
    actual_date = date_votes.most_common(1)[0][0]
    print(f"  → 實際資料日期：{actual_date}")

    # ── ④ 第二階段：對存活候選取完整歷史 ─────────────────────────
    full_start = (datetime.strptime(actual_date, "%Y-%m-%d") - timedelta(days=35)).strftime("%Y-%m-%d")
    print(f"  ► ③ 第二階段精篩（{full_start}~{actual_date}，共 {len(survivors)} 支）...")

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

    print(f"  ► ④ 三大法人 + 融資（{len(top30)} 支，{inst_start}~{actual_date}）...")
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

    # ── 完整評分，取 TopN ─────────────────────────────────────────
    scored = []
    for c in top30:
        sid  = c["sid"]
        hist = c["hist"]
        if len(hist) < 5: continue
        score, reasons, warnings, chg = calc_score(
            c["today_p"], hist,
            inst_map.get(sid,{}), margin_map.get(sid,{})
        )
        # 有認購權證加分（誠實版：只能判斷有/無，固定加 2 分）
        hw = has_warrant(sid, warrant_targets)
        score = min(100, score + (2 if hw else 0))
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
            "has_warrant": hw,
            "warrants":   [],   # V19：不再產生假明細，前端改用 has_warrant 顯示
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top_n = scored[:TOP_N]

    print(f"  → 認購權證標記：{sum(1 for c in top_n if c.get('has_warrant'))}/{len(top_n)} 支有發行過認購權證")
    print(f"  ⚠ 注意：FinMind 免費版無權證明細 API，無法提供代碼/履約價/到期日/槓桿，請至證券 APP 查詢實際可投資的權證")

    # ── 機率標籤 ─────────────────────────────────────────────
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
