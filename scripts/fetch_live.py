#!/usr/bin/env python3
"""
台股盤中即時報價抓取腳本 V10
==============================
V10 修正：
  TaiwanStockPriceMinute（分K）= Sponsor tier（付費），免費 token 回 400
  → 改用以下免費策略：

  盤中（09:05~13:30）：
    TaiwanStockPrice data_id=sid start_date=today
    FinMind 17:30 才有正式收盤資料，但「盤中途中」呼叫時，
    若當日尚無收盤資料則回空 → fallback 顯示 stocks.json 的昨收

  盤後（13:30 後）：
    TaiwanStockPrice 一樣查，17:30 後有今日收盤就會出現

  最終策略：
    1. 查 TaiwanStockPrice(sid, today)   → 有今日收盤就顯示
    2. fallback：顯示 stocks.json 裡記錄的昨收（昨日篩選時的 close）
    3. 盤中狀態（is_trading）仍然顯示，讓使用者知道現在是盤中

  注意：FinMind 免費股價資料更新時間 Mon-Fri 17:30
        → 盤中期間前台卡片顯示「昨收 / 等待盤後更新」是正常現象
        → 若需要真正盤中即時：需要 FinMind Sponsor tier 或其他資料源
"""

import requests, json, os, time
from datetime import datetime, timezone, timedelta

TZ_TW         = timezone(timedelta(hours=8))
STOCKS_PATH   = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
LIVE_PATH     = os.path.join(os.path.dirname(__file__), "../data/live.json")
FM_URL        = "https://api.finmindtrade.com/api/v4/data"
TOKEN         = os.environ.get("FINMIND_TOKEN", "")

def tw_now():
    return datetime.now(TZ_TW)

def is_trading_now():
    now = tw_now()
    if now.weekday() >= 5: return False
    t = now.hour * 60 + now.minute
    return 9 * 60 <= t <= 13 * 60 + 30

def fm_price(sid, date_str):
    params = {
        "dataset":    "TaiwanStockPrice",
        "data_id":    sid,
        "start_date": date_str,
        "end_date":   date_str,
    }
    hdrs = {}
    if TOKEN: hdrs["Authorization"] = f"Bearer {TOKEN}"
    try:
        r = requests.get(FM_URL, params=params, headers=hdrs, timeout=20)
        if r.status_code in (400, 402): return None
        r.raise_for_status()
        d = r.json()
        rows = d.get("data", []) if d.get("status") == 200 else []
        if not rows: return None
        row = rows[-1]
        close = float(row.get("close", 0) or 0)
        if close <= 0: return None
        spread = float(row.get("spread", 0) or 0)
        prev_c = round(close - spread, 2) if spread else close
        return {
            "price":  str(close),
            "prev":   str(prev_c),
            "open":   str(row.get("open", close)),
            "high":   str(row.get("max",  close)),
            "low":    str(row.get("min",  close)),
            "vol":    str(row.get("Trading_Volume", 0)),
            "source": "finmind_daily",
        }
    except Exception as e:
        print(f"  ! fm_price {sid} → {e}")
        return None

def main():
    now     = tw_now()
    now_str = now.strftime("%Y/%m/%d %H:%M:%S")
    today   = now.strftime("%Y-%m-%d")
    trading = is_trading_now()
    errors  = []

    print(f"[{now_str} 台灣時間] fetch_live V10 (交易中: {trading})")
    print(f"  FinMind token: {'已設定' if TOKEN else '未設定（匿名）'}")

    if not os.path.exists(STOCKS_PATH):
        errors.append("stocks.json 不存在，請先執行每日盤後抓資料")
        _write(now_str, now.strftime("%Y%m%d"), trading, {}, errors)
        return

    with open(STOCKS_PATH, encoding="utf-8") as f:
        stocks_data = json.load(f)

    stocks = stocks_data.get("stocks", [])
    if not stocks:
        errors.append("stocks.json 無候選股，請手動觸發「每日盤後抓資料」workflow")
        _write(now_str, now.strftime("%Y%m%d"), trading, {}, errors)
        return

    print(f"  → 候選股：{', '.join(s['sid'] for s in stocks)}")

    live = {}
    got  = 0

    for s in stocks:
        sid      = s["sid"]
        name     = s.get("name", sid)
        market   = s.get("market", "tse")
        prev_c   = str(s.get("close", "-"))  # stocks.json 裡的昨收

        p = fm_price(sid, today)
        time.sleep(0.25)

        if p:
            # 取到今日資料（通常是盤後 17:30+ 才有）
            p["prev"]   = str(prev_c)
            p["name"]   = name
            p["market"] = market
            live[sid]   = p
            got += 1
            price = p["price"]
            try:
                chg = f"+{(float(price)-float(prev_c))/float(prev_c)*100:.2f}%" \
                      if float(prev_c) > 0 else "--"
            except: chg = "--"
            print(f"  ✓ {sid} {name[:6]:6s} {price:>8s}  {chg}  [今日收盤]")
        else:
            # 盤中或資料未更新：fallback 顯示昨收（靜態）
            live[sid] = {
                "price":  prev_c,
                "prev":   prev_c,
                "open":   "-", "high": "-", "low": "-", "vol": "0",
                "name":   name,
                "market": market,
                "source": "fallback_yesterday",
            }
            status = "盤中等待" if trading else "昨收（資料未更新）"
            print(f"  - {sid} {name[:6]:6s} {prev_c:>8s}  [{status}]")

    print(f"  💾 live.json 寫入完成（{got}/{len(stocks)} 筆今日收盤）")

    if trading and got == 0:
        errors.append(
            "盤中期間：FinMind 日K資料在 17:30 後更新，盤中顯示昨收為正常現象。"
            "若需要即時報價請至 finmindtrade.com 查詢 Sponsor 方案。"
        )
    elif not trading and got == 0:
        errors.append("今日收盤資料尚未更新（FinMind 每日 17:30 更新），請稍後重新整理。")

    _write(now_str, now.strftime("%Y%m%d"), trading, live, errors)

def _write(now_str, trade_date, trading, prices, errors):
    output = {
        "updated_at":   now_str,
        "trade_date":   trade_date,
        "is_trading":   trading,
        "prices":       prices,
        "fetch_errors": errors,
    }
    os.makedirs(os.path.dirname(LIVE_PATH), exist_ok=True)
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
