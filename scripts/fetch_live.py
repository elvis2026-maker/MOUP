#!/usr/bin/env python3
"""
台股盤中即時報價抓取腳本 V9
================================
V9 根本修正：
  V8問題：stocks.json 永遠是空的（因 fetch_twse 失敗）
    → 導致 fetch_live 讀到空清單，live.json 永遠是 0 筆

  V9 即時報價策略：
    主要：FinMind taiwan_stock_tick_snapshot API
      - URL: https://api.finmindtrade.com/api/v4/taiwan_stock_tick_snapshot
      - 優點：真正即時快照（非日彙整），不需要 Session，境外 IP 可用
      - 只查10支股票，10 req/次，遠低於免費 300 req/hr 限制
      - 不需要 token 也可以用（免費額度足夠）

    備用：openapi.twse.com.tw STOCK_DAY_ALL
      - 盤後彙整，盤中資料不完整，但至少有昨日收盤
"""

import requests, json, os, time
from datetime import datetime, timezone, timedelta

TZ_TW       = timezone(timedelta(hours=8))
STOCKS_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
LIVE_PATH   = os.path.join(os.path.dirname(__file__), "../data/live.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
}

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

def tw_now():
    return datetime.now(TZ_TW)

def is_trading_now():
    now = tw_now()
    if now.weekday() >= 5: return False
    total = now.hour * 60 + now.minute
    return (9 * 60) <= total <= (13 * 60 + 30)

# ── V9 主要：FinMind tick snapshot（真正即時）──────────
def fetch_finmind_snapshot(sids):
    """
    FinMind taiwan_stock_tick_snapshot
    每支獨立呼叫 or 批次（data_id 可傳 list 但 URL 格式要注意）
    每次只查 10 支，完全在免費額度內
    """
    result = {}
    url    = "https://api.finmindtrade.com/api/v4/taiwan_stock_tick_snapshot"
    
    # 分批，每批 10 支
    for i in range(0, len(sids), 10):
        batch = sids[i:i+10]
        params = {"data_id": ",".join(batch)}
        if FINMIND_TOKEN: params["token"] = FINMIND_TOKEN
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            d = r.json()
            rows = d.get("data", [])
            print(f"  → FinMind snapshot 批次{i//10+1}: {len(rows)} 筆")
            for row in rows:
                sid   = str(row.get("stock_id","")).strip()
                price = str(row.get("close","") or row.get("price","") or "-")
                open_ = str(row.get("open","") or "-")
                high  = str(row.get("max","") or row.get("high","") or "-")
                low   = str(row.get("min","") or row.get("low","") or "-")
                vol   = str(row.get("volume","0") or "0")
                prev  = str(row.get("yesterday_price","") or "-")
                name  = str(row.get("name","") or sid)
                if not sid: continue
                result[sid] = {
                    "price": price if price else "-",
                    "prev":  prev  if prev  else "-",
                    "open":  open_ if open_ else "-",
                    "high":  high  if high  else "-",
                    "low":   low   if low   else "-",
                    "vol":   vol,
                    "name":  name,
                    "market":"tse",
                    "source":"finmind_snapshot",
                }
        except Exception as e:
            print(f"  ! FinMind snapshot 批次{i//10+1} 失敗：{e}")
        time.sleep(0.3)
    
    return result

# ── V9 備用：openapi.twse.com.tw（上市+上櫃）─────────
def fetch_openapi_twse(sids, markets):
    """openapi.twse.com.tw STOCK_DAY_ALL（盤後彙整）"""
    result  = {}
    sid_set = set(sids)

    tse_sids = [s for s in sids if markets.get(s,"tse") == "tse"]
    otc_sids = [s for s in sids if markets.get(s,"tse") == "otc"]

    # 上市
    if tse_sids:
        try:
            r = requests.get(
                "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
                headers=HEADERS, timeout=20)
            r.raise_for_status()
            for item in r.json():
                sid = str(item.get("Code","")).strip()
                if sid not in sid_set: continue
                close = str(item.get("ClosingPrice","-")).replace(",","").strip()
                open_ = str(item.get("OpeningPrice","-")).replace(",","").strip()
                high  = str(item.get("HighestPrice","-")).replace(",","").strip()
                low   = str(item.get("LowestPrice","-")).replace(",","").strip()
                vol   = str(item.get("TradeVolume","0")).replace(",","").strip()
                chg   = str(item.get("Change","0")).replace(",","").replace("+","").strip()
                name  = str(item.get("Name",sid)).strip()
                try:
                    prev = str(round(float(close) - float(chg), 2)) if close not in ("-","--") else "-"
                except:
                    prev = "-"
                result[sid] = {"price":close,"prev":prev,"open":open_,"high":high,
                                "low":low,"vol":vol,"name":name,"market":"tse","source":"openapi"}
            print(f"  → openapi.twse 上市: {len([s for s in result if markets.get(s)=='tse'])} 筆")
        except Exception as e:
            print(f"  ! openapi.twse 上市失敗：{e}")
        time.sleep(0.3)

    # 上櫃
    if otc_sids:
        try:
            r = requests.get(
                "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL_TPEX",
                headers=HEADERS, timeout=20)
            r.raise_for_status()
            for item in r.json():
                sid = str(item.get("Code","")).strip()
                if sid not in sid_set or sid in result: continue
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
                result[sid] = {"price":close,"prev":prev,"open":open_,"high":high,
                                "low":low,"vol":vol,"name":name,"market":"otc","source":"openapi_tpex"}
            print(f"  → openapi.twse 上櫃: {len([s for s in result if markets.get(s)=='otc'])} 筆")
        except Exception as e:
            print(f"  ! openapi.twse 上櫃失敗：{e}")

    return result

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

def main():
    now     = tw_now()
    now_str = now.strftime("%Y/%m/%d %H:%M:%S")
    trading = is_trading_now()
    print(f"[{now_str} 台灣時間] fetch_live V9 (交易中: {trading})")

    errors = []

    if not os.path.exists(STOCKS_PATH):
        errors.append("stocks.json 不存在，請先觸發「每日盤後抓資料」workflow")
        _write_out({}, now_str, now.strftime("%Y%m%d"), trading, errors)
        return

    with open(STOCKS_PATH, encoding="utf-8") as f:
        stocks_data = json.load(f)

    stocks = stocks_data.get("stocks", [])
    if not stocks:
        errors.append("stocks.json 無候選股，「每日盤後抓資料」workflow 尚未成功執行")
        _write_out({}, now_str, now.strftime("%Y%m%d"), trading, errors)
        return

    sids    = [s["sid"] for s in stocks]
    markets = {s["sid"]: s.get("market","tse") for s in stocks}
    print(f"  → 候選股：{', '.join(sids)}")

    # V9 主要：FinMind tick snapshot（即時）
    live = fetch_finmind_snapshot(sids)

    # 判斷有效筆數（price 非 "-" 或空）
    def is_valid(p):
        price = p.get("price","")
        return price and price not in ("-","--","0","")

    valid_count = sum(1 for p in live.values() if is_valid(p))
    print(f"  FinMind snapshot 有效：{valid_count}/{len(sids)}")

    # 備用：openapi（補足或 snapshot 全失敗時）
    if valid_count < len(sids) // 2:
        print("  ! FinMind snapshot 不足，補用 openapi.twse...")
        openapi_result = fetch_openapi_twse(sids, markets)
        # 合併：只補沒有或無效的
        for sid, p in openapi_result.items():
            if sid not in live or not is_valid(live[sid]):
                live[sid] = p

    valid_count = sum(1 for p in live.values() if is_valid(p))
    print(f"  ✅ 最終有效報價：{valid_count}/{len(sids)} 筆")

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

    if valid_count == 0 and not trading:
        errors = ["非交易時段，盤中資料將於次日 09:05 後自動更新"]
    elif valid_count == 0:
        errors.append("所有 API 均無法取得有效報價，請稍後重試")

    _write_out(live, now_str, now.strftime("%Y%m%d"), trading, errors)

if __name__ == "__main__":
    main()
