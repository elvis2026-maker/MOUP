#!/usr/bin/env python3
"""
台股盤中即時報價抓取腳本 V8
==============================
V8 修正：
  Bug3 修正：mis.twse 需要 Session 認證在 GitHub Actions 境外 IP 可能被擋
    → 改用 openapi.twse.com.tw 作為主要來源（無需Session，境外IP可用）
    → mis.twse 降為備用
  同時支援：
    openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL（主要，今日行情）
    mis.twse.com.tw/stock/api/getStockInfo.jsp（備用，盤中即時）
"""

import requests, json, os, time
from datetime import datetime, timezone, timedelta

TZ_TW       = timezone(timedelta(hours=8))
STOCKS_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
LIVE_PATH   = os.path.join(os.path.dirname(__file__), "../data/live.json")

HEADERS_OPENAPI = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}
HEADERS_MIS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": "https://mis.twse.com.tw/stock/fibest.htm",
    "X-Requested-With": "XMLHttpRequest",
}

def tw_now():
    return datetime.now(TZ_TW)

def is_trading_now():
    now = tw_now()
    if now.weekday() >= 5: return False
    total = now.hour * 60 + now.minute
    return (9 * 60) <= total <= (13 * 60 + 30)

# ── V8主要方式：openapi.twse.com.tw（無需Session，境外IP可用）──
def fetch_openapi_twse(sids):
    """
    openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
    一次取得所有上市股票今日行情
    """
    result = {}
    sid_set = set(sids)
    try:
        r = requests.get(
            "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
            headers=HEADERS_OPENAPI, timeout=20
        )
        r.raise_for_status()
        data = r.json()
        for item in data:
            sid = str(item.get("Code","")).strip()
            if sid not in sid_set: continue
            close = str(item.get("ClosingPrice","-")).replace(",","").strip()
            open_ = str(item.get("OpeningPrice","-")).replace(",","").strip()
            high  = str(item.get("HighestPrice","-")).replace(",","").strip()
            low   = str(item.get("LowestPrice","-")).replace(",","").strip()
            vol   = str(item.get("TradeVolume","0")).replace(",","").strip()
            chg   = str(item.get("Change","0")).replace(",","").replace("+","").strip()
            name  = str(item.get("Name",sid)).strip()

            # 昨收 = close - change
            try:
                prev = str(round(float(close) - float(chg), 2)) if close not in ("-","--") else "-"
            except:
                prev = "-"

            result[sid] = {
                "price": close,
                "prev":  prev,
                "open":  open_,
                "high":  high,
                "low":   low,
                "vol":   vol,
                "name":  name,
                "market":"tse",
                "source":"openapi",
            }
        print(f"  → openapi.twse 取得 {len(result)} 筆（上市）")
    except Exception as e:
        print(f"  ! openapi.twse 失敗：{e}")
    return result

def fetch_openapi_tpex(sids):
    """
    openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL_TPEX（上櫃）
    """
    result = {}
    sid_set = set(sids)
    try:
        r = requests.get(
            "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL_TPEX",
            headers=HEADERS_OPENAPI, timeout=20
        )
        r.raise_for_status()
        data = r.json()
        for item in data:
            sid = str(item.get("Code","")).strip()
            if sid not in sid_set: continue
            close = str(item.get("Close","-")).replace(",","").strip()
            open_ = str(item.get("Open","-")).replace(",","").strip()
            high  = str(item.get("High","-")).replace(",","").strip()
            low   = str(item.get("Low","-")).replace(",","").strip()
            vol   = str(item.get("Volume","0")).replace(",","").strip()
            chg   = str(item.get("Change","0")).replace(",","").replace("+","").strip()
            name  = str(item.get("Name",sid)).strip()
            try:
                prev = str(round(float(close) - float(chg), 2)) if close not in ("-","--") else "-"
            except:
                prev = "-"
            result[sid] = {
                "price": close, "prev": prev,
                "open": open_, "high": high, "low": low,
                "vol": vol, "name": name, "market": "otc", "source": "openapi_tpex",
            }
        print(f"  → openapi.twse tpex 取得 {len(result)} 筆（上櫃）")
    except Exception as e:
        print(f"  ! openapi tpex 失敗：{e}")
    return result

# ── V8備用方式：mis.twse.com.tw（盤中即時，需要 Session）──
def fetch_mis_twse(sids, markets):
    result = {}
    sess = requests.Session()
    sess.headers.update(HEADERS_MIS)
    try:
        sess.get("https://mis.twse.com.tw/stock/fibest.htm", timeout=12)
        time.sleep(0.5)
    except Exception as e:
        print(f"  ! mis.twse session 失敗：{e}")
        return result

    batch_size = 20
    for i in range(0, len(sids), batch_size):
        batch = sids[i:i+batch_size]
        parts = [f"{'otc' if markets.get(s)=='otc' else 'tse'}_{s}.tw" for s in batch]
        url = (f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
               f"?ex_ch={'|'.join(parts)}&json=1&delay=0&_={int(time.time()*1000)}")
        try:
            r = sess.get(url, timeout=15)
            r.raise_for_status()
            arr = r.json().get("msgArray",[])
            print(f"  → mis 批次{i//batch_size+1}：{len(arr)} 筆")
            for item in arr:
                sid = item.get("c","")
                if not sid: continue
                result[sid] = {
                    "price": item.get("z","-"),
                    "prev":  item.get("y","-"),
                    "open":  item.get("o","-"),
                    "high":  item.get("h","-"),
                    "low":   item.get("l","-"),
                    "vol":   item.get("v","0"),
                    "name":  item.get("n",""),
                    "market": "otc" if item.get("ex","")=="otc" else "tse",
                    "source": "mis",
                }
        except Exception as e:
            print(f"  ! mis 批次{i//batch_size+1} 失敗：{e}")
        time.sleep(0.4)
    return result

def main():
    now     = tw_now()
    now_str = now.strftime("%Y/%m/%d %H:%M:%S")
    trading = is_trading_now()
    print(f"[{now_str} 台灣時間] fetch_live V8 (交易中: {trading})")

    errors = []

    if not os.path.exists(STOCKS_PATH):
        errors.append("stocks.json 不存在，請確認每日排程是否正常")
        _write_out({}, now_str, now.strftime("%Y%m%d"), trading, errors)
        return

    with open(STOCKS_PATH, encoding="utf-8") as f:
        stocks_data = json.load(f)

    stocks  = stocks_data.get("stocks", [])
    if not stocks:
        errors.append("stocks.json 無候選股，每日排程可能未正常執行")
        _write_out({}, now_str, now.strftime("%Y%m%d"), trading, errors)
        return

    sids    = [s["sid"] for s in stocks]
    markets = {s["sid"]: s.get("market","tse") for s in stocks}
    print(f"  → 候選股：{', '.join(sids)}")

    # ── V8主要方式：openapi.twse（上市+上櫃分開抓）──
    tse_sids = [s for s in sids if markets.get(s,"tse")=="tse"]
    otc_sids = [s for s in sids if markets.get(s,"tse")=="otc"]

    live = {}
    if tse_sids:
        live.update(fetch_openapi_twse(tse_sids))
    if otc_sids:
        live.update(fetch_openapi_tpex(otc_sids))
        time.sleep(0.3)

    # 檢查是否取到有效資料（-或--表示停牌/收盤後）
    valid = {sid: p for sid, p in live.items()
             if p.get("price") not in ("-","--","","0",None)}

    # ── V8備用：盤中且 openapi 無有效資料時，嘗試 mis.twse ──
    if trading and len(valid) < len(sids) // 2:
        print("  ! openapi 有效資料不足，嘗試 mis.twse...")
        missing = [s for s in sids if s not in valid]
        mis_result = fetch_mis_twse(missing, markets)
        live.update(mis_result)
        valid = {sid: p for sid, p in live.items()
                 if p.get("price") not in ("-","--","","0",None)}

    print(f"  ✅ 最終有效報價：{len(valid)}/{len(sids)} 筆")

    for sid, p in sorted(live.items()):
        price = p.get("price","-")
        prev  = p.get("prev","-")
        name  = p.get("name",sid)
        try:
            chg = f"+{(float(price)-float(prev))/float(prev)*100:.2f}%" \
                  if price not in ("-","--") and prev not in ("-","--","0") else "--"
        except:
            chg = "--"
        print(f"  {p.get('market','?')} {sid} {name:8s} {str(price):>8s}  {chg}  [{p.get('source','?')}]")

    if len(valid) == 0 and not trading:
        errors = ["非交易時段，盤中資料將於次日 09:05 後自動更新"]
    elif len(valid) == 0:
        errors.append("所有 API 均無法取得有效報價，請稍後重試")

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
