#!/usr/bin/env python3
"""
台股盤中即時報價抓取腳本 V9
==============================
V9 修正：
  - openapi.twse.com.tw  ❌ 境外封鎖
  - mis.twse.com.tw       ❌ 境外封鎖
  - www.twse.com.tw       ❌ 境外封鎖

  → 全部改用 FinMind（境外 IP 可用）

  盤中即時報價策略：
    1. 主要：FinMind TaiwanStockPriceMinute（分K，取最後一筆）
       - 有即時報價（通常延遲 10~30 分鐘）
       - 逐支查，候選股最多 10 支，API 次數可控
    2. 備用：FinMind TaiwanStockPrice（當日收盤價，無盤中）
       - 若 TaiwanStockPriceMinute 無資料（非盤中、剛開盤），用此

  注意：FinMind 免費 token 每小時 600 次，
        10 支股票 × 每30分鐘 = 10次，完全夠用。
"""

import requests, json, os, time
from datetime import datetime, timezone, timedelta

TZ_TW         = timezone(timedelta(hours=8))
STOCKS_PATH   = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
LIVE_PATH     = os.path.join(os.path.dirname(__file__), "../data/live.json")
FINMIND_URL   = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

def tw_now():
    return datetime.now(TZ_TW)

def is_trading_now():
    now = tw_now()
    if now.weekday() >= 5: return False
    total = now.hour * 60 + now.minute
    return (9 * 60) <= total <= (13 * 60 + 30)

def finmind_get(dataset, data_id=None, start_date=None, end_date=None, retries=2):
    params = {"dataset": dataset}
    if data_id:    params["data_id"]    = data_id
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"]   = end_date
    headers = {"Content-Type": "application/json"}
    if FINMIND_TOKEN:
        params["token"]          = FINMIND_TOKEN
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"
    for i in range(retries):
        try:
            r = requests.get(FINMIND_URL, params=params, headers=headers, timeout=20)
            if r.status_code in (400, 402):
                return []
            r.raise_for_status()
            d = r.json()
            if d.get("status") == 200:
                return d.get("data", [])
            return []
        except Exception as e:
            print(f"  [retry {i+1}] FinMind {dataset}/{data_id} → {e}")
            time.sleep(1.5)
    return []

def fetch_minute_price(sid, today_str):
    """
    TaiwanStockPriceMinute：盤中分K（~10分鐘延遲），取最後一筆當即時價。
    start_date/end_date 必須是今天日期（YYYY-MM-DD）。
    """
    rows = finmind_get("TaiwanStockPriceMinute", sid, today_str, today_str)
    if not rows:
        return None
    # 取最後一筆（最新時間）
    last = sorted(rows, key=lambda x: x.get("date",""))[-1]
    close  = last.get("close", 0)
    open_  = last.get("open",  0)
    high   = last.get("max",   0)   # FinMind 欄位：max/min
    low    = last.get("min",   0)
    volume = last.get("volume",0)
    if not close or close == 0:
        return None
    return {
        "price":  str(close),
        "open":   str(open_),
        "high":   str(high),
        "low":    str(low),
        "vol":    str(volume),
        "ts":     str(last.get("date","")),
        "source": "finmind_minute",
    }

def fetch_daily_price(sid, today_str):
    """
    TaiwanStockPrice：當日收盤價（收盤後才有），作為備用。
    """
    rows = finmind_get("TaiwanStockPrice", sid, today_str, today_str)
    if not rows:
        return None
    row   = rows[-1]
    close = row.get("close", 0)
    if not close or close == 0:
        return None
    spread = row.get("spread", 0) or 0
    prev_c = round(float(close) - float(spread), 2) if spread else float(close)
    return {
        "price":  str(close),
        "prev":   str(prev_c),
        "open":   str(row.get("open", close)),
        "high":   str(row.get("max",  close)),
        "low":    str(row.get("min",  close)),
        "vol":    str(row.get("Trading_Volume", 0)),
        "ts":     str(row.get("date","")),
        "source": "finmind_daily",
    }

def main():
    now      = tw_now()
    now_str  = now.strftime("%Y/%m/%d %H:%M:%S")
    today    = now.strftime("%Y-%m-%d")
    trading  = is_trading_now()
    errors   = []

    print(f"[{now_str} 台灣時間] fetch_live V9 (交易中: {trading})")
    print(f"  FinMind token: {'已設定' if FINMIND_TOKEN else '未設定（匿名）'}")

    # 讀取候選股清單
    if not os.path.exists(STOCKS_PATH):
        errors.append("stocks.json 不存在，請先執行每日盤後抓資料")
        _write_out({}, now_str, now.strftime("%Y%m%d"), trading, errors)
        return

    with open(STOCKS_PATH, encoding="utf-8") as f:
        stocks_data = json.load(f)

    stocks = stocks_data.get("stocks", [])
    if not stocks:
        errors.append("stocks.json 無候選股（尚未執行盤後篩選）")
        _write_out({}, now_str, now.strftime("%Y%m%d"), trading, errors)
        return

    sids    = [s["sid"]               for s in stocks]
    names   = {s["sid"]: s["name"]    for s in stocks}
    markets = {s["sid"]: s.get("market","tse") for s in stocks}
    prevs   = {s["sid"]: s["close"]   for s in stocks}  # 昨日收盤（stocks.json 紀錄的）
    print(f"  → 候選股：{', '.join(sids)}")

    live = {}
    success_count = 0

    for sid in sids:
        p = None

        if trading:
            # 盤中：先試分K即時
            p = fetch_minute_price(sid, today)
            time.sleep(0.3)

        if p is None:
            # 非盤中 or 分K無資料：試當日收盤
            p = fetch_daily_price(sid, today)
            time.sleep(0.3)

        if p is None:
            # 都沒有：顯示昨日收盤（static fallback）
            prev_close = prevs.get(sid, "-")
            live[sid] = {
                "price":  str(prev_close),
                "prev":   str(prev_close),
                "open":   "-",
                "high":   "-",
                "low":    "-",
                "vol":    "0",
                "name":   names.get(sid, sid),
                "market": markets.get(sid, "tse"),
                "source": "fallback_yesterday",
            }
            print(f"  {sid} {names.get(sid,sid):8s}  無今日資料，顯示昨收 {prev_close}")
            continue

        p["prev"]   = str(prevs.get(sid, p.get("price", "-")))
        p["name"]   = names.get(sid, sid)
        p["market"] = markets.get(sid, "tse")
        live[sid]   = p
        success_count += 1

        price = p.get("price", "-")
        prev  = p.get("prev",  "-")
        src   = p.get("source", "?")
        try:
            chg = f"+{(float(price)-float(prev))/float(prev)*100:.2f}%" \
                  if prev not in ("-","0","0.0") else "--"
        except:
            chg = "--"
        print(f"  {markets.get(sid,'?')} {sid} {names.get(sid,sid):8s} {str(price):>8s}  {chg}  [{src}]")

    print(f"  ✅ 取得 {success_count}/{len(sids)} 筆有效報價")

    if success_count == 0:
        errors.append("FinMind 無法取得今日報價，可能是非交易日或 API 暫時異常")

    _write_out(live, now_str, now.strftime("%Y%m%d"), trading, errors)

def _write_out(prices, now_str, trade_date, trading, errors):
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
    print(f"  💾 live.json 寫入完成（{len(prices)} 筆）")

if __name__ == "__main__":
    main()
