#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V11
==============================
V11 修正 V10.1 的問題：

  問題①：TaiwanStockWarrant 回 422（Unprocessable Entity）
    V10.1 的 fm() 只處理 400/404，422 被當成「正常但空資料」
    → active_sids 永遠是 0，喪失活躍標的優先排序

  問題②：TaiwanStockInstitutionalInvestors 部分股票回 422
    同上，應視為「無此資料」而非 API 錯誤

  V11 修正：
    - fm() 將 422 加入「視為無資料」的 status code（非限額）
    - 只有 402 才是真正的限額
    - 422 可能代表：查詢區間無資料、data_id 不合規等，正常跳過

  問題③：盤中 live panel 顯示「選股資料載入失敗」
    → 見 fetch_live.py V11 修正（stocks.json 空時的處理）

  總 API 請求：同 V10.1，約 252 req，安全
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone
from collections import Counter

TZ_TW       = timezone(timedelta(hours=8))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N       = 10
FM_URL      = "https://api.finmindtrade.com/api/v4/data"
TOKEN       = os.environ.get("FINMIND_TOKEN", "")

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
            # V11修正：只有 402 是真正的限額
            if r.status_code == 402:
                print("  ! FinMind 402：API 次數超限！停止後續請求")
                return [], True   # (data, hit_limit)
            # V11修正：400/404/422 均視為「無此資料」，正常跳過
            if r.status_code in (400, 404, 422):
                if r.status_code == 422:
                    print(f"  ! fm {dataset}/{data_id} → 422（無此資料/查詢區間無交易，跳過）")
                return [], False
            r.raise_for_status()
            d = r.json()
            return (d.get("data", []) if d.get("status") == 200 else []), False
        except Exception as e:
            if i < retries - 1: time.sleep(1.5 * (i + 1))
            else: print(f"  ! fm {dataset}/{data_id} → {e}")
    return [], False

def fm1(dataset, data_id=None, start_date=None, end_date=None):
    """回傳 (data, hit_limit)"""
    return fm(dataset, data_id, start_date, end_date)

# ── Step①：有認購權證的標的（全表，1 req）───────────────────
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

# ── Step②：預篩 → 排除 ETF、低流動性，保留主力股 ────────────
EXCLUDE_SIDS = {"9999", "0000"}

def prefilter_sids(warrant_targets, active_warrant_sids):
    """
    兩層篩選：
    1. 排除明顯的 ETF/低流動性
    2. 優先保留有「活躍認購權證」的標的
    上限 200 支，以控制 API 次數
    """
    priority = [s for s in active_warrant_sids if s in warrant_targets]
    rest = [s for s in warrant_targets if s not in active_warrant_sids and s not in EXCLUDE_SIDS]
    combined = priority + rest
    limit = 200 if TOKEN else 150
    return combined[:limit]

# ── Step③：近期活躍認購權證標的（1 req）─────────────────────
def fetch_active_warrant_targets():
    """
    查 TaiwanStockWarrant 近 7 天，找有成交量的認購權證標的
    V11修正：422 已在 fm() 中正確處理為「無資料」，不再擋住流程
    回傳 sorted sid list，按成交量排序
    """
    start = date_back(7)
    today = tw_now().strftime("%Y-%m-%d")
    data, _ = fm1("TaiwanStockWarrant", start_date=start, end_date=today)
    
    if not data:
        print("  ! TaiwanStockWarrant 無資料（可能是假日或 422），使用全部標的排序")
        return []
    
    vol_map = {}
    for row in data:
        call_put = str(row.get("PutCall",""))
        if "C" not in call_put and "認購" not in call_put: continue
        sid = str(row.get("underlying_stock","")).strip()
        if not (sid.isdigit() and len(sid) == 4): continue
        vol = si(row.get("TradingVolume", 0))
        vol_map[sid] = vol_map.get(sid, 0) + vol
    
    sorted_sids = sorted(vol_map.keys(), key=lambda s: -vol_map[s])
    print(f"  → TaiwanStockWarrant 取得 {len(sorted_sids)} 支活躍標的")
    return sorted_sids[:150]

# ── Step④：逐支查股價 ────────────────────────────────────────
def fetch_price(sid, start_date, end_date):
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

# ── Step⑤：三大法人 & 融資 ───────────────────────────────────
def fetch_inst(sid, start_date, end_date):
    data, hit = fm1("TaiwanStockInstitutionalInvestors", sid, start_date, end_date)
    if hit: return {}, True
    # V11：422 已在 fm() 處理，data 為空時正常返回 {}
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

# ── Step⑥：Top10 權證細節 ─────────────────────────────────
def fetch_warrant_detail(sid, data_date_str):
    dt    = datetime.strptime(data_date_str, "%Y-%m-%d")
    start = (dt - timedelta(days=3)).strftime("%Y-%m-%d")
    data, _ = fm1("TaiwanStockWarrant", sid, start, data_date_str)
    warrants = []
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
            if days_left < 20: continue
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
    warrants.sort(key=lambda x:(0 if x["leverage_ok"] else 1,-x["volume"],abs(x["delta"]-0.55)))
    return warrants[:3]

# ── 評分 ─────────────────────────────────────────────────────
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

    if chg >= 5:   score += 16; reasons.append("強勢大漲 ≥5%")
    elif chg >= 3: score += 12; reasons.append("大漲 ≥3%")
    elif chg >= 1: score += 7;  reasons.append("溫和上漲")
    elif chg < 0:  score -= 10; warnings.append("今日收跌")

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

# ── 主程式 ────────────────────────────────────────────────────
def main():
    now    = tw_now()
    today  = now.strftime("%Y-%m-%d")
    today8 = now.strftime("%Y%m%d")
    req    = 0

    print(f"[{now.strftime('%H:%M:%S')} 台灣時間] fetch_twse V11 開始 {today}")
    print(f"  FinMind token: {'已設定（600req/hr）' if TOKEN else '未設定（匿名300req/hr）'}")

    # ── ① 有認購權證的全部標的（1 req）─────────────────────────
    print("  ► ① 取有認購權證標的...")
    warrant_targets = fetch_warrant_targets(); req += 1
    if not warrant_targets: print("  ! ① 失敗，中止"); return
    print(f"  → {len(warrant_targets)} 支")
    time.sleep(0.3)

    # ── ③ 近期活躍認購權證標的（1 req）─────────────────────────
    print("  ► ③ 取近期活躍認購權證...")
    active_sids = fetch_active_warrant_targets(); req += 1
    print(f"  → {len(active_sids)} 支活躍標的")
    time.sleep(0.3)

    # ── ② 預篩，控制總數 ≤ 200 ──────────────────────────────────
    scan_sids = prefilter_sids(warrant_targets, active_sids)
    print(f"  ► ② 預篩後掃描清單：{len(scan_sids)} 支（API 預算：{req}+{len(scan_sids)}+40+10={req+len(scan_sids)+50}）")

    # ── ④ 逐支查股價 ────────────────────────────────────────────
    start_date = date_back(30)
    end_date   = today
    print(f"  ► ④ 逐支查股價（{start_date}~{end_date}）...")

    candidates = []
    stop = False

    for idx, sid in enumerate(scan_sids):
        if stop: break
        if (idx + 1) % 50 == 0:
            print(f"  ... {idx+1}/{len(scan_sids)}  候選:{len(candidates)}  req:{req}")

        hist_rows, hit = fetch_price(sid, start_date, end_date)
        req += 1
        if hit:
            print(f"  ! 觸發 API 限額（req={req}），停止掃描")
            stop = True; break
        time.sleep(0.06)

        if not hist_rows or len(hist_rows) < 6: continue

        today_p = hist_rows[-1]
        last_dt = datetime.strptime(today_p["date"], "%Y-%m-%d").date()
        if (now.date() - last_dt).days > 5: continue

        close  = today_p["close"]
        volume = today_p["volume"]
        info   = warrant_targets.get(sid, {})
        market = info.get("market", "tse")

        if close < 10 or close > 5000: continue
        if volume < (150000 if market == "otc" else 400000): continue

        hist = hist_rows[:-1]
        if len(hist) < 5: continue

        _, _, _, chg = calc_score(today_p, hist, {}, {})
        if chg < 0.3: continue

        candidates.append({
            "sid": sid, "info": info,
            "today_p": today_p, "hist": hist,
            "chg": chg, "data_date": today_p["date"],
        })

    print(f"  → ④ 完成：{len(candidates)} 支候選（req={req}）")

    if not candidates:
        print("  ! 無候選，可能今日資料尚未更新（FinMind 17:30 後更新）")
        _write_empty(now, today8, req); return

    date_votes = Counter(c["data_date"] for c in candidates)
    actual_date  = date_votes.most_common(1)[0][0]
    actual_date8 = actual_date.replace("-","")
    print(f"  → 實際資料日期：{actual_date}")

    # ── ⑤ Top20：查三大法人 + 融資 ─────────────────────────────
    candidates.sort(key=lambda x: x["chg"], reverse=True)
    top20 = candidates[:20]
    inst_start = date_back(10)

    print(f"  ► ⑤ 三大法人 + 融資（{len(top20)} 支）...")
    inst_map   = {}
    margin_map = {}
    for c in top20:
        if stop: break
        sid = c["sid"]
        res, hit = fetch_inst(sid, inst_start, end_date); req += 1
        if hit: stop = True; break
        inst_map[sid] = res; time.sleep(0.12)

        res, hit = fetch_margin(sid, inst_start, end_date); req += 1
        if hit: stop = True; break
        margin_map[sid] = res; time.sleep(0.10)

    print(f"  → ⑤ 完成（req={req}）")

    # ── 完整評分，取 Top10 ───────────────────────────────────────
    scored = []
    for c in top20:
        sid  = c["sid"]
        hist = c["hist"]
        if len(hist) < 5: continue
        score, reasons, warnings, chg = calc_score(
            c["today_p"], hist,
            inst_map.get(sid,{}), margin_map.get(sid,{})
        )
        score = min(100, score + 2)
        if score < 45: continue
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
    top10 = scored[:TOP_N]

    # ── ⑥ Top10 查權證細節 ─────────────────────────────────────
    if top10 and not stop:
        print(f"  ► ⑥ 權證細節（{len(top10)} 支）...")
        for c in top10:
            c["warrants"] = fetch_warrant_detail(c["sid"], actual_date)
            req += 1; time.sleep(0.15)
        print(f"  → ⑥ 完成（req={req}）")

    # ── 機率標籤 ────────────────────────────────────────────────
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
        "total_scanned":    len(scan_sids),
        "candidates_count": len(scored),
        "total_api_req":    req,
        "stocks":           top10,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成！資料:{actual_date}  掃:{len(scan_sids)}  候選:{len(scored)}  精選:{len(top10)}  API:{req}")
    for s in top10:
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
