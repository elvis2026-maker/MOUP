#!/usr/bin/env python3
"""
台股盤中即時報價抓取腳本 V3
由 GitHub Actions 在交易時段每小時執行一次
優點：伺服器端執行，無瀏覽器 CORS 限制，可直接呼叫 mis.twse.com.tw
輸出：data/live.json（前端直接讀，無 CORS 問題）
"""

import requests, json, os, time
from datetime import datetime, timezone, timedelta

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://mis.twse.com.tw/"
}

STOCKS_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
LIVE_PATH   = os.path.join(os.path.dirname(__file__), "../data/live.json")

def tw_now():
    tz_tw = timezone(timedelta(hours=8))
    return datetime.now(tz_tw)

def fetch_live_prices(sids):
    """
    呼叫 mis.twse.com.tw 即時 API（5秒更新）
    伺服器端執行 → 無 CORS 限制
    欄位說明：
      c=代號, n=名稱, z=成交價, y=昨收, o=開盤, h=最高, l=最低
      v=累積成交量(千股), tv=成交量(張?), tlong=時間戳記
    """
    # 上市用 tse_, 上櫃用 otc_（此處先全部用 tse_）
    ex_ch = "|".join(f"tse_{sid}.tw" for sid in sids)
    url   = (
        f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
        f"?ex_ch={ex_ch}&json=1&delay=0&_={int(time.time()*1000)}"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        result = {}
        for item in data.get("msgArray", []):
            sid   = item.get("c", "")
            price = item.get("z", "-")   # 成交價（盤中），"-" 表示尚未成交
            if not sid:
                continue
            result[sid] = {
                "price":    price,         # 成交價，"-" 時代表集合競價或未成交
                "prev":     item.get("y", "-"),  # 昨收
                "open":     item.get("o", "-"),  # 開盤
                "high":     item.get("h", "-"),  # 最高
                "low":      item.get("l", "-"),  # 最低
                "vol":      item.get("v", "0"),  # 累積成交量（張）
                "time":     item.get("tlong", ""),  # 時間戳記（ms）
                "name":     item.get("n", ""),
            }
        return result
    except Exception as e:
        print(f"  ! mis.twse 抓取失敗：{e}")
        return {}

def main():
    now = tw_now()
    now_str = now.strftime("%Y/%m/%d %H:%M:%S")
    print(f"[{now_str} 台灣時間] 抓取盤中即時報價...")

    # 讀取 stocks.json 取得候選股代號
    if not os.path.exists(STOCKS_PATH):
        print("  ! stocks.json 不存在，請先跑 fetch_twse.py")
        return

    with open(STOCKS_PATH, encoding="utf-8") as f:
        stocks_data = json.load(f)

    sids = [s["sid"] for s in stocks_data.get("stocks", [])]
    if not sids:
        print("  ! 無候選股，跳過")
        return

    print(f"  → 抓取 {len(sids)} 支：{', '.join(sids)}")

    live = fetch_live_prices(sids)
    print(f"  → 成功取得 {len(live)} 支")

    # 輸出 live.json
    output = {
        "updated_at": now_str,
        "trade_date": now.strftime("%Y%m%d"),
        "is_trading": True,   # GitHub Actions 只在交易時段跑
        "prices": live        # { "2330": { price, prev, open, high, low, vol, time, name }, ... }
    }

    os.makedirs(os.path.dirname(LIVE_PATH), exist_ok=True)
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"  ✅ live.json 已輸出")
    for sid, p in live.items():
        price = p.get("price", "-")
        prev  = p.get("prev",  "-")
        name  = p.get("name",  sid)
        try:
            chg_pct = f"+{(float(price)-float(prev))/float(prev)*100:.2f}%" if price!="-" else "--"
        except:
            chg_pct = "--"
        print(f"  {sid} {name:8s} {price:>8s}  {chg_pct}")

if __name__ == "__main__":
    main()
