#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V5
資料來源改為 FinMind API（完全支援 GitHub Actions 境外 IP）
原 TWSE/MI_INDEX 境外 403 問題已修正。

資料來源：
  - 股價日K：FinMind TaiwanStockPrice（免費，需 data_id）
  - 三大法人：FinMind TaiwanStockInstitutionalInvestors（免費，需 data_id）
  - 融資融券：FinMind TaiwanStockMarginPurchaseShortSale（免費，需 data_id）
  - 認購權證：TWSE exchangeReport/TWTB4U + TWTB8U（GitHub Actions 可連線）
  - 盤中即時：mis.twse（盤中 Actions 可連線，境外 IP 非封鎖對象）

使用說明：
  無 token 可直接使用（300 req/hr 上限）。
  若要提高上限，至 finmindtrade.com 免費註冊後設定環境變數：
  export FINMIND_TOKEN=your_token_here
  或在 GitHub Actions Settings > Secrets 加入 FINMIND_TOKEN。
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone

TZ_TW = timezone(timedelta(hours=8))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N = 10

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.twse.com.tw/"
}

# ── 工具函式 ──────────────────────────────────────────
def tw_now():
    return datetime.now(TZ_TW)

def today_str():
    return tw_now().strftime("%Y-%m-%d")

def tw_date(s):
    """YYYYMMDD → YYYY-MM-DD"""
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s

def prev_trading_dates(n=22):
    """往前推 n 個自然日，過濾掉週末（FinMind 自動跳過假日）"""
    result, d = [], tw_now()
    while len(result) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            result.append(d.strftime("%Y-%m-%d"))
    return result  # 最新在前

def finmind_get(dataset, data_id, start_date, end_date=None, retries=3):
    params = {
        "dataset": dataset,
        "data_id": data_id,
        "start_date": start_date,
    }
    if end_date:
        params["end_date"] = end_date
    headers = {}
    if FINMIND_TOKEN:
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"

    for i in range(retries):
        try:
            r = requests.get(FINMIND_URL, params=params, headers=headers, timeout=20)
            if r.status_code == 402:
                print("  ! FinMind 超出使用上限（免費 300/hr），請稍後再試或設定 FINMIND_TOKEN")
                return []
            r.raise_for_status()
            d = r.json()
            if d.get("status") == 200:
                return d.get("data", [])
            print(f"  ! FinMind {dataset}/{data_id} status={d.get('status')} msg={d.get('msg','')}")
            return []
        except Exception as e:
            print(f"  [retry {i+1}/{retries}] FinMind {dataset}/{data_id} → {e}")
            time.sleep(1.5 * (i + 1))
    return []

def safe_float(v, default=0.0):
    try:
        return float(str(v).replace(",", "").strip())
    except:
        return default

def safe_int(v, default=0):
    try:
        return int(str(v).replace(",", "").strip())
    except:
        return default

# ── 股票清單 ───────────────────────────────────────────
def fetch_stock_list():
    """取得全部上市+上櫃股票清單（4碼純數字）"""
    headers = {"Authorization": f"Bearer {FINMIND_TOKEN}"} if FINMIND_TOKEN else {}
    try:
        r = requests.get(FINMIND_URL, params={"dataset": "TaiwanStockInfo"},
                         headers=headers, timeout=20)
        r.raise_for_status()
        d = r.json()
        stocks = {}
        for row in d.get("data", []):
            sid = str(row.get("stock_id", "")).strip()
            if not (sid.isdigit() and len(sid) == 4):
                continue
            t = str(row.get("type", "")).strip()
            # 只要上市(twse)和上櫃(tpex)，排除興櫃
            if t not in ("twse", "tpex"):
                continue
            stocks[sid] = {
                "name": str(row.get("stock_name", sid)).strip(),
                "market": "tse" if t == "twse" else "otc",
            }
        print(f"  → 股票清單：{len(stocks)} 支（上市+上櫃）")
        return stocks
    except Exception as e:
        print(f"  ! fetch_stock_list 失敗：{e}")
        return {}

# ── 個股股價（FinMind） ───────────────────────────────
def fetch_price_one(sid, start_date, end_date):
    """取得單支股票近期日K"""
    rows = finmind_get("TaiwanStockPrice", sid, start_date, end_date)
    result = []
    for row in rows:
        try:
            result.append({
                "date":   str(row["date"]),
                "open":   safe_float(row.get("open", 0)),
                "high":   safe_float(row.get("max", 0)),
                "low":    safe_float(row.get("min", 0)),
                "close":  safe_float(row.get("close", 0)),
                "volume": safe_int(row.get("Trading_Volume", 0)),
                "spread": safe_float(row.get("spread", 0)),
            })
        except:
            continue
    return sorted(result, key=lambda x: x["date"])

# ── 三大法人（FinMind） ──────────────────────────────
def fetch_institutional_one(sid, start_date, end_date):
    """取得單支股票三大法人買賣超（張）"""
    rows = finmind_get("TaiwanStockInstitutionalInvestors", sid, start_date, end_date)
    # 同一天有多筆（外資/投信/自營商分開），合併
    by_date = {}
    for row in rows:
        date = str(row.get("date", ""))
        name = str(row.get("name", ""))
        buy  = safe_int(row.get("buy", 0))
        sell = safe_int(row.get("sell", 0))
        net  = buy - sell
        if date not in by_date:
            by_date[date] = {"foreign_net": 0, "trust_net": 0, "dealer_net": 0}
        if "外資" in name:
            by_date[date]["foreign_net"] += net
        elif "投信" in name:
            by_date[date]["trust_net"] += net
        elif "自營" in name:
            by_date[date]["dealer_net"] += net
    # 換算：FinMind 單位是「股」，除以 1000 轉為「張」
    result = {}
    for date, v in by_date.items():
        result[date] = {
            "foreign_net": v["foreign_net"] // 1000,
            "trust_net":   v["trust_net"]   // 1000,
            "dealer_net":  v["dealer_net"]  // 1000,
            "total_net":   (v["foreign_net"] + v["trust_net"] + v["dealer_net"]) // 1000,
        }
    return result  # {date: {...}}

# ── 融資融券（FinMind） ──────────────────────────────
def fetch_margin_one(sid, start_date, end_date):
    """取得融資融券（只取最後一天）"""
    rows = finmind_get("TaiwanStockMarginPurchaseShortSale", sid, start_date, end_date)
    if not rows:
        return {}
    # 取最新一天
    latest = sorted(rows, key=lambda x: x.get("date", ""))[-1]
    return {
        "margin_buy": safe_int(latest.get("MarginPurchaseBuy", 0)),
        "margin_bal": safe_int(latest.get("MarginPurchaseRemainAmount", 1)) or 1,
        "short_sell": safe_int(latest.get("ShortSaleSell", 0)),
    }

# ── 認購權證（TWSE，GitHub Actions 可連線） ───────────
def safe_get_twse(url, params=None, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  [retry {i+1}/{retries}] {url[:60]}... → {e}")
            time.sleep(1.5 * (i + 1))
    return None

def _parse_warrants_common(rows, today):
    result = {}
    def sf(val, default=0.0):
        v = str(val).replace(",","").replace("%","").replace("+","").strip()
        try: return float(v) if v and v not in ("--","---","") else default
        except: return default
    def si(val, default=0):
        v = str(val).replace(",","").strip()
        try: return int(v) if v and v not in ("--","---","") else default
        except: return default

    for row in rows:
        try:
            if len(row) < 14: continue
            w_code     = str(row[0]).strip()
            underlying = str(row[2]).strip()
            call_put   = str(row[4]).strip()
            expire_str = str(row[5]).strip()
            strike     = sf(row[6])
            bid        = sf(row[8])
            ask        = sf(row[9])
            vol        = si(row[10])
            leverage   = sf(row[11])
            iv         = sf(row[12])
            delta      = sf(row[13])

            if "認購" not in call_put: continue
            parts = expire_str.split("/")
            if len(parts) != 3: continue
            try:
                expire_dt = datetime(int(parts[0]) + 1911, int(parts[1]), int(parts[2]))
            except: continue
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
            result.setdefault(underlying, []).append({
                "code":       w_code,
                "issuer":     str(row[1]).strip()[:2],
                "type":       "call",
                "expire":     expire_dt.strftime("%Y/%m/%d"),
                "days_left":  days_left,
                "strike":     strike,
                "leverage":   round(leverage, 1),
                "iv":         round(iv, 1),
                "delta":      round(delta, 2),
                "moneyness":  moneyness,
                "bid":        bid,
                "ask":        ask,
                "volume":     vol,
                "leverage_ok": leverage_ok,
            })
        except: continue
    return result

def fetch_warrants(date_str_yyyymmdd):
    warrants = {}
    today = datetime.strptime(date_str_yyyymmdd, "%Y%m%d")

    for endpoint, label in [("TWTB4U","上市"), ("TWTB8U","上櫃")]:
        data = safe_get_twse(f"https://www.twse.com.tw/exchangeReport/{endpoint}",
                             {"response": "json", "date": date_str_yyyymmdd})
        if not data or data.get("stat") != "OK":
            print(f"  ! {endpoint}({label}) 未取得")
            continue
        parsed = _parse_warrants_common(data.get("data", []), today)

        def w_score(w):
            s = 0
            if w["leverage_ok"]:           s += 2
            if w["volume"] > 1000:         s += 1
            if 0.45 <= w["delta"] <= 0.65: s += 1
            return s

        for sid, wlist in parsed.items():
            wlist.sort(key=lambda x: (-w_score(x), -x["volume"]))
            if sid in warrants:
                warrants[sid] = (warrants[sid] + wlist[:3])[:3]
            else:
                warrants[sid] = wlist[:3]
        print(f"  → {endpoint}({label}) 取得 {len(parsed)} 支標的")
        time.sleep(0.5)

    return warrants

# ── 評分 ──────────────────────────────────────────────
def calc_ma(closes, n):
    if len(closes) < n: return None
    return round(statistics.mean(closes[-n:]), 2)

def calc_score(sid, today_price, hist, inst_today, margin):
    score, reasons, warnings = 0, [], []
    closes  = [h["close"]  for h in hist]
    volumes = [h["volume"] for h in hist]

    close  = today_price["close"]
    high   = today_price["high"]
    low    = today_price["low"]
    spread = today_price.get("spread", 0)

    # 漲跌幅（用 spread 計算，spread = close - prev_close）
    prev_close = close - spread if spread != 0 else (closes[-2] if len(closes) >= 2 else close)
    chg_pct = round(spread / prev_close * 100, 2) if prev_close != 0 else 0

    if chg_pct >= 5:   score += 16; reasons.append("強勢大漲 ≥5%")
    elif chg_pct >= 3: score += 12; reasons.append("大漲 ≥3%")
    elif chg_pct >= 1: score += 7;  reasons.append("溫和上漲")
    elif chg_pct < 0:  score -= 10; warnings.append("今日收跌")

    if high != low:
        cp = (close - low) / (high - low)
        if cp >= 0.8:   score += 14; reasons.append("收盤靠近最高點（買盤強）")
        elif cp >= 0.6: score += 8
        elif cp < 0.3:  score -= 8;  warnings.append("長上影線（賣壓重）")

    if len(volumes) >= 5:
        avg_vol = statistics.mean(volumes[-5:])
        today_vol = today_price["volume"]
        vr = today_vol / avg_vol if avg_vol > 0 else 0
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

    if inst_today:
        tn = inst_today.get("total_net", 0)
        fn = inst_today.get("foreign_net", 0)
        tr = inst_today.get("trust_net", 0)
        if tn > 5000:    score += 15; reasons.append(f"三大法人大幅買超 {tn//1000}千張")
        elif tn > 1000:  score += 10; reasons.append(f"三大法人買超 {tn}張")
        elif tn > 0:     score += 5
        elif tn < -3000: score -= 10; warnings.append("三大法人大幅賣超")
        if tr > 500:     score += 5;  reasons.append("投信積極買超")
        if fn > 3000:    score += 5;  reasons.append("外資積極買超")

    if margin:
        mb   = margin.get("margin_buy", 0)
        mbal = margin.get("margin_bal", 1)
        if mbal > 0 and mb / mbal > 0.15:
            score -= 5; warnings.append("融資追價明顯（散戶擁擠）")

    return max(0, min(100, score)), reasons, warnings, chg_pct

# ── 主程式 ────────────────────────────────────────────
def main():
    now = tw_now()
    today  = now.strftime("%Y-%m-%d")
    today8 = now.strftime("%Y%m%d")
    print(f"[{now.strftime('%H:%M:%S')} 台灣時間] V5 開始抓取 {today} 資料...")
    if FINMIND_TOKEN:
        print("  → 使用 FinMind token（600 req/hr 上限）")
    else:
        print("  → 無 token，使用匿名（300 req/hr 上限）")

    # ① 股票清單
    print("  ► 取得股票清單...")
    stock_info = fetch_stock_list()
    if not stock_info:
        print("  ! 無法取得股票清單，中止")
        return
    time.sleep(0.5)

    # ② 近 22 個交易日的日期範圍
    past = prev_trading_dates(22)
    start_date = past[-1]   # 最早
    end_date   = today      # 今天

    # ③ 認購權證（先抓，不受 IP 限制）
    print("  ► 認購權證...")
    warrants = fetch_warrants(today8)
    print(f"  → 共 {len(warrants)} 支標的有認購權證")
    time.sleep(0.5)

    # ④ 批次抓個股資料（只抓「有認購權證」的標的，縮小請求量）
    # 若有認購權證的不夠，補抓全市場高市值股
    candidate_sids = list(warrants.keys())
    # 保留 4 碼純數字且在 stock_info 內的
    candidate_sids = [s for s in candidate_sids if s in stock_info]
    print(f"  → 有認購權證的候選股：{len(candidate_sids)} 支")

    all_candidates = []
    total_req = 0
    per_stock_req = 3  # price + inst + margin

    for idx, sid in enumerate(candidate_sids):
        info = stock_info[sid]
        name = info["name"]
        market = info["market"]

        # 進度顯示
        if (idx + 1) % 20 == 0:
            print(f"  ... 已處理 {idx+1}/{len(candidate_sids)} 支")

        # 個股股價
        price_hist = fetch_price_one(sid, start_date, end_date)
        total_req += 1
        time.sleep(0.2)

        if not price_hist:
            continue

        today_price = price_hist[-1]
        # 確認是否為今日（或最近交易日）資料
        if today_price["date"] < past[0]:  # 資料太舊（超過1個交易日前）
            continue

        close   = today_price["close"]
        high    = today_price["high"]
        low_p   = today_price["low"]
        volume  = today_price["volume"]

        if close < 10 or close > 5000: continue
        min_vol = 200000 if market == "otc" else 500000
        if volume < min_vol:           continue

        hist = price_hist[:-1]  # 不含今日的歷史
        if len(hist) < 5:       continue

        # 三大法人
        inst_data = fetch_institutional_one(sid, start_date, end_date)
        total_req += 1
        time.sleep(0.2)
        inst_today = inst_data.get(today_price["date"], {})

        # 融資融券
        margin = fetch_margin_one(sid, start_date, end_date)
        total_req += 1
        time.sleep(0.2)

        # 評分
        score, reasons, warnings, chg_pct = calc_score(
            sid, today_price, hist, inst_today, margin
        )

        # 有認購權證加分
        if sid in warrants:
            score = min(100, score + 2)

        if score >= 50 and chg_pct >= 0.5:
            ma_closes = [h["close"] for h in hist]
            all_candidates.append({
                "sid":        sid,
                "name":       name,
                "close":      close,
                "change_pct": chg_pct,
                "volume":     volume,
                "market":     market,
                "score":      score,
                "reasons":    reasons,
                "warnings":   warnings,
                "inst":       inst_today,
                "ma5":        calc_ma(ma_closes, 5),
                "ma10":       calc_ma(ma_closes, 10),
                "ma20":       calc_ma(ma_closes, 20),
                "warrants":   warrants.get(sid, []),
            })

        # 安全限速
        if total_req >= 280:
            print("  ! 接近 API 限制（280 req），停止掃描")
            break

    all_candidates.sort(key=lambda x: x["score"], reverse=True)
    top10 = all_candidates[:TOP_N]

    # ⑤ 機率標籤
    for c in top10:
        s = c["score"]
        if s >= 85:
            prob_pct = min(82, 60 + (s - 85) * 2 + 15)
            c["prob"] = f"高（{prob_pct}%）"; c["prob_level"] = "high"
        elif s >= 70:
            prob_pct = 62 + (s - 70)
            c["prob"] = f"中高（{prob_pct}%）"; c["prob_level"] = "medium-high"
        elif s >= 55:
            prob_pct = 48 + (s - 55)
            c["prob"] = f"中（{prob_pct}%）"; c["prob_level"] = "medium"
        else:
            c["prob"] = "偏低（<48%）"; c["prob_level"] = "low"

    output = {
        "updated_at":       tw_now().strftime("%Y/%m/%d %H:%M"),
        "trade_date":       today8,
        "total_scanned":    len(candidate_sids),
        "candidates_count": len(all_candidates),
        "stocks":           top10,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成！掃描 {len(candidate_sids)} 支，候選 {len(all_candidates)} 支，精選 {len(top10)} 支")
    print(f"   總 API 請求：{total_req} 次")
    for s in top10:
        wc = len(s.get("warrants", []))
        print(f"  [{s['score']:3d}] {s['market']} {s['sid']} {s['name']:8s} +{s['change_pct']}%  {s['prob']}  權證:{wc}支")

if __name__ == "__main__":
    main()
