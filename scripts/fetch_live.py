#!/usr/bin/env python3
"""
台股盤中即時報價抓取腳本 V5
V5 修正：
  1. 非交易時段 prices={} 為正常現象，不寫入誤導性錯誤訊息
  2. 錯誤訊息更清楚，區分「非交易時段」和「API 失敗」
  3. 原 V4.1 所有修正保留：Session 機制、is_trading 動態判斷、備用 API
"""

import requests, json, os, time
from datetime import datetime, timezone, timedelta

TZ_TW = timezone(timedelta(hours=8))

# mis.twse 需要帶完整 Header + 正確 Referer
HEADERS_MIS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": "https://mis.twse.com.tw/stock/fibest.htm",
    "X-Requested-With": "XMLHttpRequest",
}

HEADERS_OPENAPI = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

STOCKS_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
LIVE_PATH   = os.path.join(os.path.dirname(__file__), "../data/live.json")

def tw_now():
    return datetime.now(TZ_TW)

def is_trading_now():
    now = tw_now()
    if now.weekday() >= 5:
        return False
    total = now.hour * 60 + now.minute
    return (9 * 60) <= total <= (13 * 60 + 30)

def get_mis_session():
    """
    mis.twse 必須先造訪 fibest.htm 才能取得有效 jsessionid
    否則 getStockInfo.jsp 回傳空 msgArray
    """
    sess = requests.Session()
    sess.headers.update(HEADERS_MIS)
    try:
        # Step 1: 打首頁拿 session
        sess.get("https://mis.twse.com.tw/stock/fibest.htm", timeout=12)
        time.sleep(0.5)
        print("  → mis.twse session 取得成功")
    except Exception as e:
        print(f"  ! mis.twse session 失敗：{e}")
    return sess

def fetch_mis_twse(sess, sids, markets):
    """
    呼叫 mis.twse getStockInfo，批次處理
    markets: dict {sid: 'tse'|'otc'}
    """
    result = {}
    batch_size = 30  # 縮小批次，URL 不要太長

    for i in range(0, len(sids), batch_size):
        batch = sids[i:i+batch_size]
        parts = []
        for sid in batch:
            mkt = markets.get(sid, 'tse')
            prefix = 'otc' if mkt == 'otc' else 'tse'
            parts.append(f"{prefix}_{sid}.tw")

        ex_ch = "|".join(parts)
        url = (
            f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
            f"?ex_ch={ex_ch}&json=1&delay=0&_={int(time.time()*1000)}"
        )
        try:
            r = sess.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
            arr = data.get("msgArray", [])
            print(f"  → 批次 {i//batch_size+1}: 回傳 {len(arr)} 筆")
            for item in arr:
                sid = item.get("c", "")
                if not sid:
                    continue
                result[sid] = {
                    "price": item.get("z", "-"),
                    "prev":  item.get("y", "-"),
                    "open":  item.get("o", "-"),
                    "high":  item.get("h", "-"),
                    "low":   item.get("l", "-"),
                    "vol":   item.get("v", "0"),
                    "time":  item.get("tlong", ""),
                    "name":  item.get("n", ""),
                    "market": "otc" if item.get("ex","") == "otc" else "tse",
                }
        except Exception as e:
            print(f"  ! 批次 {i//batch_size+1} 失敗：{e}")
        time.sleep(0.4)

    return result

def fetch_openapi_fallback(sids):
    """
    備用 API：openapi.twse.com.tw（無需 Session，但只有上市收盤價，非盤中）
    僅用於 mis.twse 完全失敗時的降級方案
    """
    result = {}
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        r = requests.get(url, headers=HEADERS_OPENAPI, timeout=20)
        r.raise_for_status()
        data = r.json()
        sid_set = set(sids)
        for item in data:
            sid = item.get("Code", "")
            if sid not in sid_set:
                continue
            close = item.get("ClosingPrice", "-")
            open_ = item.get("OpeningPrice", "-")
            high  = item.get("HighestPrice", "-")
            low   = item.get("LowestPrice",  "-")
            vol   = item.get("TradeVolume",  "0")
            result[sid] = {
                "price": close,   # 昨日收盤當暫用
                "prev":  close,
                "open":  open_,
                "high":  high,
                "low":   low,
                "vol":   str(int(vol.replace(",","")) // 1000) if vol.replace(",","").isdigit() else "0",
                "time":  "",
                "name":  item.get("Name", sid),
                "market": "tse",
                "is_prev_close": True,  # 標記是昨日收盤，非盤中
            }
        print(f"  → openapi 備用取得 {len(result)} 筆（為昨日收盤，非即時）")
    except Exception as e:
        print(f"  ! openapi 備用也失敗：{e}")
    return result

def main():
    now = tw_now()
    now_str = now.strftime("%Y/%m/%d %H:%M:%S")
    trading = is_trading_now()
    print(f"[{now_str} 台灣時間] 抓取盤中即時報價 (交易中: {trading})")

    errors = []

    if not os.path.exists(STOCKS_PATH):
        errors.append("stocks.json 不存在，請先執行 fetch_twse.py")
        _write_out({}, now_str, now.strftime("%Y%m%d"), trading, errors)
        return

    with open(STOCKS_PATH, encoding="utf-8") as f:
        stocks_data = json.load(f)

    stocks = stocks_data.get("stocks", [])
    if not stocks:
        errors.append("stocks.json 無候選股，請確認每日盤後抓資料 workflow 是否正常執行")
        _write_out({}, now_str, now.strftime("%Y%m%d"), trading, errors)
        return

    sids    = [s["sid"]              for s in stocks]
    markets = {s["sid"]: s.get("market", "tse") for s in stocks}
    print(f"  → 候選股：{', '.join(sids)}")

    # V5修正：非交易時段仍嘗試抓取（可能有盤後零星成交），但明確標記
    if not trading:
        print("  ⚠ 目前非交易時段，嘗試取得最近成交價...")

    # 主要方式：mis.twse（需要 Session）
    sess = get_mis_session()
    live = fetch_mis_twse(sess, sids, markets)

    if len(live) == 0:
        msg = "mis.twse 回傳 0 筆（可能非交易時段或 API 暫時不可用），改用 openapi 備用"
        print(f"  ! {msg}")
        errors.append(msg)
        live = fetch_openapi_fallback(sids)

    if len(live) == 0:
        errors.append("所有 API 均無法取得報價（非交易時段為正常現象）")
    
    # V5修正：非交易時段 prices 為空是正常的，不算錯誤
    if not trading and len(live) == 0:
        errors = ["非交易時段，盤中資料將在 09:05 後自動更新"]

    print(f"  ✅ 最終取得 {len(live)} 筆")
    for sid, p in live.items():
        price = p.get("price", "-")
        prev  = p.get("prev",  "-")
        name  = p.get("name",  sid)
        try:
            chg = f"+{(float(price)-float(prev))/float(prev)*100:.2f}%" if price not in ("-","--") else "--"
        except:
            chg = "--"
        print(f"  {p.get('market','?')} {sid} {name:8s} {price:>8s}  {chg}")

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
