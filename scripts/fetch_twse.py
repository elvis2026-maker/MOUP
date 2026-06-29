#!/usr/bin/env python3
"""
台股權證標的篩選腳本 V10
==============================
V10 根本修正（反向思考）：

  核心問題：
    TaiwanStockPrice 不帶 data_id → 需要 Backer/Sponsor（付費）
    免費 token 查全市場一律回 400，這就是每次「全市場行情取得失敗」的根源。
    從 V5 到 V9 都在同一個坑裡打轉。

  V10 新策略（完全免費 tier 可用）：
    Step1. TaiwanStockInfoWithWarrant（免費）→ 取「有認購權證的標的」約 300~400 支
    Step2. 對這 300~400 支逐一查 TaiwanStockPrice（免費，帶 data_id）
           → 有 token 600/hr，300 支 × 1 req = 300 req，完全夠用
           → 無 token 300/hr 上限，掃描前 280 支
    Step3. 在 Step2 的結果裡按漲幅/量能/技術面評分，取 Top10
    Step4. 對 Top10 逐一查 TaiwanStockInstitutionalInvestors + Margin（免費）
    Step5. 對 Top10 查 TaiwanStockWarrant（免費）取權證清單

  優點：
    - 完全不碰「需要 Backer 的 batch endpoint」
    - API 次數可預測：約 300（股價）+ 20（法人）+ 10（融資）+ 1（權證）= ~331 次
    - 有 token 綽綽有餘；無 token 略微壓縮掃描範圍
"""

import requests, json, time, os, statistics
from datetime import datetime, timedelta, timezone

TZ_TW       = timezone(timedelta(hours=8))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
TOP_N       = 10
FM_URL      = "https://api.finmindtrade.com/api/v4/data"
TOKEN       = os.environ.get("FINMIND_TOKEN", "")

def tw_now():
    return datetime.now(TZ_TW)

def today_str():
    return tw_now().strftime("%Y-%m-%d")

def today_str8():
    return tw_now().strftime("%Y%m%d")

def date_back(days):
    return (tw_now() - timedelta(days=days)).strftime("%Y-%m-%d")

def sf(v, d=0.0):
    try:    return float(str(v).replace(",","").strip())
    except: return d

def si(v, d=0):
    try:    return int(str(v).replace(",","").strip())
    except: return d

# ── FinMind 請求（帶 data_id 的免費 endpoint）───────────────
def fm(dataset, data_id, start_date, end_date=None, retries=3):
    params = {"dataset": dataset, "data_id": data_id, "start_date": start_date}
    if end_date: params["end_date"] = end_date
    hdrs = {}
    if TOKEN: hdrs["Authorization"] = f"Bearer {TOKEN}"
    for i in range(retries):
        try:
            r = requests.get(FM_URL, params=params, headers=hdrs, timeout=25)
            if r.status_code == 402:
                print("  ! FinMind 402：API 次數超限，請稍後再試")
                return []
            if r.status_code == 400:
                # 400 = 此 stock_id 無該日期資料（例如興櫃、剛上市），正常跳過
                return []
            r.raise_for_status()
            d = r.json()
            return d.get("data", []) if d.get("status") == 200 else []
        except Exception as e:
            if i < retries - 1: time.sleep(1.5 * (i + 1))
            else: print(f"  ! fm {dataset}/{data_id} → {e}")
    return []

def fm_nokey(dataset, retries=3):
    """不帶 data_id 的 endpoint（TaiwanStockInfo 等免費全表）"""
    params = {"dataset": dataset}
    hdrs   = {}
    if TOKEN: hdrs["Authorization"] = f"Bearer {TOKEN}"
    for i in range(retries):
        try:
            r = requests.get(FM_URL, params=params, headers=hdrs, timeout=30)
            r.raise_for_status()
            d = r.json()
            return d.get("data", []) if d.get("status") == 200 else []
        except Exception as e:
            if i < retries - 1: time.sleep(2)
            else: print(f"  ! fm_nokey {dataset} → {e}")
    return []

# ── Step1：有認購權證的標的（免費）─────────────────────────
def fetch_warrant_targets():
    rows = fm_nokey("TaiwanStockInfoWithWarrant")
    result = {}
    for row in rows:
        sid = str(row.get("stock_id","")).strip()
        t   = str(row.get("type","")).strip()
        if not (sid.isdigit() and len(sid) == 4): continue
        if t not in ("twse", "tpex"):             continue
        result[sid] = {
            "name":   str(row.get("stock_name", sid)).strip(),
            "market": "tse" if t == "twse" else "otc",
        }
    return result

# ── Step2：逐支查當日股價（免費，帶 data_id）────────────────
def fetch_price_one(sid, start_date, end_date):
    rows = fm(dataset="TaiwanStockPrice",
              data_id=sid,
              start_date=start_date,
              end_date=end_date)
    result = []
    for row in rows:
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
    return sorted(result, key=lambda x: x["date"])

# ── Step4：三大法人（免費，帶 data_id）──────────────────────
def fetch_inst_one(sid, start_date, end_date):
    rows = fm("TaiwanStockInstitutionalInvestors", sid, start_date, end_date)
    by_date = {}
    for row in rows:
        date = str(row.get("date",""))
        name = str(row.get("name",""))
        net  = si(row.get("buy",0)) - si(row.get("sell",0))
        if date not in by_date:
            by_date[date] = {"foreign_net":0,"trust_net":0,"dealer_net":0}
        if   "外資" in name: by_date[date]["foreign_net"] += net
        elif "投信" in name: by_date[date]["trust_net"]   += net
        elif "自營" in name: by_date[date]["dealer_net"]  += net
    if not by_date: return {}
    latest = sorted(by_date.keys())[-1]
    v = by_date[latest]
    return {
        "foreign_net": v["foreign_net"]//1000,
        "trust_net":   v["trust_net"]//1000,
        "dealer_net":  v["dealer_net"]//1000,
        "total_net":   (v["foreign_net"]+v["trust_net"]+v["dealer_net"])//1000,
        "date":        latest,
    }

def fetch_margin_one(sid, start_date, end_date):
    rows = fm("TaiwanStockMarginPurchaseShortSale", sid, start_date, end_date)
    if not rows: return {}
    latest = sorted(rows, key=lambda x: x.get("date",""))[-1]
    bal = si(latest.get("MarginPurchaseRemainAmount",1)) or 1
    return {
        "margin_buy": si(latest.get("MarginPurchaseBuy",0)),
        "margin_bal": bal,
    }

# ── Step5：認購權證細節（免費）──────────────────────────────
def fetch_warrants_for(sids, data_date_str):
    """對 top10 的每支股票查 TaiwanStockWarrant（帶 data_id）"""
    dt = datetime.strptime(data_date_str, "%Y-%m-%d")
    start = (dt - timedelta(days=3)).strftime("%Y-%m-%d")
    result = {}
    for sid in sids:
        rows = fm("TaiwanStockWarrant", sid, start, data_date_str)
        warrants = []
        for row in rows:
            try:
                call_put = str(row.get("PutCall","")).strip()
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
                    "code":       w_code,
                    "issuer":     str(row.get("Issuer",""))[:3],
                    "type":       "call",
                    "expire":     expire_dt.strftime("%Y/%m/%d"),
                    "days_left":  days_left,
                    "leverage":   round(leverage, 1),
                    "delta":      round(delta, 2),
                    "moneyness":  moneyness,
                    "bid":        bid,
                    "ask":        ask,
                    "volume":     vol,
                    "leverage_ok": 4 < leverage < 12,
                })
            except: continue
        # 排序：leverage_ok + 量大 + delta 靠近 0.55
        warrants.sort(key=lambda x: (
            0 if x["leverage_ok"] else 1,
            -x["volume"],
            abs(x["delta"] - 0.55)
        ))
        result[sid] = warrants[:3]
        time.sleep(0.2)
    return result

# ── 評分 ─────────────────────────────────────────────────────
def calc_ma(closes, n):
    if len(closes) < n: return None
    return round(statistics.mean(closes[-n:]), 2)

def calc_score(today_p, hist, inst, margin):
    score, reasons, warnings = 0, [], []
    close   = today_p["close"]
    high    = today_p["high"]
    low_p   = today_p["low"]
    spread  = today_p.get("spread", 0)
    prev_c  = round(close - spread, 2) if spread != 0 else (hist[-1]["close"] if hist else close)
    chg_pct = round(spread / prev_c * 100, 2) if prev_c > 0 else 0

    if chg_pct >= 5:   score += 16; reasons.append("強勢大漲 ≥5%")
    elif chg_pct >= 3: score += 12; reasons.append("大漲 ≥3%")
    elif chg_pct >= 1: score += 7;  reasons.append("溫和上漲")
    elif chg_pct < 0:  score -= 10; warnings.append("今日收跌")

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
        if ma5  and close > ma5:    score += 4
        if ma20 and close > ma20:   score += 6;  reasons.append(f"站上月線 MA20={ma20}")
        elif ma20 and close < ma20: score -= 5;  warnings.append("跌破月線")
        rh = max(closes[-10:]) if len(closes) >= 10 else closes[-1]
        if close >= rh * 0.99: score += 10; reasons.append("突破近10日高點")

    if inst:
        tn = inst.get("total_net", 0)
        tr = inst.get("trust_net", 0)
        fn = inst.get("foreign_net", 0)
        if tn > 5000:    score += 15; reasons.append(f"三大法人大幅買超 {tn}張")
        elif tn > 1000:  score += 10; reasons.append(f"三大法人買超 {tn}張")
        elif tn > 0:     score += 5
        elif tn < -3000: score -= 10; warnings.append("三大法人大幅賣超")
        if tr > 500:     score += 5;  reasons.append("投信積極買超")
        if fn > 3000:    score += 5;  reasons.append("外資積極買超")

    if margin:
        mb  = margin.get("margin_buy", 0)
        mbl = margin.get("margin_bal", 1)
        if mbl > 0 and mb / mbl > 0.15:
            score -= 5; warnings.append("融資追價明顯（散戶擁擠）")

    return max(0, min(100, score)), reasons, warnings, chg_pct

# ── 主程式 ────────────────────────────────────────────────────
def main():
    now    = tw_now()
    today  = today_str()
    today8 = today_str8()
    has_token = bool(TOKEN)
    print(f"[{now.strftime('%H:%M:%S')} 台灣時間] fetch_twse V10 開始 {today}")
    print(f"  FinMind token: {'已設定（600req/hr）' if has_token else '未設定（匿名300req/hr）'}")

    # ──────────────────────────────────────────────────────────
    # Step1：取「有認購權證」的標的清單（免費全表，1 req）
    # ──────────────────────────────────────────────────────────
    print("  ► Step1 取有認購權證標的清單（TaiwanStockInfoWithWarrant）...")
    warrant_targets = fetch_warrant_targets()
    if not warrant_targets:
        print("  ! Step1 失敗，中止")
        return
    sid_list = list(warrant_targets.keys())
    print(f"  → {len(sid_list)} 支有認購權證")
    time.sleep(0.3)

    # ──────────────────────────────────────────────────────────
    # Step2：對每支逐一查近 25 天股價（免費，帶 data_id）
    # 有 token：全掃；無 token：限 280 支（接近 300/hr 上限）
    # ──────────────────────────────────────────────────────────
    start_date = date_back(25)
    end_date   = today
    scan_limit = len(sid_list) if has_token else 280
    scan_sids  = sid_list[:scan_limit]

    print(f"  ► Step2 逐支查股價（{len(scan_sids)} 支，{start_date}~{end_date}）...")
    pre_candidates = []
    req_count = 1  # Step1 算 1 req

    for idx, sid in enumerate(scan_sids):
        if (idx + 1) % 50 == 0:
            print(f"  ... {idx+1}/{len(scan_sids)}  候選:{len(pre_candidates)}  API:{req_count}")

        hist_rows = fetch_price_one(sid, start_date, end_date)
        req_count += 1
        time.sleep(0.08)  # 每秒約 12 req，避免觸發 rate limit

        if len(hist_rows) < 6:  # 至少需要 5 天歷史 + 1 天今日
            continue

        today_p = hist_rows[-1]
        # 確認最後一筆確實是今日或最近交易日（FinMind 17:30 後才有今日資料）
        # 允許「最近 3 個自然日內」，以涵蓋剛收盤但還未到 17:30 的情境
        from datetime import date as ddate
        last_data_date = datetime.strptime(today_p["date"], "%Y-%m-%d").date()
        today_date     = now.date()
        if (today_date - last_data_date).days > 5:
            # 資料太舊，跳過
            continue

        close   = today_p["close"]
        volume  = today_p["volume"]
        info    = warrant_targets[sid]
        market  = info["market"]

        if close < 10 or close > 5000: continue
        min_vol = 150000 if market == "otc" else 400000
        if volume < min_vol:           continue

        hist = hist_rows[:-1]  # 不含今日

        _, _, _, chg_pct = calc_score(today_p, hist, {}, {})
        if chg_pct < 0.3: continue  # 初步過濾：今日需正報酬

        pre_candidates.append({
            "sid":    sid,
            "info":   info,
            "today_p":today_p,
            "hist":   hist,
            "chg_pct": chg_pct,
            "data_date": today_p["date"],
        })

        # 安全停止
        if req_count >= 290 and not has_token:
            print(f"  ! 接近免費 API 上限（{req_count} req），停止掃描")
            break

    print(f"  → Step2 完成：{len(pre_candidates)} 支初步候選（共 {req_count} req）")

    # 若完全沒有候選（盤後資料還沒更新），放寬條件
    if not pre_candidates:
        print("  ! 初步篩選為 0，可能是今日資料尚未更新（FinMind 17:30 後才有當日收盤）")
        print("  ! 改用「最近有資料的交易日」繼續...")
        # 用 hist_rows[-1] 不管日期的版本
        for idx, sid in enumerate(scan_sids[:150]):
            hist_rows = fetch_price_one(sid, date_back(35), today)
            req_count += 1
            time.sleep(0.08)
            if len(hist_rows) < 6: continue
            today_p = hist_rows[-1]
            close   = today_p["close"]
            volume  = today_p["volume"]
            info    = warrant_targets[sid]
            if close < 10 or close > 5000: continue
            if volume < (100000 if info["market"] == "otc" else 300000): continue
            hist = hist_rows[:-1]
            _, _, _, chg_pct = calc_score(today_p, hist, {}, {})
            if chg_pct < 0: continue
            pre_candidates.append({
                "sid": sid, "info": info,
                "today_p": today_p, "hist": hist,
                "chg_pct": chg_pct, "data_date": today_p["date"],
            })
            if req_count >= 290: break
        print(f"  → 放寬後：{len(pre_candidates)} 支")

    if not pre_candidates:
        print("  ! 仍無候選，可能是假日或 API 問題，輸出空結果")
        _write_empty(now, today8)
        return

    # ──────────────────────────────────────────────────────────
    # Step3：評分 + 取 Top20（再深入查）
    # ──────────────────────────────────────────────────────────
    print("  ► Step3 初步評分...")
    pre_candidates.sort(key=lambda x: x["chg_pct"], reverse=True)
    top20 = pre_candidates[:20]

    # 取 actual_date（最多候選股使用的日期）
    from collections import Counter
    date_votes = Counter(c["data_date"] for c in pre_candidates)
    actual_date = date_votes.most_common(1)[0][0]
    actual_date8 = actual_date.replace("-","")
    print(f"  → 實際資料日期：{actual_date}（{len(pre_candidates)} 支中最多）")

    # ──────────────────────────────────────────────────────────
    # Step4：對 Top20 查三大法人 + 融資（免費，帶 data_id）
    # ──────────────────────────────────────────────────────────
    inst_start = date_back(10)
    print(f"  ► Step4 三大法人 + 融資（{len(top20)} 支）...")
    inst_map   = {}
    margin_map = {}
    for c in top20:
        sid = c["sid"]
        inst_map[sid]   = fetch_inst_one(sid, inst_start, today)
        req_count += 1
        time.sleep(0.15)
        margin_map[sid] = fetch_margin_one(sid, inst_start, today)
        req_count += 1
        time.sleep(0.1)
    print(f"  → 完成（API:{req_count}）")

    # ──────────────────────────────────────────────────────────
    # Step3b：完整評分，取 Top10
    # ──────────────────────────────────────────────────────────
    scored = []
    for c in top20:
        sid  = c["sid"]
        hist = c["hist"]
        if len(hist) < 5: continue
        score, reasons, warnings, chg_pct = calc_score(
            c["today_p"], hist,
            inst_map.get(sid,{}),
            margin_map.get(sid,{})
        )
        score = min(100, score + 2)  # 有認購權證加分
        if score < 45: continue
        ma_c = [h["close"] for h in hist]
        scored.append({
            "sid":        sid,
            "name":       c["info"]["name"],
            "close":      c["today_p"]["close"],
            "change_pct": chg_pct,
            "volume":     c["today_p"]["volume"],
            "market":     c["info"]["market"],
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

    # ──────────────────────────────────────────────────────────
    # Step5：查 Top10 的認購權證細節
    # ──────────────────────────────────────────────────────────
    if top10:
        print(f"  ► Step5 認購權證細節（{len(top10)} 支）...")
        warrant_map = fetch_warrants_for([c["sid"] for c in top10], actual_date)
        req_count += len(top10)
        for c in top10:
            c["warrants"] = warrant_map.get(c["sid"], [])
        print(f"  → 完成（API:{req_count}）")

    # ── 機率標籤 ────────────────────────────────────────────
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
        "stocks":           top10,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成！資料:{actual_date}  掃:{len(scan_sids)}  候選:{len(scored)}  精選:{len(top10)}  API:{req_count}")
    for s in top10:
        wc = len(s.get("warrants",[]))
        print(f"  [{s['score']:3d}] {s['sid']} {s['name'][:8]:8s} {s['change_pct']:+.2f}%  {s['prob']}  權證:{wc}支")

def _write_empty(now, today8):
    output = {
        "updated_at":       now.strftime("%Y/%m/%d %H:%M"),
        "trade_date":       today8,
        "data_date":        today_str(),
        "total_scanned":    0,
        "candidates_count": 0,
        "stocks":           [],
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print("  💾 stocks.json 輸出空結果")

if __name__ == "__main__":
    main()
