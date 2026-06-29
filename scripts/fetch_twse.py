#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V9
==============================
V9 修正紀錄（根據實際 Actions log 診斷）：

  Bug1（本次）：FinMind TaiwanStockPrice 全部回 400 Bad Request
    原因：finmind_get() 傳 start_date=end_date=today，但今日盤後資料
          在台灣時間 15:00 後才會出現在 API；盤中或剛收盤時回 400。
    修正：
      a) 找最近有資料的交易日（往前最多找5天）
      b) 一次抓「最近1個月」範圍（2026-06-01 ~ today），
         FinMind 會回傳所有在範圍內的日期，再取最新日期的資料
      c) token 改用 Authorization header 傳遞（FinMind v4 推薦方式）

  Bug2（本次）：openapi.twse.com.tw / mis.twse.com.tw 境外封鎖
    → fetch_live.py 也一起改用 FinMind（見 fetch_live.py）

  Bug3（V8已修）：chg_pct 計算錯誤 → 保留 V8 修正

  架構說明：
    - 股票清單：FinMind TaiwanStockInfo
    - 全市場行情：FinMind TaiwanStockPrice（一次抓近30天，取最新日）
    - 三大法人：FinMind TaiwanStockInstitutionalInvestors（個股）
    - 融資：FinMind TaiwanStockMarginPurchaseShortSale（個股）
    - 認購權證：FinMind TaiwanStockWarrant
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone

TZ_TW       = timezone(timedelta(hours=8))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N       = 10

FINMIND_URL   = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

def tw_now():
    return datetime.now(TZ_TW)

def today_str():
    return tw_now().strftime("%Y-%m-%d")

def today_str8():
    return tw_now().strftime("%Y%m%d")

def date_range_start(days_back=35):
    """從今天往前推 N 天作為 start_date"""
    d = tw_now() - timedelta(days=days_back)
    return d.strftime("%Y-%m-%d")

def prev_trading_dates(n=25):
    result, d = [], tw_now()
    while len(result) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            result.append(d.strftime("%Y-%m-%d"))
    return result

def safe_float(v, default=0.0):
    try:    return float(str(v).replace(",","").strip())
    except: return default

def safe_int(v, default=0):
    try:    return int(str(v).replace(",","").strip())
    except: return default

# ── FinMind 共用請求（V9修正：Authorization header 方式）──────
def finmind_get(dataset, data_id=None, start_date=None, end_date=None, retries=3):
    params = {"dataset": dataset}
    if data_id:    params["data_id"]    = data_id
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"]   = end_date

    # V9修正：token 同時放 params 和 header（相容新舊 API 版本）
    headers = {"Content-Type": "application/json"}
    if FINMIND_TOKEN:
        params["token"]        = FINMIND_TOKEN
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"

    for i in range(retries):
        try:
            r = requests.get(FINMIND_URL, params=params, headers=headers, timeout=30)
            if r.status_code == 402:
                print("  ! FinMind 超出 API 上限（402）")
                return []
            if r.status_code == 400:
                # 400 通常代表日期範圍無資料，不是致命錯誤
                print(f"  ! FinMind {dataset} 400：日期範圍可能無資料（{start_date}~{end_date}）")
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

# ── 全市場行情（V9核心修正）──────────────────────────────────
def fetch_all_prices_latest():
    """
    V9修正：不再用 start_date=today&end_date=today（盤中/剛收盤時回400）
    改用近35天範圍，取回所有資料後按日期找最新有資料的交易日。
    一次請求涵蓋所有股票，不需要逐支查詢。
    """
    start = date_range_start(35)
    end   = today_str()
    print(f"  → 抓取全市場行情範圍：{start} ~ {end}")

    rows = finmind_get("TaiwanStockPrice", start_date=start, end_date=end)
    if not rows:
        print("  ! TaiwanStockPrice 完全無資料")
        return {}, ""

    # 找最新的有資料日期（上市股票數量 > 100 才算有效）
    from collections import defaultdict
    by_date = defaultdict(list)
    for row in rows:
        date = str(row.get("date",""))
        sid  = str(row.get("stock_id","")).strip()
        if date and sid.isdigit() and len(sid)==4:
            by_date[date].append(row)

    # 找最近有大量資料的日期
    latest_date = ""
    for d in sorted(by_date.keys(), reverse=True):
        if len(by_date[d]) > 100:
            latest_date = d
            break

    if not latest_date:
        print("  ! 找不到有效交易日資料")
        return {}, ""

    print(f"  → 最新有效交易日：{latest_date}（{len(by_date[latest_date])} 支）")

    result = {}
    for row in by_date[latest_date]:
        try:
            sid    = str(row.get("stock_id","")).strip()
            if not (sid.isdigit() and len(sid)==4): continue
            close  = safe_float(row.get("close", 0))
            open_  = safe_float(row.get("open", 0))
            high   = safe_float(row.get("max", 0))
            low    = safe_float(row.get("min", 0))
            volume = safe_int(row.get("Trading_Volume", 0))
            spread = safe_float(row.get("spread", 0))
            if close <= 0: continue

            # chg_pct：spread 有值用 spread，否則用 close-open
            if spread != 0:
                prev_c  = close - spread
                chg_pct = round(spread / prev_c * 100, 2) if prev_c > 0 else 0
            elif open_ > 0:
                chg_pct = round((close - open_) / open_ * 100, 2)
            else:
                chg_pct = 0

            result[sid] = {
                "close":   close,
                "open":    open_,
                "high":    high,
                "low":     low,
                "volume":  volume,
                "chg_pct": chg_pct,
            }
        except:
            continue

    print(f"  → 全市場行情：{len(result)} 支（{latest_date}）")
    return result, latest_date

# ── 歷史行情（V9：用同一批 rows，不再逐支查詢，節省 API 次數）─
def build_history_from_rows_bulk(start_date, end_date, target_sids):
    """
    一次抓近35天全市場，按 sid 整理成歷史序列。
    target_sids：篩選後的候選股代號集合，節省記憶體。
    """
    rows = finmind_get("TaiwanStockPrice", start_date=start_date, end_date=end_date)
    sid_set = set(target_sids)
    history = {}  # sid -> [{date, close, volume}]
    for row in rows:
        sid = str(row.get("stock_id","")).strip()
        if sid not in sid_set: continue
        date = str(row.get("date",""))
        close  = safe_float(row.get("close",0))
        volume = safe_int(row.get("Trading_Volume",0))
        if close > 0:
            history.setdefault(sid, []).append({"date":date,"close":close,"volume":volume})
    for sid in history:
        history[sid].sort(key=lambda x: x["date"])
    return history

# ── 認購權證 ──────────────────────────────────────────────────
def fetch_warrants_finmind(today_date):
    today_dt = datetime.strptime(today_date, "%Y-%m-%d")
    start    = (today_dt - timedelta(days=5)).strftime("%Y-%m-%d")
    rows     = finmind_get("TaiwanStockWarrant", start_date=start, end_date=today_date)
    if not rows:
        return {}

    result = {}
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
            try:
                expire_dt = datetime.strptime(expire_str[:10], "%Y-%m-%d")
            except:
                continue
            days_left = (expire_dt - today_dt).days
            if days_left < 20:               continue
            if vol < 50:                     continue
            if leverage <= 0 or leverage > 15: continue
            if delta >= 0.70:    moneyness = "深度價內"
            elif delta >= 0.55:  moneyness = "輕度價內"
            elif delta >= 0.45:  moneyness = "價平"
            elif delta >= 0.30:  moneyness = "輕度價外"
            else:                moneyness = "價外"
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
        if w["volume"] > 500:          s += 1
        if 0.45 <= w["delta"] <= 0.65: s += 1
        return s
    for sid in result:
        result[sid].sort(key=lambda x: (-w_score(x), -x["volume"]))
        result[sid] = result[sid][:3]
    return result

# ── 三大法人（個股，近35天，取最新日）────────────────────────
def fetch_institutional_bulk(sids, start_date, end_date):
    """一次抓多支個股的三大法人（逐支查，因 FinMind 不支援全市場一次查）"""
    result = {}
    for sid in sids:
        rows = finmind_get("TaiwanStockInstitutionalInvestors", sid, start_date, end_date)
        by_date = {}
        for row in rows:
            date = str(row.get("date",""))
            name = str(row.get("name",""))
            net  = safe_int(row.get("buy",0)) - safe_int(row.get("sell",0))
            if date not in by_date:
                by_date[date] = {"foreign_net":0,"trust_net":0,"dealer_net":0}
            if "外資" in name:   by_date[date]["foreign_net"] += net
            elif "投信" in name: by_date[date]["trust_net"]   += net
            elif "自營" in name: by_date[date]["dealer_net"]  += net
        if by_date:
            latest = sorted(by_date.keys())[-1]
            v = by_date[latest]
            result[sid] = {
                "foreign_net": v["foreign_net"]//1000,
                "trust_net":   v["trust_net"]//1000,
                "dealer_net":  v["dealer_net"]//1000,
                "total_net":   (v["foreign_net"]+v["trust_net"]+v["dealer_net"])//1000,
            }
        time.sleep(0.2)
    return result

def fetch_margin_bulk(sids, start_date, end_date):
    result = {}
    for sid in sids:
        rows = finmind_get("TaiwanStockMarginPurchaseShortSale", sid, start_date, end_date)
        if rows:
            latest = sorted(rows, key=lambda x: x.get("date",""))[-1]
            result[sid] = {
                "margin_buy": safe_int(latest.get("MarginPurchaseBuy",0)),
                "margin_bal": safe_int(latest.get("MarginPurchaseRemainAmount",1)) or 1,
            }
        time.sleep(0.15)
    return result

# ── 評分 ─────────────────────────────────────────────────────
def calc_ma(closes, n):
    if len(closes) < n: return None
    return round(statistics.mean(closes[-n:]), 2)

def calc_score(tp, hist_closes, hist_vols, inst, margin):
    score, reasons, warnings = 0, [], []
    close   = tp["close"]
    high    = tp["high"]
    low     = tp["low"]
    chg_pct = tp["chg_pct"]

    if chg_pct >= 5:    score += 16; reasons.append("強勢大漲 ≥5%")
    elif chg_pct >= 3:  score += 12; reasons.append("大漲 ≥3%")
    elif chg_pct >= 1:  score += 7;  reasons.append("溫和上漲")
    elif chg_pct < 0:   score -= 10; warnings.append("今日收跌")

    if high > low:
        cp = (close - low) / (high - low)
        if cp >= 0.8:   score += 14; reasons.append("收盤靠近最高點（買盤強）")
        elif cp >= 0.6: score += 8
        elif cp < 0.3:  score -= 8;  warnings.append("長上影線（賣壓重）")

    if len(hist_vols) >= 5:
        avg_vol = statistics.mean(hist_vols[-5:])
        vr = tp["volume"] / avg_vol if avg_vol > 0 else 0
        if 1.5 <= vr <= 4:  score += 10; reasons.append(f"量能放大 {vr:.1f}x")
        elif vr > 4:        score += 5;  warnings.append("量能過度放大（注意追高）")
        elif vr < 0.7:      score -= 5;  warnings.append("量能萎縮")

    if len(hist_closes) >= 20:
        ma5  = calc_ma(hist_closes, 5)
        ma10 = calc_ma(hist_closes, 10)
        ma20 = calc_ma(hist_closes, 20)
        if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
            score += 10; reasons.append("均線多頭排列")
        elif ma5 and ma10 and ma5 > ma10:
            score += 5
        if ma5  and close > ma5:    score += 4
        if ma20 and close > ma20:   score += 6;  reasons.append(f"站上月線 MA20={ma20}")
        elif ma20 and close < ma20: score -= 5;  warnings.append("跌破月線")
        if hist_closes:
            rh = max(hist_closes[-10:]) if len(hist_closes)>=10 else hist_closes[-1]
            if close >= rh * 0.99: score += 10; reasons.append("突破近10日高點")

    if inst:
        tn = inst.get("total_net",0)
        fn = inst.get("foreign_net",0)
        tr = inst.get("trust_net",0)
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

    return max(0, min(100, score)), reasons, warnings

# ── 股票基本資料 ──────────────────────────────────────────────
def fetch_stock_info():
    rows = finmind_get("TaiwanStockInfo")
    result = {}
    for row in rows:
        sid = str(row.get("stock_id","")).strip()
        if not (sid.isdigit() and len(sid)==4): continue
        t = str(row.get("type","")).strip()
        if t not in ("twse","tpex"): continue
        result[sid] = {
            "name":   str(row.get("stock_name",sid)).strip(),
            "market": "tse" if t=="twse" else "otc",
        }
    return result

# ── 主程式 ────────────────────────────────────────────────────
def main():
    now    = tw_now()
    today  = now.strftime("%Y-%m-%d")
    today8 = now.strftime("%Y%m%d")
    print(f"[{now.strftime('%H:%M:%S')} 台灣時間] fetch_twse V9 開始 {today}")
    print(f"  FinMind token: {'已設定（600req/hr）' if FINMIND_TOKEN else '未設定（匿名300req/hr）'}")

    # ① 股票清單
    print("  ► 取得股票清單...")
    stock_info = fetch_stock_info()
    if not stock_info:
        print("  ! 股票清單失敗，中止")
        return
    print(f"  → {len(stock_info)} 支")
    time.sleep(0.5)

    # ② V9核心修正：全市場行情（近35天範圍，取最新有效日）
    print("  ► 全市場行情（近35天取最新日）...")
    all_today, actual_date = fetch_all_prices_latest()
    if not all_today:
        print("  ! 全市場行情取得失敗，中止")
        return
    actual_date8 = actual_date.replace("-","")
    time.sleep(0.8)

    # ③ 認購權證
    print("  ► 認購權證...")
    warrants = fetch_warrants_finmind(actual_date)
    print(f"  → {len(warrants)} 支標的有認購權證")
    time.sleep(0.5)

    # ④ 量價預篩
    print("  ► 量價預篩選...")
    pre_candidates = []
    for sid, tp in all_today.items():
        info = stock_info.get(sid)
        if not info: continue
        close   = tp["close"]
        volume  = tp["volume"]
        chg_pct = tp["chg_pct"]
        if close < 10 or close > 5000:  continue
        min_vol = 200000 if info["market"]=="otc" else 500000
        if volume < min_vol:             continue
        if chg_pct < 0.5:               continue
        pre_candidates.append(sid)

    print(f"  → 預篩後：{len(pre_candidates)} 支")

    if len(pre_candidates) == 0:
        print("  ! 預篩結果為0（可能是假日或盤後資料尚未更新），放寬條件...")
        for sid, tp in all_today.items():
            info = stock_info.get(sid)
            if not info: continue
            if tp["close"] < 10 or tp["close"] > 5000: continue
            if tp["volume"] < 100000: continue
            pre_candidates.append(sid)
        print(f"  → 放寬後：{len(pre_candidates)} 支")

    pre_candidates.sort(key=lambda s: (0 if s in warrants else 1, s))
    time.sleep(0.3)

    # ⑤ V9優化：一次抓近35天全市場歷史（不再逐支查詢，大幅節省 API 次數）
    hist_start = date_range_start(35)
    print(f"  ► 批次抓近35天歷史行情（{hist_start} ~ {actual_date}），候選 {len(pre_candidates[:60])} 支...")
    history = build_history_from_rows_bulk(hist_start, actual_date, pre_candidates[:60])
    print(f"  → 歷史資料：{len(history)} 支")
    time.sleep(0.8)

    # ⑥ 取有足夠歷史的候選股（≥5日）
    qualified = [sid for sid in pre_candidates[:60] if len(history.get(sid,[])) >= 5]
    print(f"  → 有足夠歷史：{len(qualified)} 支")

    # ⑦ 三大法人（個股，限 API 次數）
    max_inst = 40 if FINMIND_TOKEN else 20
    inst_sids = qualified[:max_inst]
    print(f"  ► 三大法人（{len(inst_sids)} 支）...")
    institutional = fetch_institutional_bulk(inst_sids, hist_start, actual_date)
    print(f"  → {len(institutional)} 筆")
    time.sleep(0.3)

    # ⑧ 融資（限 API 次數）
    max_margin = 30 if FINMIND_TOKEN else 15
    margin_sids = qualified[:max_margin]
    print(f"  ► 融資融券（{len(margin_sids)} 支）...")
    margin_data = fetch_margin_bulk(margin_sids, hist_start, actual_date)
    print(f"  → {len(margin_data)} 筆")
    time.sleep(0.3)

    # ⑨ 評分
    print("  ► 計算評分...")
    all_candidates = []
    for sid in qualified:
        info = stock_info[sid]
        tp   = all_today[sid]
        hist = history.get(sid, [])
        hist_closes = [h["close"]  for h in hist if h["date"] < actual_date]
        hist_vols   = [h["volume"] for h in hist if h["date"] < actual_date]
        if len(hist_closes) < 5: continue

        score, reasons, warnings = calc_score(tp, hist_closes, hist_vols,
                                               institutional.get(sid),
                                               margin_data.get(sid))
        if sid in warrants: score = min(100, score + 2)
        if score < 45: continue

        all_candidates.append({
            "sid":        sid,
            "name":       info["name"],
            "close":      tp["close"],
            "change_pct": tp["chg_pct"],
            "volume":     tp["volume"],
            "market":     info["market"],
            "score":      score,
            "reasons":    reasons,
            "warnings":   warnings,
            "inst":       institutional.get(sid, {}),
            "ma5":        calc_ma(hist_closes, 5),
            "ma10":       calc_ma(hist_closes, 10),
            "ma20":       calc_ma(hist_closes, 20),
            "warrants":   warrants.get(sid, []),
        })

    all_candidates.sort(key=lambda x: x["score"], reverse=True)
    top10 = all_candidates[:TOP_N]

    # ⑩ 機率標籤
    for c in top10:
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
        "trade_date":       actual_date8,
        "data_date":        actual_date,
        "total_scanned":    len(pre_candidates),
        "candidates_count": len(all_candidates),
        "stocks":           top10,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成！資料日期 {actual_date}，預篩 {len(pre_candidates)} 支，候選 {len(all_candidates)} 支，精選 {len(top10)} 支")
    for s in top10:
        wc = len(s.get("warrants",[]))
        print(f"  [{s['score']:3d}] {s['sid']} {s['name']:8s} {s['change_pct']:+.1f}%  {s['prob']}  權證:{wc}支")

if __name__ == "__main__":
    main()
