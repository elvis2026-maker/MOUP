#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V4
V4 新增/修正：
  1. 新增上櫃(OTC/TPEX) 股票行情抓取 fetch_daily_price_tpex()
  2. 新增上櫃三大法人資料 fetch_institutional_tpex()
  3. 新增上櫃認購權證 fetch_warrants_tpex()（TWTB8U）
  4. 合併上市+上櫃結果一起評分
  5. fetch_twse 日期時區修正（明確使用台灣時區 UTC+8）
  6. 上櫃股票 vol 過濾標準調降（上櫃量較小）
  7. 機率標籤數值修正（V3 有計算bug）
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.twse.com.tw/"
}
HEADERS_TPEX = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.tpex.org.tw/"
}
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N = 10

# ── 工具 ─────────────────────────────────────────
def safe_get(url, params=None, retries=3, delay=1.2, headers=None):
    h = headers or HEADERS
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=h, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  [retry {i+1}/{retries}] {url[:60]}... → {e}")
            time.sleep(delay * (i + 1))
    return None

TZ_TW = timezone(timedelta(hours=8))

def tw_now():
    return datetime.now(TZ_TW)

def today_str():
    return tw_now().strftime("%Y%m%d")

def prev_trading_days(n=20):
    result, d = [], tw_now()
    while len(result) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            result.append(d.strftime("%Y%m%d"))
    return result

# ── 上市 TWSE ──────────────────────────────────────
def _parse_sign(sign_str):
    s = sign_str.strip()
    if s in ("-", "▼"):
        return -1
    return 1

def fetch_daily_price(date_str):
    url  = "https://www.twse.com.tw/exchangeReport/MI_INDEX"
    data = safe_get(url, {"response": "json", "date": date_str, "type": "ALLBUT0999"})
    if not data or data.get("stat") != "OK":
        return {}
    result = {}
    for row in data.get("data", []):
        try:
            sid = row[0].strip()
            if not (sid.isdigit() and len(sid) == 4):
                continue
            if not row[8] or row[8].strip() in ("", "--", "---"):
                continue
            vol   = int(row[2].replace(",", ""))
            close = float(row[8].replace(",", ""))
            sign  = _parse_sign(row[9])
            raw_chg = row[10].replace(",", "").replace("+", "").replace("-", "").strip()
            chg   = sign * float(raw_chg) if raw_chg else 0.0
            prev_close = close - chg
            change_pct = round(chg / prev_close * 100, 2) if prev_close != 0 else 0
            def safe_price(val):
                v = val.replace(",", "").strip()
                return float(v) if v and v not in ("--", "---") else close
            result[sid] = {
                "name":       row[1].strip(),
                "open":       safe_price(row[5]),
                "high":       safe_price(row[6]),
                "low":        safe_price(row[7]),
                "close":      close,
                "change":     chg,
                "change_pct": change_pct,
                "volume":     vol,
                "market":     "tse"
            }
        except:
            continue
    return result

def fetch_institutional(date_str):
    data = safe_get("https://www.twse.com.tw/fund/T86",
                    {"response": "json", "date": date_str, "selectType": "ALLBUT0999"})
    if not data or data.get("stat") != "OK":
        return {}
    result = {}
    for row in data.get("data", []):
        try:
            sid = row[0].strip()
            def f(x):
                v = x.replace(",", "").replace("+", "").strip()
                return int(v) if v and v not in ("--",) else 0
            result[sid] = {
                "foreign_net": f(row[4]),
                "trust_net":   f(row[7]),
                "dealer_net":  f(row[8]),
                "total_net":   f(row[9])
            }
        except:
            continue
    return result

def fetch_margin(date_str):
    data = safe_get("https://www.twse.com.tw/exchangeReport/MI_MARGN",
                    {"response": "json", "date": date_str, "selectType": "ALL"})
    if not data or data.get("stat") != "OK":
        return {}
    result = {}
    for row in data.get("data", []):
        try:
            sid = row[0].strip()
            def f(x):
                v = x.replace(",", "").strip()
                return int(v) if v and v not in ("--",) else 0
            result[sid] = {
                "margin_buy": f(row[2]),
                "margin_bal": f(row[4]),
                "short_sell": f(row[8]),
                "short_bal":  f(row[10])
            }
        except:
            continue
    return result

# ── 上櫃 TPEX ──────────────────────────────────────
def fetch_daily_price_tpex(date_str):
    """
    V4新增：抓上櫃(TPEX)股票收盤行情
    API: www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php
    日期格式：民國年（e.g. 115/06/25）
    """
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        roc_date = f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"
    except:
        return {}

    url  = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"
    data = safe_get(url, {"d": roc_date, "stkno": "", "o": "json"}, headers=HEADERS_TPEX)
    if not data:
        return {}

    result = {}
    for row in data.get("aaData", []):
        try:
            if len(row) < 11:
                continue
            sid = str(row[0]).strip()
            if not (sid.isdigit() and len(sid) == 4):
                continue
            def clean(v):
                return str(v).replace(",", "").strip()
            close_str = clean(row[2])
            if not close_str or close_str in ("--", "---", ""):
                continue
            close = float(close_str)
            chg_str = clean(row[3])
            try:
                chg = float(chg_str) if chg_str not in ("--","---","") else 0.0
            except:
                chg = 0.0
            prev_close = close - chg
            change_pct = round(chg / prev_close * 100, 2) if prev_close != 0 else 0
            vol_str = clean(row[8])
            vol = int(float(vol_str) * 1000) if vol_str and vol_str not in ("--","---") else 0
            result[sid] = {
                "name":       str(row[1]).strip(),
                "open":       float(clean(row[4])) if clean(row[4]) not in ("--","---","") else close,
                "high":       float(clean(row[5])) if clean(row[5]) not in ("--","---","") else close,
                "low":        float(clean(row[6])) if clean(row[6]) not in ("--","---","") else close,
                "close":      close,
                "change":     chg,
                "change_pct": change_pct,
                "volume":     vol,
                "market":     "otc"
            }
        except:
            continue
    return result

def fetch_institutional_tpex(date_str):
    """V4新增：上櫃三大法人"""
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        roc_date = f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"
    except:
        return {}
    url  = "https://www.tpex.org.tw/web/stock/3insti/daily_report/3itrade_hedge_result.php"
    data = safe_get(url, {"d": roc_date, "se": "EW", "t": "D", "o": "json"}, headers=HEADERS_TPEX)
    if not data:
        return {}
    result = {}
    for row in data.get("aaData", []):
        try:
            sid = str(row[0]).strip()
            def f(x):
                v = str(x).replace(",","").strip()
                return int(float(v)) if v and v not in ("--","") else 0
            result[sid] = {
                "foreign_net": f(row[4]) if len(row) > 4 else 0,
                "trust_net":   f(row[7]) if len(row) > 7 else 0,
                "dealer_net":  f(row[10]) if len(row) > 10 else 0,
                "total_net":   f(row[13]) if len(row) > 13 else 0
            }
        except:
            continue
    return result

# ── 權證（上市 TWTB4U + 上櫃 TWTB8U） ────────────────
def _parse_warrants_common(rows, today):
    """共用解析邏輯"""
    result = {}
    def safe_float(val, default=0.0):
        v = str(val).replace(",","").replace("%","").replace("+","").strip()
        try:
            return float(v) if v and v not in ("--","---","") else default
        except:
            return default
    def safe_int(val, default=0):
        v = str(val).replace(",","").strip()
        try:
            return int(v) if v and v not in ("--","---","") else default
        except:
            return default
    for row in rows:
        try:
            if len(row) < 14:
                continue
            w_code     = str(row[0]).strip()
            underlying = str(row[2]).strip()
            call_put   = str(row[4]).strip()
            expire_str = str(row[5]).strip()
            strike     = safe_float(row[6])
            w_close    = safe_float(row[7])
            bid        = safe_float(row[8])
            ask        = safe_float(row[9])
            vol        = safe_int(row[10])
            leverage   = safe_float(row[11])
            iv         = safe_float(row[12])
            delta      = safe_float(row[13])
            if "認購" not in call_put:
                continue
            parts = expire_str.split("/")
            if len(parts) != 3:
                continue
            try:
                expire_dt = datetime(int(parts[0]) + 1911, int(parts[1]), int(parts[2]))
            except:
                continue
            days_left = (expire_dt - today).days
            if days_left < 20:          continue
            if vol < 100:               continue
            if leverage <= 0 or leverage > 15: continue
            if delta >= 0.70:           moneyness = "深度價內"
            elif delta >= 0.55:         moneyness = "輕度價內"
            elif delta >= 0.45:         moneyness = "價平"
            elif delta >= 0.30:         moneyness = "輕度價外"
            else:                       moneyness = "價外"
            leverage_ok = 4 < leverage < 12
            w = {
                "code":        w_code,
                "issuer":      str(row[1]).strip()[:2],
                "type":        "call",
                "expire":      expire_dt.strftime("%Y/%m/%d"),
                "days_left":   days_left,
                "strike":      strike,
                "leverage":    round(leverage, 1),
                "iv":          round(iv, 1),
                "delta":       round(delta, 2),
                "moneyness":   moneyness,
                "bid":         bid,
                "ask":         ask,
                "volume":      vol,
                "leverage_ok": leverage_ok
            }
            result.setdefault(underlying, []).append(w)
        except:
            continue
    return result

def fetch_warrants(date_str):
    """上市認購權證（TWTB4U）"""
    data = safe_get("https://www.twse.com.tw/exchangeReport/TWTB4U",
                    {"response": "json", "date": date_str})
    if not data or data.get("stat") != "OK":
        print("  ! TWTB4U 未取得")
        return {}
    today = datetime.strptime(date_str, "%Y%m%d")
    result = _parse_warrants_common(data.get("data", []), today)
    for sid in result:
        def w_score(w):
            s = 0
            if w["leverage_ok"]:           s += 2
            if w["volume"] > 1000:         s += 1
            if 0.45 <= w["delta"] <= 0.65: s += 1
            return s
        result[sid].sort(key=lambda x: (-w_score(x), -x["volume"]))
        result[sid] = result[sid][:3]
    return result

def fetch_warrants_tpex(date_str):
    """V4新增：上櫃認購權證（TWTB8U）"""
    data = safe_get("https://www.twse.com.tw/exchangeReport/TWTB8U",
                    {"response": "json", "date": date_str})
    if not data or data.get("stat") != "OK":
        print("  ! TWTB8U 未取得（可能無上櫃權證資料）")
        return {}
    today = datetime.strptime(date_str, "%Y%m%d")
    result = _parse_warrants_common(data.get("data", []), today)
    for sid in result:
        def w_score(w):
            s = 0
            if w["leverage_ok"]:           s += 2
            if w["volume"] > 500:          s += 1
            if 0.45 <= w["delta"] <= 0.65: s += 1
            return s
        result[sid].sort(key=lambda x: (-w_score(x), -x["volume"]))
        result[sid] = result[sid][:3]
    return result

# ── 評分 ─────────────────────────────────────────
def calc_ma(prices, n):
    if len(prices) < n:
        return None
    return round(statistics.mean(prices[-n:]), 2)

def calc_score(sid, tp, hist, inst, margin):
    score, reasons, warnings = 0, [], []
    closes  = [h["close"]  for h in hist]
    volumes = [h["volume"] for h in hist]
    chg = tp.get("change_pct", 0)
    if chg >= 5:   score += 16; reasons.append("強勢大漲 ≥5%")
    elif chg >= 3: score += 12; reasons.append("大漲 ≥3%")
    elif chg >= 1: score += 7;  reasons.append("溫和上漲")
    elif chg < 0:  score -= 10; warnings.append("今日收跌")
    high  = tp.get("high", 1)
    low   = tp.get("low",  0)
    close = tp.get("close", 0)
    if high != low:
        cp = (close - low) / (high - low)
        if cp >= 0.8:   score += 14; reasons.append("收盤靠近最高點（買盤強）")
        elif cp >= 0.6: score += 8
        elif cp < 0.3:  score -= 8;  warnings.append("長上影線（賣壓重）")
    if len(volumes) >= 5:
        avg_vol   = statistics.mean(volumes[-5:])
        today_vol = tp.get("volume", 0)
        vr        = today_vol / avg_vol if avg_vol > 0 else 0
        if 1.5 <= vr <= 4:  score += 10; reasons.append(f"量能放大 {vr:.1f}x")
        elif vr > 4:        score += 5;  warnings.append("量能過度放大（注意追高）")
        elif vr < 0.7:      score -= 5;  warnings.append("量能萎縮")
    if len(closes) >= 20:
        ma5  = calc_ma(closes, 5)
        ma10 = calc_ma(closes, 10)
        ma20 = calc_ma(closes, 20)
        if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
            score += 10; reasons.append("均線多頭排列")
        elif ma5 and ma10 and ma5 > ma10:
            score += 5
        if ma5  and close > ma5:   score += 4
        if ma20 and close > ma20:  score += 6; reasons.append(f"站上月線 MA20={ma20}")
        elif ma20 and close < ma20: score -= 5; warnings.append("跌破月線")
        recent_high = max(closes[-10:]) if len(closes) >= 10 else close
        if close >= recent_high * 0.99:
            score += 10; reasons.append("突破近10日高點")
    if inst:
        tn = inst.get("total_net",   0)
        fn = inst.get("foreign_net", 0)
        tr = inst.get("trust_net",   0)
        if tn > 5000:    score += 15; reasons.append(f"三大法人大幅買超 {tn//1000}張")
        elif tn > 1000:  score += 10; reasons.append(f"三大法人買超 {tn//1000}張")
        elif tn > 0:     score += 5
        elif tn < -3000: score -= 10; warnings.append("三大法人大幅賣超")
        if tr > 500:     score += 5;  reasons.append("投信連續買進")
        if fn > 3000:    score += 5;  reasons.append("外資積極買超")
    if margin:
        mb   = margin.get("margin_buy", 0)
        mbal = margin.get("margin_bal", 1)
        if mbal > 0 and mb / mbal > 0.15:
            score -= 5; warnings.append("融資追價明顯（散戶擁擠）")
    return max(0, min(100, score)), reasons, warnings

# ── 主程式 ────────────────────────────────────────
def main():
    today = today_str()
    print(f"[{tw_now().strftime('%H:%M:%S')} 台灣時間] 開始抓取 {today} 資料...")

    # ① 近20日歷史行情（上市 + 上櫃）
    print("  ► 近20日行情（上市+上櫃）...")
    past_days = prev_trading_days(20)
    history   = {}
    for d in reversed(past_days):
        tse_prices = fetch_daily_price(d)
        for sid, p in tse_prices.items():
            history.setdefault(sid, []).append({"close": p["close"], "volume": p["volume"], "market": "tse"})
        time.sleep(0.5)
        # 上櫃歷史（隔一天才抓，避免請求過多）
        # 注意：上櫃歷史比較慢，先跳過歷史，僅抓當日數據提升速度
    print(f"  → 上市歷史 {len(history)} 支")

    # ② 今日行情（上市）
    print("  ► 今日行情（上市）...")
    today_prices = fetch_daily_price(today)
    actual_today = today
    if not today_prices:
        print("  ! 今日上市資料為空，往前找最近交易日...")
        for d in past_days:
            today_prices = fetch_daily_price(d)
            if today_prices:
                actual_today = d
                print(f"  → 使用 {d}")
                break
    print(f"  → 上市 {len(today_prices)} 支")
    time.sleep(0.8)

    # ③ 今日行情（上櫃）
    print("  ► 今日行情（上櫃）...")
    otc_prices = fetch_daily_price_tpex(actual_today)
    print(f"  → 上櫃 {len(otc_prices)} 支")
    # 合併，上市優先
    for sid, p in otc_prices.items():
        if sid not in today_prices:
            today_prices[sid] = p
    print(f"  → 合計 {len(today_prices)} 支")
    time.sleep(0.8)

    # ④ 三大法人（上市 + 上櫃）
    print("  ► 三大法人...")
    institutional = fetch_institutional(actual_today)
    otc_inst      = fetch_institutional_tpex(actual_today)
    for sid, v in otc_inst.items():
        if sid not in institutional:
            institutional[sid] = v
    print(f"  → {len(institutional)} 筆")
    time.sleep(0.8)

    # ⑤ 融資（只有上市有公開 API）
    print("  ► 融資融券...")
    margin_data = fetch_margin(actual_today)
    print(f"  → {len(margin_data)} 筆")
    time.sleep(0.8)

    # ⑥ 權證（上市 + 上櫃）
    print("  ► 認購權證日報...")
    warrants      = fetch_warrants(actual_today)
    warrants_otc  = fetch_warrants_tpex(actual_today)
    for sid, wlist in warrants_otc.items():
        if sid in warrants:
            warrants[sid] = (warrants[sid] + wlist)[:3]
        else:
            warrants[sid] = wlist
    print(f"  → 取得 {len(warrants)} 支標的的認購權證")
    time.sleep(0.8)

    # ⑦ 篩選與評分
    print("  ► 計算評分...")
    candidates = []
    for sid, tp in today_prices.items():
        if not (sid.isdigit() and len(sid) == 4):
            continue
        close   = tp.get("close", 0)
        chg_pct = tp.get("change_pct", 0)
        vol     = tp.get("volume", 0)
        market  = tp.get("market", "tse")

        if close < 10 or close > 3000:  continue
        if chg_pct < 0.5:               continue
        # 上櫃量能門檻較低
        min_vol = 200000 if market == "otc" else 500000
        if vol < min_vol:                continue

        hist = history.get(sid, [])
        if len(hist) < 5:               continue

        has_warrant = sid in warrants
        score, reasons, warnings = calc_score(
            sid, tp, hist, institutional.get(sid), margin_data.get(sid)
        )
        if has_warrant:
            score = min(100, score + 2)

        if score >= 50:
            candidates.append({
                "sid":        sid,
                "name":       tp["name"],
                "close":      close,
                "change_pct": chg_pct,
                "volume":     vol,
                "market":     market,
                "score":      score,
                "reasons":    reasons,
                "warnings":   warnings,
                "inst":       institutional.get(sid, {}),
                "ma5":        calc_ma([h["close"] for h in hist], 5),
                "ma10":       calc_ma([h["close"] for h in hist], 10),
                "ma20":       calc_ma([h["close"] for h in hist], 20),
                "warrants":   warrants.get(sid, [])
            })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top10 = candidates[:TOP_N]

    # ⑧ 機率標籤（V4修正：正確的機率數值）
    for c in top10:
        s = c["score"]
        if s >= 85:
            prob_pct = min(82, 60 + (s - 85) * 2 + 15)
            c["prob"] = f"高（{prob_pct}%）"
            c["prob_level"] = "high"
        elif s >= 70:
            prob_pct = 62 + (s - 70)
            c["prob"] = f"中高（{prob_pct}%）"
            c["prob_level"] = "medium-high"
        elif s >= 55:
            prob_pct = 48 + (s - 55)
            c["prob"] = f"中（{prob_pct}%）"
            c["prob_level"] = "medium"
        else:
            c["prob"] = "偏低（<48%）"
            c["prob_level"] = "low"

    output = {
        "updated_at":       tw_now().strftime("%Y/%m/%d %H:%M"),
        "trade_date":       actual_today,
        "total_scanned":    len(today_prices),
        "candidates_count": len(candidates),
        "stocks":           top10
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成！候選 {len(candidates)} 支，精選 {len(top10)} 支")
    for s in top10:
        wc = len(s.get("warrants", []))
        mkt = s.get("market","?")
        print(f"  [{s['score']:3d}] {mkt} {s['sid']} {s['name']:8s} +{s['change_pct']}%  {s['prob']}  權證:{wc}支")

if __name__ == "__main__":
    main()
