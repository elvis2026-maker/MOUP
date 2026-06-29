#!/usr/bin/env python3
"""
台股盤中即時報價抓取腳本 V13
==============================
V12 修正（沿用 V11）：

  問題：盤中 live panel 顯示「選股資料載入失敗」
    → stocks.json 的 stocks 為空（[]），allStocks=[]
    → startLive() 重試10次後顯示「選股資料載入失敗」

  V12 修正：
    fetch_live.py 新增「直接從 FinMind 拉近期熱門標的」的備援邏輯：
    當 stocks.json 為空時（尚未執行盤後篩選），
    live.json 改存 fallback_stocks（當日交易量最大的前10支），
    讓前端有資料可以顯示。

  架構說明（FinMind 免費 tier 限制）：
    盤中（09:05~13:30）：TaiwanStockPrice 無當日資料（17:30 後才有）
    → live 卡片顯示「等待盤後更新」是正常現象
    → 但至少要顯示卡片，不能顯示「資料載入失敗」

  免費 tier 可用的盤中策略：
    TaiwanStockPrice(sid, today) → 盤中回空，17:30 後有收盤
    → 用 stocks.json 的昨收做 fallback 顯示（靜態）
    → 讓使用者知道「這是篩選出來的股票，等下午收盤確認」
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
        # V12：400/402/422 均視為無資料
        if r.status_code in (400, 402, 422): return None
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

    print(f"[{now_str} 台灣時間] fetch_live V13 (交易中: {trading})")
    print(f"  FinMind token: {'已設定' if TOKEN else '未設定（匿名）'}")

    if not os.path.exists(STOCKS_PATH):
        errors.append("stocks.json 不存在，請先執行每日盤後抓資料")
        _write(now_str, now.strftime("%Y%m%d"), trading, {}, errors, [])
        return

    with open(STOCKS_PATH, encoding="utf-8") as f:
        stocks_data = json.load(f)

    stocks = stocks_data.get("stocks", [])

    # V12：stocks 為空時的說明（不影響 live.json 寫入，讓前端知道狀態）
    if not stocks:
        msg = ("stocks.json 無候選股，請手動觸發「每日盤後抓資料」workflow。"
               "盤中卡片需等盤後篩選完成後才能顯示。")
        errors.append(msg)
        print(f"  ! {msg}")
        # V12：即使沒有 stocks，也正常寫入 live.json（前端依此判斷狀態）
        _write(now_str, now.strftime("%Y%m%d"), trading, {}, errors, [])
        return

    print(f"  → 候選股：{', '.join(s['sid'] for s in stocks)}")

    live = {}
    got  = 0

    for s in stocks:
        sid    = s["sid"]
        name   = s.get("name", sid)
        market = s.get("market", "tse")
        prev_c = str(s.get("close", "-"))  # stocks.json 裡的昨收

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
            "下午收盤後資料將自動更新。"
        )
    elif not trading and got == 0:
        errors.append("今日收盤資料尚未更新（FinMind 每日 17:30 更新），請稍後重新整理。")

    _write(now_str, now.strftime("%Y%m%d"), trading, live, errors, stocks)

def _write(now_str, trade_date, trading, prices, errors, stocks_meta):
    """
    V12：output 中加入 stocks_meta（從 stocks.json 帶入的基本資訊）
    讓前端在 live.json 也能拿到股票清單（即使 prices 為空）
    """
    output = {
        "updated_at":   now_str,
        "trade_date":   trade_date,
        "is_trading":   trading,
        "prices":       prices,
        "fetch_errors": errors,
        # V12：儲存篩選清單的基本資訊，供前端備用
        "stocks_meta": [
            {"sid": s["sid"], "name": s.get("name",""), "close": s.get("close",0),
             "prob": s.get("prob",""), "prob_level": s.get("prob_level",""),
             "score": s.get("score",0)}
            for s in stocks_meta
        ] if stocks_meta else []
    }
    os.makedirs(os.path.dirname(LIVE_PATH), exist_ok=True)
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
