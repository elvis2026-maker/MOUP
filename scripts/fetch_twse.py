#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V8
=========================
V8 修正：
  Bug1 修正：TWSE TWTB4U 境外 IP 403 問題
    → 改用 FinMind TaiwanStockWarrant（完全支援境外 IP）取代 TWSE 直連
    → 同時保留 TWSE 直連作為備用（境內 runner 可用）
  Bug2 修正：FinMind 無 token 300/hr 上限
    → 先用 TaiwanStockPrice 批次取全市場當日行情，1次請求取代 N 次
    → 量價過濾後再個別查法人（大幅減少請求數）
  Bug3 修正：V7保護機制設計缺陷
    → 移除「0支結果才保護」邏輯，改為「若 API 完全失敗才保護」

資料來源：
  FinMind TaiwanStockPrice         → 全市場當日股價（1次請求）
  FinMind TaiwanStockInstitutionalInvestors → 個股三大法人（篩選後）
  FinMind TaiwanStockMarginPurchaseShortSale → 個股融資（篩選後）
  FinMind TaiwanStockWarrant        → 認購權證（主要）
  TWSE TWTB4U/TWTB8U              → 認購權證（備用，境內IP）
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone

TZ_TW       = timezone(timedelta(hours=8))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N       = 10

FINMIND_URL   = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer":    "https://www.twse.com.tw/"
}

# ── 工具 ─────────────────────────────────────────────
def tw_now():
    return datetime.now(TZ_TW)

def today_str():
    return tw_now().strftime("%Y-%m-%d")

def today_str8():
    return tw_now().strftime("%Y%m%d")

def prev_trading_dates(n=22):
    result, d = [], tw_now()
    while len(result) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            result.append(d.strftime("%Y-%m-%d"))
    return result  # 最新在前

def safe_float(v, default=0.0):
    try:    return float(str(v).replace(",","").strip())
    except: return default

def safe_int(v, default=0):
    try:    return int(str(v).replace(",","").strip())
    except: return default

# ── FinMind 共用請求 ──────────────────────────────────
def finmind_get(dataset, data_id=None, start_date=None, end_date=None, retries=3):
    params = {"dataset": dataset}
    if data_id:    params["data_id"]    = data_id
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"]   = end_date
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN

    for i in range(retries):
        try:
            r = requests.get(FINMIND_URL, params=params, timeout=30)
            if r.status_code == 402:
                print("  ! FinMind 超出 API 上限，請至 finmindtrade.com 免費申請 token")
                return []
            r.raise_for_status()
            d = r.json()
            if d.get("status") == 200:
                return d.get("data", [])
            print(f"  ! FinMind {dataset} status={d.get('status')} msg={d.get('msg','')}")
            return []
        except Exception as e:
            print(f"  [retry {i+1}/{retries}] FinMind {dataset} → {e}")
            time.sleep(2 * (i + 1))
    return []

# ── V8 Bug1 修正：認購權證 ────────────────────────────
def fetch_warrants_finmind(today_date):
    """
    主要方式：FinMind TaiwanStockWarrant（境外 IP 100% 可用）
    回傳 {sid: [warrant,...]}
    """
    today = datetime.strptime(today_date, "%Y-%m-%d")
    # 抓近3天，確保今日資料
    start = (today - timedelta(days=3)).strftime("%Y-%m-%d")
    rows  = finmind_get("TaiwanStockWarrant", start_date=start, end_date=today_date)
    if not rows:
        return {}

    result = {}
    for row in rows:
        try:
            # 只要今日或最近交易日資料
            row_date = str(row.get("date",""))
            if row_date < start:
                continue

            call_put = str(row.get("PutCall","")).strip()
            if "C" not in call_put and "認購" not in call_put:
                continue

            sid        = str(row.get("underlying_stock","")).strip()
            w_code     = str(row.get("stock_id","")).strip()
            expire_str = str(row.get("ExpirationDate","")).strip()  # YYYY-MM-DD
            leverage   = safe_float(row.get("EffectiveLeverage", 0))
            iv         = safe_float(row.get("ImpliedVolatility", 0))
            delta      = safe_float(row.get("Delta", 0))
            bid        = safe_float(row.get("BidPrice", 0))
            ask        = safe_float(row.get("AskPrice", 0))
            vol        = safe_int(row.get("TradingVolume", 0))

            if not sid or not w_code or not expire_str:
                continue

            try:
                expire_dt = datetime.strptime(expire_str[:10], "%Y-%m-%d")
            except:
                continue

            days_left = (expire_dt - today).days
            if days_left < 20:             continue
            if vol < 100:                  continue
            if leverage <= 0 or leverage > 15: continue

            if delta >= 0.70:     moneyness = "深度價內"
            elif delta >= 0.55:   moneyness = "輕度價內"
            elif delta >= 0.45:   moneyness = "價平"
            elif delta >= 0.30:   moneyness = "輕度價外"
            else:                 moneyness = "價外"

            result.setdefault(sid, []).append({
                "code":        w_code,
                "issuer":      str(row.get("Issuer",""))[:3],
                "type":        "call",
                "expire":      expire_dt.strftime("%Y/%m/%d"),
                "days_left":   days_left,
                "leverage":    round(leverage, 1),
                "iv":          round(iv, 1),
                "delta":       round(delta, 2),
                "moneyness":   moneyness,
                "bid":         bid,
                "ask":         ask,
                "volume":      vol,
                "leverage_ok": 4 < leverage < 12,
            })
        except:
            continue

    def w_score(w):
        s = 0
        if w["leverage_ok"]:           s += 2
        if w["volume"] > 1000:         s += 1
        if 0.45 <= w["delta"] <= 0.65: s += 1
        return s

    for sid in result:
        result[sid].sort(key=lambda x: (-w_score(x), -x["volume"]))
        result[sid] = result[sid][:3]

    return result

def fetch_warrants_twse_fallback(date8):
    """
    備用：TWSE TWTB4U/TWTB8U（境內 IP 用）
    """
    result = {}
    today  = datetime.strptime(date8, "%Y%m%d")

    for ep in ["TWTB4U", "TWTB8U"]:
        try:
            r = requests.get(
                f"https://www.twse.com.tw/exchangeReport/{ep}",
                params={"response":"json","date":date8},
                headers=HEADERS, timeout=20
            )
            r.raise_for_status()
            d = r.json()
            if d.get("stat") != "OK":
                continue
            for row in d.get("data", []):
                try:
                    if len(row) < 14: continue
                    call_put = str(row[4]).strip()
                    if "認購" not in call_put: continue
                    sid      = str(row[2]).strip()
                    parts    = str(row[5]).split("/")
                    if len(parts) != 3: continue
                    expire_dt = datetime(int(parts[0])+1911, int(parts[1]), int(parts[2]))
                    days_left = (expire_dt - today).days
                    if days_left < 20: continue
                    vol      = safe_int(row[10])
                    leverage = safe_float(row[11])
                    iv       = safe_float(row[12])
                    delta    = safe_float(row[13])
                    if vol < 100 or leverage <= 0 or leverage > 15: continue
                    if delta >= 0.70:   mn = "深度價內"
                    elif delta >= 0.55: mn = "輕度價內"
                    elif delta >= 0.45: mn = "價平"
                    elif delta >= 0.30: mn = "輕度價外"
                    else:               mn = "價外"
                    result.setdefault(sid, []).append({
                        "code": str(row[0]).strip(),
                        "issuer": str(row[1]).strip()[:2],
                        "type": "call",
                        "expire": expire_dt.strftime("%Y/%m/%d"),
                        "days_left": days_left,
                        "leverage": round(leverage,1),
                        "iv": round(iv,1), "delta": round(delta,2),
                        "moneyness": mn,
                        "bid": safe_float(row[8]), "ask": safe_float(row[9]),
                        "volume": vol, "leverage_ok": 4 < leverage < 12
                    })
                except: continue
            time.sleep(0.5)
        except Exception as e:
            print(f"  ! TWSE {ep} → {e}")

    def w_score(w):
        s = 0
        if w["leverage_ok"]: s += 2
        if w["volume"] > 1000: s += 1
        if 0.45 <= w["delta"] <= 0.65: s += 1
        return s
    for sid in result:
        result[sid].sort(key=lambda x: (-w_score(x), -x["volume"]))
        result[sid] = result[sid][:3]
    return result

# ── V8 Bug2 修正：全市場當日行情（1次請求）────────────
def fetch_all_prices_today(today_date):
    """
    FinMind TaiwanStockPrice：1次請求取得全市場今日行情
    回傳 {sid: {close,open,high,low,volume,spread}}
    """
    rows = finmind_get("TaiwanStockPrice", start_date=today_date, end_date=today_date)
    result = {}
    for row in rows:
        try:
            sid = str(row.get("stock_id","")).strip()
            if not (sid.isdigit() and len(sid) == 4): continue
            result[sid] = {
                "close":  safe_float(row.get("close",0)),
                "open":   safe_float(row.get("open",0)),
                "high":   safe_float(row.get("max",0)),
                "low":    safe_float(row.get("min",0)),
                "volume": safe_int(row.get("Trading_Volume",0)),
                "spread": safe_float(row.get("spread",0)),
            }
        except: continue
    print(f"  → 全市場行情：{len(result)} 支")
    return result

def fetch_price_history(sid, start_date, end_date):
    """取得單支股票近 22 日歷史（不含今日）"""
    rows = finmind_get("TaiwanStockPrice", sid, start_date, end_date)
    result = []
    for row in rows:
        try:
            result.append({
                "date":   str(row["date"]),
                "close":  safe_float(row.get("close",0)),
                "volume": safe_int(row.get("Trading_Volume",0)),
            })
        except: continue
    return sorted(result, key=lambda x: x["date"])

def fetch_institutional_one(sid, start_date, end_date):
    rows = finmind_get("TaiwanStockInstitutionalInvestors", sid, start_date, end_date)
    by_date = {}
    for row in rows:
        date = str(row.get("date",""))
        name = str(row.get("name",""))
        net  = safe_int(row.get("buy",0)) - safe_int(row.get("sell",0))
        if date not in by_date:
            by_date[date] = {"foreign_net":0,"trust_net":0,"dealer_net":0}
        if "外資" in name:  by_date[date]["foreign_net"] += net
        elif "投信" in name: by_date[date]["trust_net"]  += net
        elif "自營" in name: by_date[date]["dealer_net"] += net
    result = {}
    for date, v in by_date.items():
        result[date] = {
            "foreign_net": v["foreign_net"]//1000,
            "trust_net":   v["trust_net"]//1000,
            "dealer_net":  v["dealer_net"]//1000,
            "total_net":   (v["foreign_net"]+v["trust_net"]+v["dealer_net"])//1000,
        }
    return result

def fetch_margin_one(sid, start_date, end_date):
    rows = finmind_get("TaiwanStockMarginPurchaseShortSale", sid, start_date, end_date)
    if not rows: return {}
    latest = sorted(rows, key=lambda x: x.get("date",""))[-1]
    return {
        "margin_buy": safe_int(latest.get("MarginPurchaseBuy",0)),
        "margin_bal": safe_int(latest.get("MarginPurchaseRemainAmount",1)) or 1,
    }

# ── 評分 ─────────────────────────────────────────────
def calc_ma(closes, n):
    if len(closes) < n: return None
    return round(statistics.mean(closes[-n:]), 2)

def calc_score(today_p, hist_closes, hist_vols, inst, margin):
    score, reasons, warnings = 0, [], []

    close  = today_p["close"]
    high   = today_p["high"]
    low    = today_p["low"]
    spread = today_p.get("spread", 0)
    prev_close = close - spread if spread != 0 else (hist_closes[-1] if hist_closes else close)
    chg_pct = round(spread / prev_close * 100, 2) if prev_close != 0 else 0

    # 量價 (40分)
    if chg_pct >= 5:    score += 16; reasons.append("強勢大漲 ≥5%")
    elif chg_pct >= 3:  score += 12; reasons.append("大漲 ≥3%")
    elif chg_pct >= 1:  score += 7;  reasons.append("溫和上漲")
    elif chg_pct < 0:   score -= 10; warnings.append("今日收跌")

    if high != low:
        cp = (close - low) / (high - low)
        if cp >= 0.8:    score += 14; reasons.append("收盤靠近最高點（買盤強）")
        elif cp >= 0.6:  score += 8
        elif cp < 0.3:   score -= 8;  warnings.append("長上影線（賣壓重）")

    if len(hist_vols) >= 5:
        avg_vol = statistics.mean(hist_vols[-5:])
        vr = today_p["volume"] / avg_vol if avg_vol > 0 else 0
        if 1.5 <= vr <= 4:  score += 10; reasons.append(f"量能放大 {vr:.1f}x")
        elif vr > 4:        score += 5;  warnings.append("量能過度放大（注意追高）")
        elif vr < 0.7:      score -= 5;  warnings.append("量能萎縮")

    # 技術 (30分)
    if len(hist_closes) >= 20:
        ma5  = calc_ma(hist_closes, 5)
        ma10 = calc_ma(hist_closes, 10)
        ma20 = calc_ma(hist_closes, 20)
        if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
            score += 10; reasons.append("均線多頭排列")
        elif ma5 and ma10 and ma5 > ma10:
            score += 5
        if ma5  and close > ma5:  score += 4
        if ma20 and close > ma20: score += 6; reasons.append(f"站上月線 MA20={ma20}")
        elif ma20 and close < ma20: score -= 5; warnings.append("跌破月線")
        if hist_closes:
            recent_high = max(hist_closes[-10:]) if len(hist_closes) >= 10 else hist_closes[-1]
            if close >= recent_high * 0.99:
                score += 10; reasons.append("突破近10日高點")

    # 籌碼 (30分)
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

    if margin:
        mb, mbal = margin.get("margin_buy",0), margin.get("margin_bal",1)
        if mbal > 0 and mb/mbal > 0.15:
            score -= 5; warnings.append("融資追價明顯（散戶擁擠）")

    return max(0, min(100, score)), reasons, warnings, chg_pct

# ── 股票基本資料 ──────────────────────────────────────
def fetch_stock_info():
    rows = finmind_get("TaiwanStockInfo")
    result = {}
    for row in rows:
        sid = str(row.get("stock_id","")).strip()
        if not (sid.isdigit() and len(sid) == 4): continue
        t = str(row.get("type","")).strip()
        if t not in ("twse","tpex"): continue
        result[sid] = {
            "name":   str(row.get("stock_name",sid)).strip(),
            "market": "tse" if t == "twse" else "otc",
        }
    return result

# ── 主程式 ────────────────────────────────────────────
def main():
    now    = tw_now()
    today  = now.strftime("%Y-%m-%d")
    today8 = now.strftime("%Y%m%d")
    print(f"[{now.strftime('%H:%M:%S')} 台灣時間] fetch_twse V8 開始 {today}")
    print(f"  FinMind token: {'已設定' if FINMIND_TOKEN else '未設定（匿名 300req/hr）'}")

    api_success = False  # 追蹤是否有任何 API 成功

    # ① 股票基本資料
    print("  ► 取得股票清單...")
    stock_info = fetch_stock_info()
    if not stock_info:
        print("  ! 股票清單失敗，中止")
        return
    api_success = True
    time.sleep(0.5)

    # ② V8核心修正：1次請求取全市場今日行情（大幅節省 API 請求數）
    print("  ► 全市場今日行情（1次請求）...")
    all_today = fetch_all_prices_today(today)
    if not all_today:
        # 嘗試前一個交易日
        past = prev_trading_dates(5)
        for d in past:
            print(f"  ! 改抓 {d}...")
            all_today = fetch_all_prices_today(d)
            if all_today:
                today = d
                today8 = d.replace("-","")
                break
    if not all_today:
        print("  ! 行情資料取得失敗，中止")
        return
    time.sleep(0.5)

    # ③ 認購權證（主要：FinMind，備用：TWSE）
    print("  ► 認購權證（FinMind）...")
    warrants = fetch_warrants_finmind(today)
    if not warrants:
        print("  ! FinMind 無權證資料，改用 TWSE 直連...")
        warrants = fetch_warrants_twse_fallback(today8)
    print(f"  → 共 {len(warrants)} 支標的有認購權證")
    time.sleep(0.5)

    # ④ 第一輪篩選：用今日行情做量價過濾（不消耗額外 API）
    print("  ► 量價預篩選...")
    pre_candidates = []
    for sid, tp in all_today.items():
        info = stock_info.get(sid)
        if not info: continue
        close  = tp["close"]
        volume = tp["volume"]
        spread = tp.get("spread", 0)
        prev   = close - spread if spread != 0 else close
        chg_pct = round(spread / prev * 100, 2) if prev != 0 else 0

        if close < 10 or close > 5000:  continue
        min_vol = 200000 if info["market"]=="otc" else 500000
        if volume < min_vol:             continue
        if chg_pct < 0.5:               continue  # 今日要上漲

        pre_candidates.append(sid)

    print(f"  → 量價預篩選後：{len(pre_candidates)} 支候選")
    # 優先有認購權證的排前面
    pre_candidates.sort(key=lambda s: (0 if s in warrants else 1, s))

    # ⑤ 近22日歷史（用於技術指標）
    past  = prev_trading_dates(22)
    start_date = past[-1]   # 22天前

    # ⑥ 逐支抓技術+籌碼（量控制在 200 req 內）
    print(f"  ► 個股分析（最多掃 {min(len(pre_candidates),60)} 支）...")
    all_candidates = []
    total_req = 2  # 已用：stock_info + all_today

    for idx, sid in enumerate(pre_candidates[:60]):  # 最多60支
        info   = stock_info[sid]
        tp     = all_today[sid]
        close  = tp["close"]
        volume = tp["volume"]

        if (idx+1) % 10 == 0:
            print(f"  ... {idx+1}/{min(len(pre_candidates),60)}")

        # 個股歷史股價
        hist_rows = fetch_price_history(sid, start_date, today)
        total_req += 1
        time.sleep(0.25)

        # 排除今日（今日已在 all_today），取歷史
        hist_rows = [h for h in hist_rows if h["date"] < today]
        if len(hist_rows) < 5: continue

        hist_closes = [h["close"] for h in hist_rows]
        hist_vols   = [h["volume"] for h in hist_rows]

        # 三大法人
        inst_data  = fetch_institutional_one(sid, start_date, today)
        inst_today = inst_data.get(today, {})
        total_req += 1
        time.sleep(0.25)

        # 融資（選做，節省 req）
        margin = {}
        if total_req < 180:
            margin = fetch_margin_one(sid, start_date, today)
            total_req += 1
            time.sleep(0.2)

        score, reasons, warnings, chg_pct = calc_score(
            tp, hist_closes, hist_vols, inst_today, margin
        )
        if sid in warrants: score = min(100, score + 2)

        if score >= 50:
            all_candidates.append({
                "sid":        sid,
                "name":       info["name"],
                "close":      close,
                "change_pct": chg_pct,
                "volume":     volume,
                "market":     info["market"],
                "score":      score,
                "reasons":    reasons,
                "warnings":   warnings,
                "inst":       inst_today,
                "ma5":        calc_ma(hist_closes, 5),
                "ma10":       calc_ma(hist_closes, 10),
                "ma20":       calc_ma(hist_closes, 20),
                "warrants":   warrants.get(sid, []),
            })

        if total_req >= 280:
            print(f"  ! 接近 API 上限 ({total_req} req)，停止掃描")
            break

    all_candidates.sort(key=lambda x: x["score"], reverse=True)
    top10 = all_candidates[:TOP_N]

    # ⑦ V8 Bug3修正：只有在 API 完全沒回任何資料才保護
    if len(all_candidates) == 0 and not api_success:
        print("  ⚠ V8保護：API 完全失敗，保留上次資料")
        return
    if len(all_candidates) == 0:
        print("  ⚠ 本日無符合條件股票（可能休市或條件過嚴），寫入空清單")

    # ⑧ 機率標籤
    for c in top10:
        s = c["score"]
        if s >= 85:
            c["prob"] = f"高（{min(82,60+(s-85)*2+15)}%）"; c["prob_level"] = "high"
        elif s >= 70:
            c["prob"] = f"中高（{62+(s-70)}%）";            c["prob_level"] = "medium-high"
        elif s >= 55:
            c["prob"] = f"中（{48+(s-55)}%）";              c["prob_level"] = "medium"
        else:
            c["prob"] = "偏低（<48%）";                      c["prob_level"] = "low"

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

    print(f"\n✅ 完成！預篩 {len(pre_candidates)} 支，分析 {idx+1} 支，候選 {len(all_candidates)} 支，精選 {len(top10)} 支")
    print(f"   總 API 請求：{total_req} 次")
    for s in top10:
        wc = len(s.get("warrants",[]))
        print(f"  [{s['score']:3d}] {s['sid']} {s['name']:8s} +{s['change_pct']}%  {s['prob']}  權證:{wc}支")

if __name__ == "__main__":
    main()
