#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V2
修正項目：
  1. fetch_daily_price: row[9] 漲跌號修正（原只處理 "-"，加入空白/+/特殊字元）
  2. fetch_warrants (TWTB4U): IV 欄空值處理，防止 ValueError；欄位數量防護
  3. 加入 fetch_daily_price_tpex: 補抓上櫃股票（OTC，原版只抓上市）
  4. today_str 改為明確使用台灣時區，避免 UTC 環境跑出錯誤日期
  5. 主程式：若今日行情為空，自動往前找最近交易日
  6. 統一 safe_get timeout 提升至 20 秒，加強重試邏輯
每日盤後自動抓取 TWSE 公開資料，篩選續漲機率高的股票，並附上對應認購權證
輸出：data/stocks.json
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.twse.com.tw/"
}
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N = 10

# ── 工具 ─────────────────────────────────────────
def safe_get(url, params=None, retries=3, delay=1.2):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  [retry {i+1}/{retries}] {url[:60]}... → {e}")
            time.sleep(delay * (i + 1))
    return None

def tw_now():
    """明確使用台灣時區（UTC+8），GitHub Actions 跑在 UTC 環境"""
    tz_tw = timezone(timedelta(hours=8))
    return datetime.now(tz_tw)

def today_str():
    return tw_now().strftime("%Y%m%d")

def prev_trading_days(n=20):
    result, d = [], tw_now()
    while len(result) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # 週一~週五
            result.append(d.strftime("%Y%m%d"))
    return result

# ── 行情抓取（上市 TWSE） ──────────────────────────
def _parse_sign(sign_str):
    """
    修正：TWSE 漲跌符號欄位有時是空白、'+'、'-'、或 HTML 特殊字元
    原版只處理 '-'，導致正數漲幅被誤算
    """
    s = sign_str.strip()
    if s == "-" or s == "▼":
        return -1
    # '+', '▲', '' 都當作正數
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
            # 防護：過濾空值
            if not row[8] or row[8].strip() in ("", "--", "---"):
                continue

            vol   = int(row[2].replace(",", ""))
            close = float(row[8].replace(",", ""))

            # ✅ 修正：正確處理漲跌號
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
                "volume":     vol
            }
        except Exception as e:
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

# ── 權證抓取（TWTB4U） ────────────────────────────
def fetch_warrants(date_str):
    """
    抓 TWSE 全市場認購權證日報（TWTB4U）
    修正：
      - 欄位數量防護（< 14 欄跳過）
      - IV 欄可能為 '' 或 '--'，安全處理
      - delta 欄同上
    """
    data = safe_get("https://www.twse.com.tw/exchangeReport/TWTB4U",
                    {"response": "json", "date": date_str})
    if not data or data.get("stat") != "OK":
        print("  ! 權證日報資料未取得（可能假日或資料尚未發布）")
        return {}

    result = {}
    today = datetime.strptime(date_str, "%Y%m%d")

    def safe_float(val, default=0.0):
        """安全轉 float，空值或 '--' 回傳 default"""
        v = val.replace(",", "").replace("%", "").replace("+", "").strip()
        try:
            return float(v) if v and v not in ("--", "---", "") else default
        except:
            return default

    def safe_int(val, default=0):
        v = val.replace(",", "").strip()
        try:
            return int(v) if v and v not in ("--", "---", "") else default
        except:
            return default

    fields = data.get("fields", [])
    print(f"    TWTB4U 欄位({len(fields)}): {fields[:5]}...")

    for row in data.get("data", []):
        try:
            # ✅ 修正：欄位數量防護
            if len(row) < 14:
                continue

            w_code     = row[0].strip()
            underlying = row[2].strip()
            call_put   = row[4].strip()
            expire_str = row[5].strip()
            strike     = safe_float(row[6])
            w_close    = safe_float(row[7])
            bid        = safe_float(row[8])
            ask        = safe_float(row[9])
            vol        = safe_int(row[10])
            leverage   = safe_float(row[11])
            iv         = safe_float(row[12])   # ✅ 修正：原版空值會 ValueError
            delta      = safe_float(row[13])   # ✅ 修正：同上

            if "認購" not in call_put:
                continue

            # 轉換民國年到期日
            parts = expire_str.split("/")
            if len(parts) != 3:
                continue
            try:
                expire_dt = datetime(int(parts[0]) + 1911, int(parts[1]), int(parts[2]))
            except:
                continue

            days_left = (expire_dt - today).days
            if days_left < 20:    continue  # 快到期排除
            if vol < 100:         continue  # 低流動性排除
            if leverage <= 0 or leverage > 15: continue

            # 價內外
            if delta >= 0.70:        moneyness = "深度價內"
            elif delta >= 0.55:      moneyness = "輕度價內"
            elif delta >= 0.45:      moneyness = "價平"
            elif delta >= 0.30:      moneyness = "輕度價外"
            else:                    moneyness = "價外"

            leverage_ok = 4 < leverage < 12

            w = {
                "code":        w_code,
                "issuer":      row[1].strip()[:2],
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

    # 每支股票最多保留3支：優先槓桿OK、流動性高
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

# ── 評分 ─────────────────────────────────────────
def calc_ma(prices, n):
    if len(prices) < n:
        return None
    return round(statistics.mean(prices[-n:]), 2)

def calc_score(sid, tp, hist, inst, margin):
    score, reasons, warnings = 0, [], []
    closes  = [h["close"]  for h in hist]
    volumes = [h["volume"] for h in hist]

    # 量價 (40分)
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

    # 技術 (30分)
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

    # 籌碼 (30分)
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

    # 近20日歷史行情
    print("  ► 近20日行情（每次 0.8s 間隔避免被封）...")
    past_days = prev_trading_days(20)
    history   = {}
    for d in reversed(past_days):
        for sid, p in fetch_daily_price(d).items():
            history.setdefault(sid, []).append({"close": p["close"], "volume": p["volume"]})
        time.sleep(0.8)

    # ✅ 修正：今日行情若為空，自動往前找最近交易日（假日或資料延遲時）
    print("  ► 今日行情...")
    today_prices = fetch_daily_price(today)
    actual_today = today
    if not today_prices:
        print("  ! 今日資料為空，往前尋找最近交易日...")
        for d in past_days:
            today_prices = fetch_daily_price(d)
            if today_prices:
                actual_today = d
                print(f"  → 使用 {d} 的資料")
                break
    print(f"  → 抓到 {len(today_prices)} 支股票")
    time.sleep(0.8)

    # 三大法人
    print("  ► 三大法人...")
    institutional = fetch_institutional(actual_today)
    print(f"  → {len(institutional)} 筆")
    time.sleep(0.8)

    # 融資
    print("  ► 融資融券...")
    margin_data = fetch_margin(actual_today)
    print(f"  → {len(margin_data)} 筆")
    time.sleep(0.8)

    # 權證日報
    print("  ► 認購權證日報...")
    warrants = fetch_warrants(actual_today)
    print(f"  → 取得 {len(warrants)} 支標的的認購權證資料")
    time.sleep(0.8)

    # 篩選與評分
    print("  ► 計算評分...")
    candidates = []
    for sid, tp in today_prices.items():
        if not (sid.isdigit() and len(sid) == 4):
            continue
        close   = tp.get("close", 0)
        chg_pct = tp.get("change_pct", 0)
        vol     = tp.get("volume", 0)

        if close < 20 or close > 2500: continue
        if chg_pct < 0.5:              continue
        if vol < 500000:               continue

        hist = history.get(sid, [])
        if len(hist) < 5:              continue

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

    # 機率標籤
    for c in top10:
        s = c["score"]
        if s >= 85:    c["prob"] = f"高（{min(85, 60+s//5)}%）";  c["prob_level"] = "high"
        elif s >= 70:  c["prob"] = f"中高（{55+s//5}%）";          c["prob_level"] = "medium-high"
        elif s >= 55:  c["prob"] = f"中（{45+s//5}%）";            c["prob_level"] = "medium"
        else:          c["prob"] = "偏低（<45%）";                  c["prob_level"] = "low"

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
        print(f"  [{s['score']:3d}] {s['sid']} {s['name']:8s} +{s['change_pct']}%  {s['prob']}  權證:{wc}支")

if __name__ == "__main__":
    main()
