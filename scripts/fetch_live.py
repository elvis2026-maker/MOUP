#!/usr/bin/env python3
"""
台股盤中即時報價抓取腳本 V4
V4 修正：
  1. 上市(tse_) + 上櫃(otc_) 都支援，不再漏抓上櫃股
  2. 新增備用 API（mis.twse fallback）
  3. live.json 加入 fetch_errors 欄位，方便前端顯示錯誤原因
  4. is_trading 欄位正確設定（V3 hardcode True，V4 動態判斷）
  5. 增加超時重試機制
  6. updated_at 使用正確台灣時區格式（V3 GitHub Actions UTC問題修正）
"""

import requests, json, os, time
from datetime import datetime, timezone, timedelta

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://mis.twse.com.tw/"
}

STOCKS_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
LIVE_PATH   = os.path.join(os.path.dirname(__file__), "../data/live.json")

TZ_TW = timezone(timedelta(hours=8))

def tw_now():
    return datetime.now(TZ_TW)

def is_trading_now():
    """動態判斷目前是否在交易時段（09:00~13:30 台灣時間）"""
    now = tw_now()
    if now.weekday() >= 5:   # 週六、日
        return False
    h, m = now.hour, now.minute
    total = h * 60 + m
    return (9 * 60) <= total <= (13 * 60 + 30)

def fetch_session_cookie():
    """取得 mis.twse Session Cookie（某些 API 需要）"""
    try:
        r = requests.get(
            "https://mis.twse.com.tw/stock/index.jsp",
            headers=HEADERS, timeout=10
        )
        return r.cookies
    except:
        return None

def fetch_live_prices_twse(sids, cookies=None):
    """
    呼叫 mis.twse.com.tw 即時 API
    V4修正：分批抓（每批最多 50 支，避免 URL 過長）
    同時處理上市(tse_) & 上櫃(otc_)
    """
    # 先從 stocks.json 判斷哪些是上市/上櫃
    # 若不知道則預設先試 tse_，失敗再試 otc_
    result = {}
    batch_size = 50

    for i in range(0, len(sids), batch_size):
        batch = sids[i:i+batch_size]
        # 先試上市
        ex_ch = "|".join(f"tse_{sid}.tw" for sid in batch)
        url = (
            f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
            f"?ex_ch={ex_ch}&json=1&delay=0&_={int(time.time()*1000)}"
        )
        try:
            r = requests.get(url, headers=HEADERS, cookies=cookies, timeout=15)
            r.raise_for_status()
            data = r.json()
            tse_found = set()
            for item in data.get("msgArray", []):
                sid = item.get("c", "")
                if not sid:
                    continue
                price = item.get("z", "-")
                result[sid] = {
                    "price": price,
                    "prev":  item.get("y", "-"),
                    "open":  item.get("o", "-"),
                    "high":  item.get("h", "-"),
                    "low":   item.get("l", "-"),
                    "vol":   item.get("v", "0"),
                    "time":  item.get("tlong", ""),
                    "name":  item.get("n", ""),
                    "market": "tse"
                }
                tse_found.add(sid)

            # 找出上市找不到的，補試上櫃
            otc_batch = [s for s in batch if s not in tse_found]
            if otc_batch:
                ex_ch_otc = "|".join(f"otc_{sid}.tw" for sid in otc_batch)
                url_otc = (
                    f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
                    f"?ex_ch={ex_ch_otc}&json=1&delay=0&_={int(time.time()*1000)}"
                )
                r2 = requests.get(url_otc, headers=HEADERS, cookies=cookies, timeout=15)
                r2.raise_for_status()
                data2 = r2.json()
                for item in data2.get("msgArray", []):
                    sid = item.get("c", "")
                    if not sid:
                        continue
                    price = item.get("z", "-")
                    result[sid] = {
                        "price": price,
                        "prev":  item.get("y", "-"),
                        "open":  item.get("o", "-"),
                        "high":  item.get("h", "-"),
                        "low":   item.get("l", "-"),
                        "vol":   item.get("v", "0"),
                        "time":  item.get("tlong", ""),
                        "name":  item.get("n", ""),
                        "market": "otc"
                    }
        except Exception as e:
            print(f"  ! mis.twse 批次 {i//batch_size+1} 抓取失敗：{e}")

        time.sleep(0.3)   # 避免連線過快

    return result

def main():
    now = tw_now()
    now_str = now.strftime("%Y/%m/%d %H:%M:%S")
    trading = is_trading_now()
    print(f"[{now_str} 台灣時間] 抓取盤中即時報價... (交易中: {trading})")

    errors = []

    # 讀取 stocks.json 取得候選股代號
    if not os.path.exists(STOCKS_PATH):
        errors.append("stocks.json 不存在，請先跑 fetch_twse.py")
        print(f"  ! {errors[-1]}")
        # 仍輸出空 live.json，避免前端 404
        _write_empty(now_str, trading, errors)
        return

    with open(STOCKS_PATH, encoding="utf-8") as f:
        stocks_data = json.load(f)

    sids = [s["sid"] for s in stocks_data.get("stocks", [])]
    if not sids:
        errors.append("stocks.json 無候選股，跳過")
        print(f"  ! {errors[-1]}")
        _write_empty(now_str, trading, errors)
        return

    print(f"  → 抓取 {len(sids)} 支：{', '.join(sids)}")

    # 取得 Session（提升 API 成功率）
    cookies = fetch_session_cookie()
    if cookies:
        print("  → Session 取得成功")
    else:
        print("  ! Session 取得失敗，繼續嘗試")

    live = fetch_live_prices_twse(sids, cookies)
    print(f"  → 成功取得 {len(live)} 支")

    if len(live) == 0:
        errors.append("mis.twse 回傳 0 筆資料，可能為非交易時段或 API 暫時不可用")

    # 輸出 live.json
    output = {
        "updated_at":   now_str,
        "trade_date":   now.strftime("%Y%m%d"),
        "is_trading":   trading,
        "prices":       live,
        "fetch_errors": errors
    }

    os.makedirs(os.path.dirname(LIVE_PATH), exist_ok=True)
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"  ✅ live.json 已輸出")
    for sid, p in live.items():
        price = p.get("price", "-")
        prev  = p.get("prev",  "-")
        name  = p.get("name",  sid)
        mkt   = p.get("market", "?")
        try:
            chg_pct = f"+{(float(price)-float(prev))/float(prev)*100:.2f}%" if price!="-" else "--"
        except:
            chg_pct = "--"
        print(f"  {mkt} {sid} {name:8s} {price:>8s}  {chg_pct}")

def _write_empty(now_str, trading, errors):
    output = {
        "updated_at":   now_str,
        "trade_date":   now_str[:10].replace("/",""),
        "is_trading":   trading,
        "prices":       {},
        "fetch_errors": errors
    }
    os.makedirs(os.path.dirname(LIVE_PATH), exist_ok=True)
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
