#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V9
================================
V9 根本修正：
  Bug（V8 根本問題）：FinMind TaiwanStockPrice 免費帳號對 2026 年日期全部回傳 400
    → 整個選股流程完全失效，stocks.json 永遠是空的
    → 原因：FinMind 免費版不提供最近資料，需要付費 token 才能查近期日行情

  V9 改用 TWSE 官方 openapi（完全免費，境外 GitHub Actions IP 可用）：
    ① 全市場今日行情：openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
    ② 上櫃行情：       openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL_TPEX
    ③ 三大法人：       openapi.twse.com.tw/v1/fund/TWT38U
    ④ 個股歷史：       openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY
    ⑤ 認購權證：       openapi.twse.com.tw/v1/exchangeReport/TWTB4U  (上市)
                        openapi.twse.com.tw/v1/exchangeReport/TWTB8U  (上市買斷)
    
  備用（FinMind, 僅 Warrant 仍用 FinMind，日行情完全不用）：
    - FinMind TaiwanStockWarrant（認購權證備用，成功率高）

  fetch_live.py 改用：
    - FinMind taiwan_stock_tick_snapshot（10支即時快照，只需10req/次，無需token）
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone

TZ_TW       = timezone(timedelta(hours=8))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N       = 10

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
}

FINMIND_URL   = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

def tw_now():
    return datetime.now(TZ_TW)

def today_str():
    return tw_now().strftime("%Y-%m-%d")

def today_str8():
    return tw_now().strftime("%Y%m%d")

def prev_trading_dates(n=25):
    result, d = [], tw_now()
    while len(result) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            result.append(d.strftime("%Y%m%d"))  # 返回8碼格式
    return result

def safe_float(v, default=0.0):
    try:    return float(str(v).replace(",","").replace("+","").strip())
    except: return default

def safe_int(v, default=0):
    try:    return int(str(v).replace(",","").strip())
    except: return default

# ── TWSE openapi 共用請求 ──────────────────────────────
def twse_get(endpoint, params=None, retries=3):
    """呼叫 openapi.twse.com.tw，不需要 token，境外 IP 可用"""
    url = f"https://openapi.twse.com.tw/v1/{endpoint}"
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and data.get("stat") == "OK":
                return data.get("data", [])
            return data
        except Exception as e:
            print(f"  [retry {i+1}/{retries}] TWSE openapi {endpoint} → {e}")
            time.sleep(2 * (i+1))
    return []

def twse_get_report(endpoint, params=None, retries=3):
    """呼叫 www.twse.com.tw/exchangeReport 盤後報表（有些 endpoint 在 openapi 沒有）"""
    url = f"https://www.twse.com.tw/exchangeReport/{endpoint}"
    for i in range(retries):
        try:
            p = {"response": "json"}
            if params: p.update(params)
            r = requests.get(url, params=p, headers=HEADERS, timeout=20)
            r.raise_for_status()
            d = r.json()
            if d.get("stat") == "OK":
                return d.get("data", []), d.get("fields", [])
            return [], []
        except Exception as e:
            print(f"  [retry {i+1}/{retries}] TWSE report {endpoint} → {e}")
            time.sleep(2 * (i+1))
    return [], []

# ── ① 全市場今日行情（openapi）─────────────────────────
def fetch_all_prices_today(date8=None):
    """
    一次取得全市場今日收盤行情（上市+上櫃）
    openapi.twse.com.tw 在盤後（約15:30後）才有當日資料
    """
    result = {}

    # 上市
    tse_data = twse_get("exchangeReport/STOCK_DAY_ALL")
    print(f"    上市 STOCK_DAY_ALL: {len(tse_data)} 筆")
    for item in tse_data:
        try:
            sid   = str(item.get("Code","")).strip()
            if not (sid.isdigit() and len(sid) == 4): continue
            close = safe_float(item.get("ClosingPrice","0"))
            open_ = safe_float(item.get("OpeningPrice","0"))
            high  = safe_float(item.get("HighestPrice","0"))
            low   = safe_float(item.get("LowestPrice","0"))
            vol   = safe_int(item.get("TradeVolume","0"))
            chg   = safe_float(item.get("Change","0"))  # 漲跌價差（元）
            name  = str(item.get("Name",sid)).strip()
            if close <= 0: continue
            prev = round(close - chg, 2)
            chg_pct = round(chg / prev * 100, 2) if prev > 0 else 0
            result[sid] = {"name":name,"close":close,"open":open_,"high":high,
                           "low":low,"volume":vol,"chg_pct":chg_pct,"market":"tse"}
        except: continue
    time.sleep(0.5)

    # 上櫃
    otc_data = twse_get("exchangeReport/STOCK_DAY_ALL_TPEX")
    print(f"    上櫃 STOCK_DAY_ALL_TPEX: {len(otc_data)} 筆")
    for item in otc_data:
        try:
            sid   = str(item.get("Code","")).strip()
            if not (sid.isdigit() and len(sid) == 4): continue
            if sid in result: continue  # 上市優先
            close = safe_float(item.get("Close","0"))
            open_ = safe_float(item.get("Open","0"))
            high  = safe_float(item.get("High","0"))
            low   = safe_float(item.get("Low","0"))
            vol   = safe_int(item.get("Volume","0"))
            chg   = safe_float(item.get("Change","0"))
            name  = str(item.get("Name",sid)).strip()
            if close <= 0: continue
            prev = round(close - chg, 2)
            chg_pct = round(chg / prev * 100, 2) if prev > 0 else 0
            result[sid] = {"name":name,"close":close,"open":open_,"high":high,
                           "low":low,"volume":vol,"chg_pct":chg_pct,"market":"otc"}
        except: continue

    print(f"  → 全市場行情：{len(result)} 支")
    return result

# ── ② 個股歷史行情（openapi，逐支查）──────────────────
def fetch_stock_history(sid, date8):
    """
    取得個股近 N 月行情（openapi STOCK_DAY）
    date8: YYYYMMDD，以該月份為準
    """
    items = twse_get("exchangeReport/STOCK_DAY", {"stockNo": sid, "date": date8})
    result = []
    for item in items:
        try:
            # openapi 回傳中華民國年份日期，格式 "115/06/29"
            date_str = str(item.get("Date","")).strip()
            parts    = date_str.split("/")
            if len(parts) == 3:
                y, m, d = int(parts[0])+1911, int(parts[1]), int(parts[2])
                iso_date = f"{y:04d}-{m:02d}-{d:02d}"
            else:
                iso_date = date_str
            close = safe_float(item.get("ClosingPrice","0"))
            vol   = safe_int(item.get("TradeVolume","0"))
            if close > 0:
                result.append({"date": iso_date, "close": close, "volume": vol})
        except: continue
    return sorted(result, key=lambda x: x["date"])

def fetch_price_history_multi_month(sid, months=2):
    """取近2個月行情，合併去重"""
    now   = tw_now()
    dates = []
    for i in range(months):
        d = now - timedelta(days=30 * i)
        dates.append(d.strftime("%Y%m%d"))
    
    all_rows = {}
    for date8 in dates:
        rows = fetch_stock_history(sid, date8)
        for r in rows:
            all_rows[r["date"]] = r
        time.sleep(0.3)
    
    return sorted(all_rows.values(), key=lambda x: x["date"])

# ── ③ 三大法人（openapi TWT38U）──────────────────────
def fetch_institutional_today():
    """
    openapi.twse.com.tw/v1/fund/TWT38U
    三大法人今日買賣超（盤後更新）
    """
    data = twse_get("fund/TWT38U")
    result = {}
    for item in data:
        try:
            sid = str(item.get("Code","")).strip()
            if not sid: continue
            fn  = safe_int(item.get("Foreign_Investor_Buy","0")) - safe_int(item.get("Foreign_Investor_Sell","0"))
            tr  = safe_int(item.get("Investment_Trust_Buy","0")) - safe_int(item.get("Investment_Trust_Sell","0"))
            dn  = safe_int(item.get("Dealer_Buy","0")) - safe_int(item.get("Dealer_Sell","0"))
            tn  = fn + tr + dn
            result[sid] = {
                "foreign_net": fn // 1000,
                "trust_net":   tr // 1000,
                "dealer_net":  dn // 1000,
                "total_net":   tn // 1000,
            }
        except: continue
    print(f"  → 三大法人：{len(result)} 支")
    return result

# ── ④ 認購權證（openapi TWTB4U + FinMind 備用）────────
def fetch_warrants_openapi(date8):
    """TWSE openapi 認購權證日報"""
    result = {}
    today  = datetime.strptime(date8, "%Y%m%d")

    for ep in ["exchangeReport/TWTB4U"]:
        data, fields = twse_get_report(ep, {"date": date8})
        print(f"    {ep}: {len(data)} 筆")
        for row in data:
            try:
                if len(row) < 14: continue
                if "認購" not in str(row[4]): continue
                sid        = str(row[2]).strip()
                w_code     = str(row[0]).strip()
                # 到期日：民國年 "115/12/31"
                expire_str = str(row[5]).strip()
                parts      = expire_str.split("/")
                if len(parts) != 3: continue
                expire_dt  = datetime(int(parts[0])+1911, int(parts[1]), int(parts[2]))
                days_left  = (expire_dt - today).days
                vol        = safe_int(row[10])
                lev        = safe_float(row[11])
                iv         = safe_float(str(row[12]).replace("%",""))
                dlt        = safe_float(row[13])
                if days_left < 20 or vol < 50 or lev <= 0 or lev > 15: continue
                if dlt >= 0.70:    mn = "深度價內"
                elif dlt >= 0.55:  mn = "輕度價內"
                elif dlt >= 0.45:  mn = "價平"
                elif dlt >= 0.30:  mn = "輕度價外"
                else:              mn = "價外"
                result.setdefault(sid, []).append({
                    "code": w_code, "issuer": str(row[1]).strip()[:2],
                    "type": "call", "expire": expire_dt.strftime("%Y/%m/%d"),
                    "days_left": days_left, "leverage": round(lev,1),
                    "iv": round(iv,1), "delta": round(dlt,2), "moneyness": mn,
                    "bid": safe_float(row[8]), "ask": safe_float(row[9]),
                    "volume": vol, "leverage_ok": 4 < lev < 12,
                })
            except: continue
        time.sleep(0.5)

    return result

def fetch_warrants_finmind(today_date):
    """FinMind 備用權證（免費，僅用於權證，不抓日行情）"""
    if not FINMIND_TOKEN:
        # 不帶 token 也可查，但有 300req/hr 限制
        pass
    today_dt = datetime.strptime(today_date, "%Y-%m-%d")
    start    = (today_dt - timedelta(days=5)).strftime("%Y-%m-%d")
    params   = {"dataset":"TaiwanStockWarrant","start_date":start,"end_date":today_date}
    if FINMIND_TOKEN: params["token"] = FINMIND_TOKEN
    try:
        r = requests.get(FINMIND_URL, params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        d = r.json()
        if d.get("status") != 200: return {}
        rows = d.get("data", [])
    except Exception as e:
        print(f"  ! FinMind warrant 失敗：{e}")
        return {}

    result   = {}
    today_dt2 = datetime.strptime(today_date, "%Y-%m-%d")
    for row in rows:
        try:
            call_put = str(row.get("PutCall","")).strip()
            if "C" not in call_put and "認購" not in call_put: continue
            sid        = str(row.get("underlying_stock","")).strip()
            w_code     = str(row.get("stock_id","")).strip()
            expire_str = str(row.get("ExpirationDate","")).strip()
            leverage   = safe_float(row.get("EffectiveLeverage",0))
            iv         = safe_float(row.get("ImpliedVolatility",0))
            delta      = safe_float(row.get("Delta",0))
            bid        = safe_float(row.get("BidPrice",0))
            ask        = safe_float(row.get("AskPrice",0))
            vol        = safe_int(row.get("TradingVolume",0))
            if not sid or not w_code: continue
            try: expire_dt = datetime.strptime(expire_str[:10], "%Y-%m-%d")
            except: continue
            days_left = (expire_dt - today_dt2).days
            if days_left < 20 or vol < 50 or leverage <= 0 or leverage > 15: continue
            if delta >= 0.70:    mn = "深度價內"
            elif delta >= 0.55:  mn = "輕度價內"
            elif delta >= 0.45:  mn = "價平"
            elif delta >= 0.30:  mn = "輕度價外"
            else:                mn = "價外"
            result.setdefault(sid, []).append({
                "code":w_code,"issuer":str(row.get("Issuer",""))[:3],
                "type":"call","expire":expire_dt.strftime("%Y/%m/%d"),
                "days_left":days_left,"leverage":round(leverage,1),
                "iv":round(iv,1),"delta":round(delta,2),"moneyness":mn,
                "bid":bid,"ask":ask,"volume":vol,"leverage_ok":4<leverage<12,
            })
        except: continue

    def w_score(w):
        s = 0
        if w["leverage_ok"]: s += 2
        if w["volume"] > 500: s += 1
        if 0.45 <= w["delta"] <= 0.65: s += 1
        return s
    for sid in result:
        result[sid].sort(key=lambda x: (-w_score(x), -x["volume"]))
        result[sid] = result[sid][:3]
    return result

# ── ⑤ 評分 ─────────────────────────────────────────
def calc_ma(closes, n):
    if len(closes) < n: return None
    return round(statistics.mean(closes[-n:]), 2)

def calc_score(tp, hist_closes, hist_vols, inst):
    score, reasons, warnings = 0, [], []
    close   = tp["close"]
    high    = tp["high"]
    low     = tp["low"]
    chg_pct = tp["chg_pct"]

    # 量價 40分
    if chg_pct >= 5:    score += 16; reasons.append("強勢大漲 ≥5%")
    elif chg_pct >= 3:  score += 12; reasons.append("大漲 ≥3%")
    elif chg_pct >= 1:  score += 7;  reasons.append("溫和上漲")
    elif chg_pct < 0:   score -= 10; warnings.append("今日收跌")

    if high > low:
        cp = (close - low) / (high - low)
        if cp >= 0.8:    score += 14; reasons.append("收盤靠近最高點（買盤強）")
        elif cp >= 0.6:  score += 8
        elif cp < 0.3:   score -= 8;  warnings.append("長上影線（賣壓重）")

    if len(hist_vols) >= 5:
        avg_vol = statistics.mean(hist_vols[-5:])
        vr = tp["volume"] / avg_vol if avg_vol > 0 else 0
        if 1.5 <= vr <= 4:  score += 10; reasons.append(f"量能放大 {vr:.1f}x")
        elif vr > 4:        score += 5;  warnings.append("量能過度放大（注意追高）")
        elif vr < 0.7:      score -= 5;  warnings.append("量能萎縮")

    # 技術 30分
    if len(hist_closes) >= 20:
        ma5  = calc_ma(hist_closes, 5)
        ma10 = calc_ma(hist_closes, 10)
        ma20 = calc_ma(hist_closes, 20)
        if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
            score += 10; reasons.append("均線多頭排列")
        elif ma5 and ma10 and ma5 > ma10: score += 5
        if ma5  and close > ma5:   score += 4
        if ma20 and close > ma20:  score += 6; reasons.append(f"站上月線 MA20={ma20}")
        elif ma20 and close < ma20: score -= 5; warnings.append("跌破月線")
        if hist_closes:
            rh = max(hist_closes[-10:]) if len(hist_closes) >= 10 else hist_closes[-1]
            if close >= rh * 0.99: score += 10; reasons.append("突破近10日高點")

    # 籌碼 30分
    if inst:
        tn = inst.get("total_net", 0)
        fn = inst.get("foreign_net", 0)
        tr = inst.get("trust_net", 0)
        if tn > 5000:    score += 15; reasons.append(f"三大法人大幅買超 {tn}張")
        elif tn > 1000:  score += 10; reasons.append(f"三大法人買超 {tn}張")
        elif tn > 0:     score += 5
        elif tn < -3000: score -= 10; warnings.append("三大法人大幅賣超")
        if tr > 500:     score += 5;  reasons.append("投信積極買超")
        if fn > 3000:    score += 5;  reasons.append("外資積極買超")

    return max(0, min(100, score)), reasons, warnings

# ── 主程式 ────────────────────────────────────────
def main():
    now    = tw_now()
    today  = now.strftime("%Y-%m-%d")
    today8 = now.strftime("%Y%m%d")
    print(f"[{now.strftime('%H:%M:%S')} 台灣時間] fetch_twse V9 開始 {today}")
    print(f"  資料來源：TWSE openapi（不依賴 FinMind 日行情）")

    # ① 全市場今日行情
    print("  ► 全市場今日行情（TWSE openapi）...")
    all_today = fetch_all_prices_today(today8)

    if not all_today:
        # 往前找最近交易日
        past = prev_trading_dates(5)
        for d8 in past:
            d_str = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
            print(f"  ! 今日無資料，嘗試 {d_str}...")
            all_today = fetch_all_prices_today(d8)
            if all_today:
                today  = d_str
                today8 = d8
                break

    if not all_today:
        print("  ! 行情資料完全取得失敗，中止")
        return

    time.sleep(0.5)

    # ② 三大法人
    print("  ► 三大法人（TWSE openapi TWT38U）...")
    institutional = fetch_institutional_today()
    time.sleep(0.5)

    # ③ 認購權證
    print(f"  ► 認購權證（TWSE openapi TWTB4U）...")
    warrants = fetch_warrants_openapi(today8)
    if not warrants:
        print("  ! openapi 無權證資料，改用 FinMind...")
        warrants = fetch_warrants_finmind(today)
    print(f"  → {len(warrants)} 支標的有認購權證")
    time.sleep(0.5)

    # ④ 量價預篩
    print("  ► 量價預篩選...")
    pre_candidates = []
    for sid, tp in all_today.items():
        close   = tp["close"]
        volume  = tp["volume"]
        chg_pct = tp["chg_pct"]
        market  = tp.get("market","tse")

        if close < 10 or close > 5000: continue
        min_vol = 200000 if market == "otc" else 500000
        if volume < min_vol:           continue
        if chg_pct < 0.5:              continue

        pre_candidates.append(sid)

    print(f"  → 量價預篩選後：{len(pre_candidates)} 支候選")

    if len(pre_candidates) == 0:
        print("  ! 預篩結果為0，放寬條件...")
        for sid, tp in all_today.items():
            if tp["close"] < 10 or tp["close"] > 5000: continue
            if tp["volume"] < 100000: continue
            pre_candidates.append(sid)
        print(f"  → 放寬後：{len(pre_candidates)} 支候選")

    # 有認購權證的優先
    pre_candidates.sort(key=lambda s: (0 if s in warrants else 1, s))

    # ⑤ 個股歷史分析
    print(f"  ► 個股歷史分析（最多50支）...")
    all_candidates = []
    processed      = 0

    for sid in pre_candidates[:50]:
        tp = all_today[sid]
        processed += 1
        if processed % 10 == 0:
            print(f"  ... {processed}/{min(len(pre_candidates),50)}")

        # 近2個月歷史行情
        hist_rows   = fetch_price_history_multi_month(sid, months=2)
        # 只用今日之前的歷史（今日是從 STOCK_DAY_ALL 來的）
        hist_rows   = [h for h in hist_rows if h["date"] < today]
        if len(hist_rows) < 5: continue

        hist_closes = [h["close"] for h in hist_rows]
        hist_vols   = [h["volume"] for h in hist_rows]
        inst_today  = institutional.get(sid, {})

        score, reasons, warnings = calc_score(tp, hist_closes, hist_vols, inst_today)
        if sid in warrants: score = min(100, score + 2)

        if score >= 45:
            all_candidates.append({
                "sid":        sid,
                "name":       tp["name"],
                "close":      tp["close"],
                "change_pct": tp["chg_pct"],
                "volume":     tp["volume"],
                "market":     tp.get("market","tse"),
                "score":      score,
                "reasons":    reasons,
                "warnings":   warnings,
                "inst":       inst_today,
                "ma5":        calc_ma(hist_closes, 5),
                "ma10":       calc_ma(hist_closes, 10),
                "ma20":       calc_ma(hist_closes, 20),
                "warrants":   warrants.get(sid, []),
            })

    all_candidates.sort(key=lambda x: x["score"], reverse=True)
    top10 = all_candidates[:TOP_N]

    # ⑥ 機率標籤
    for c in top10:
        s = c["score"]
        if s >= 85:   c["prob"] = f"高（{min(82,60+(s-85)*2+15)}%）";  c["prob_level"] = "high"
        elif s >= 70: c["prob"] = f"中高（{62+(s-70)}%）";              c["prob_level"] = "medium-high"
        elif s >= 55: c["prob"] = f"中（{48+(s-55)}%）";                c["prob_level"] = "medium"
        else:         c["prob"] = "偏低（<48%）";                        c["prob_level"] = "low"

    output = {
        "updated_at":       now.strftime("%Y/%m/%d %H:%M"),
        "trade_date":       today8,
        "total_scanned":    len(pre_candidates),
        "candidates_count": len(all_candidates),
        "stocks":           top10,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成！預篩 {len(pre_candidates)} 支，分析 {processed} 支，"
          f"候選 {len(all_candidates)} 支，精選 {len(top10)} 支")
    for s in top10:
        wc = len(s.get("warrants",[]))
        print(f"  [{s['score']:3d}] {s['sid']} {s['name']:8s} {s['change_pct']:+.1f}%  {s['prob']}  權證:{wc}支")

if __name__ == "__main__":
    main()
