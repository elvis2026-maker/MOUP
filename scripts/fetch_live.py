#!/usr/bin/env python3
"""
台股盤中即時報價抓取腳本 V8
V8 修正：
  1. mis.twse 境外 IP 封鎖問題：改用多重備援策略
     主要：TWSE openapi /v1/exchangeReport/STOCK_DAY_ALL（境外可連，每日更新）
     盤中主要：mis.twse getStockInfo（境外可能被封，加強 Session + retry）
     盤中備援：twse openapi 即時成交（/v1/futuresoption/StockOptDailyTrade 或 ISIN）
  2. 非交易時段：直接寫入 prev_close 讓前端顯示昨收，不再顯示空白
  3. 增加 fetch_source 欄位，前端可顯示資料來源說明
"""

import requests, json, os, time
from datetime import datetime, timezone, timedelta

TZ_TW = timezone(timedelta(hours=8))

HEADERS_MIS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": "https://mis.twse.com.tw/stock/fibest.htm",
    "X-Requested-With": "XMLHttpRequest",
}

HEADERS_OPENAPI = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "zh-TW,zh;q=0.9",
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

# ── 方法1：mis.twse（盤中即時，境外可能被封） ─────────────
def get_mis_session():
    sess = requests.Session()
    sess.headers.update(HEADERS_MIS)
    try:
        r = sess.get("https://mis.twse.com.tw/stock/fibest.htm", timeout=12)
        r.raise_for_status()
        time.sleep(0.5)
        print("  → mis.twse session 取得成功")
    except Exception as e:
        print(f"  ! mis.twse session 失敗：{e}")
    return sess

def fetch_mis_twse(sess, sids, markets):
    result = {}
    batch_size = 20
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
            print(f"  → mis 批次 {i//batch_size+1}: 回傳 {len(arr)} 筆")
            for item in arr:
                sid = item.get("c", "")
                if not sid:
                    continue
                z = item.get("z", "-")
                if z in ("-", "--", ""):
                    z = item.get("y", "-")  # 若即時價空，用昨收
                result[sid] = {
                    "price":  z,
                    "prev":   item.get("y", "-"),
                    "open":   item.get("o", "-"),
                    "high":   item.get("h", "-"),
                    "low":    item.get("l", "-"),
                    "vol":    item.get("v", "0"),
                    "time":   item.get("tlong", ""),
                    "name":   item.get("n", ""),
                    "market": "otc" if item.get("ex","") == "otc" else "tse",
                    "source": "mis_live",
                }
        except Exception as e:
            print(f"  ! mis 批次 {i//batch_size+1} 失敗：{e}")
        time.sleep(0.4)
    return result

# ── 方法2：TWSE OpenAPI 上市股票（境外可連，有當日成交） ──
def fetch_twse_openapi(sids):
    """
    https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
    回傳當日（或最近交易日）所有上市股票行情，境外 IP 可連
    """
    result = {}
    sid_set = set(sids)
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        r = requests.get(url, headers=HEADERS_OPENAPI, timeout=25)
        r.raise_for_status()
        data = r.json()
        for item in data:
            sid = item.get("Code", "")
            if sid not in sid_set:
                continue
            close = item.get("ClosingPrice", "-").replace(",","").strip()
            open_ = item.get("OpeningPrice", "-").replace(",","").strip()
            high  = item.get("HighestPrice", "-").replace(",","").strip()
            low   = item.get("LowestPrice",  "-").replace(",","").strip()
            chg   = item.get("Change", "0").replace(",","").strip()
            vol   = item.get("TradeVolume", "0").replace(",","").strip()
            # prev = close - change
            try:
                prev = str(round(float(close) - float(chg), 2)) if close not in ("-","") and chg not in ("","--") else close
            except:
                prev = close
            vol_lots = str(int(vol) // 1000) if vol.lstrip("-").isdigit() else "0"
            result[sid] = {
                "price":  close if close not in ("","0") else "-",
                "prev":   prev,
                "open":   open_,
                "high":   high,
                "low":    low,
                "vol":    vol_lots,
                "time":   "",
                "name":   item.get("Name", sid),
                "market": "tse",
                "source": "twse_openapi",
            }
        print(f"  → TWSE openapi 取得 {len(result)} 筆（上市當日行情）")
    except Exception as e:
        print(f"  ! TWSE openapi 失敗：{e}")
    return result

# ── 方法3：TPEx OpenAPI 上櫃股票 ──────────────────────────
def fetch_tpex_openapi(sids):
    """
    https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes
    回傳上櫃股票每日收盤行情
    """
    result = {}
    sid_set = set(sids)
    try:
        url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
        r = requests.get(url, headers=HEADERS_OPENAPI, timeout=25)
        r.raise_for_status()
        data = r.json()
        for item in data:
            sid = str(item.get("SecuritiesCompanyCode", "")).strip()
            if sid not in sid_set:
                continue
            close = str(item.get("Close", "-")).replace(",","").strip()
            open_ = str(item.get("Open", "-")).replace(",","").strip()
            high  = str(item.get("High", "-")).replace(",","").strip()
            low   = str(item.get("Low",  "-")).replace(",","").strip()
            chg   = str(item.get("Change", "0")).replace(",","").strip()
            vol   = str(item.get("TradingShares", "0")).replace(",","").strip()
            try:
                prev = str(round(float(close) - float(chg), 2)) if close not in ("-","") and chg not in ("","--") else close
            except:
                prev = close
            vol_lots = str(int(vol) // 1000) if vol.lstrip("-").isdigit() else "0"
            result[sid] = {
                "price":  close if close not in ("","0") else "-",
                "prev":   prev,
                "open":   open_,
                "high":   high,
                "low":    low,
                "vol":    vol_lots,
                "time":   "",
                "name":   str(item.get("CompanyName", sid)).strip(),
                "market": "otc",
                "source": "tpex_openapi",
            }
        print(f"  → TPEx openapi 取得 {len(result)} 筆（上櫃當日行情）")
    except Exception as e:
        print(f"  ! TPEx openapi 失敗：{e}")
    return result

def main():
    now = tw_now()
    now_str = now.strftime("%Y/%m/%d %H:%M:%S")
    trading = is_trading_now()
    print(f"[{now_str} 台灣時間] V8 抓取報價 (交易中: {trading})")

    errors = []
    fetch_source = "twse_openapi"

    if not os.path.exists(STOCKS_PATH):
        errors.append("stocks.json 不存在，請先確認每日盤後 workflow 執行成功，且 FINMIND_TOKEN 已設定")
        _write_out({}, now_str, now.strftime("%Y%m%d"), trading, errors, fetch_source)
        return

    with open(STOCKS_PATH, encoding="utf-8") as f:
        stocks_data = json.load(f)

    stocks = stocks_data.get("stocks", [])
    if not stocks:
        errors.append("stocks.json 內無候選股，請確認每日盤後抓資料 workflow 是否正常執行")
        _write_out({}, now_str, now.strftime("%Y%m%d"), trading, errors, fetch_source)
        return

    sids    = [s["sid"]                        for s in stocks]
    markets = {s["sid"]: s.get("market","tse") for s in stocks}
    tse_sids = [s for s in sids if markets[s] == "tse"]
    otc_sids = [s for s in sids if markets[s] == "otc"]
    print(f"  → 候選股：{', '.join(sids)}")
    print(f"  → 上市：{len(tse_sids)} 支，上櫃：{len(otc_sids)} 支")

    live = {}
    mis_tried = False

    # 盤中優先嘗試 mis.twse（有逐筆即時價）
    if trading:
        print("  ► 嘗試 mis.twse 即時報價...")
        sess = get_mis_session()
        mis_result = fetch_mis_twse(sess, sids, markets)
        valid_mis = {k: v for k, v in mis_result.items() if v.get("price") not in (None, "-", "--", "")}
        print(f"  → mis.twse 有效報價：{len(valid_mis)} 筆")
        if len(valid_mis) >= len(sids) * 0.5:  # 至少取得一半才用
            live = mis_result
            fetch_source = "mis_live"
            mis_tried = True
        else:
            msg = f"mis.twse 只取得 {len(valid_mis)}/{len(sids)} 筆（可能境外 IP 被限），改用 TWSE OpenAPI"
            print(f"  ! {msg}")
            errors.append(msg)
        mis_tried = True

    # 備援（或非交易時段）：TWSE + TPEx OpenAPI
    if not live or len(live) < len(sids) * 0.5:
        print("  ► 使用 TWSE/TPEx OpenAPI 備援...")
        if tse_sids:
            tse_data = fetch_twse_openapi(tse_sids)
            live.update(tse_data)
        if otc_sids:
            otc_data = fetch_tpex_openapi(otc_sids)
            live.update(otc_data)
        if fetch_source != "mis_live":
            fetch_source = "openapi_daily"

    # 最終確認
    valid_count = sum(1 for v in live.values() if v.get("price") not in (None, "-", "--", ""))
    print(f"  ✅ 最終取得 {len(live)} 筆，有效報價 {valid_count} 筆")
    for sid, p in live.items():
        price = p.get("price", "-")
        prev  = p.get("prev",  "-")
        name  = p.get("name",  sid)
        src   = p.get("source","?")
        try:
            chg = f"+{(float(price)-float(prev))/float(prev)*100:.2f}%" if price not in ("-","--") and prev not in ("-","--","0") else "--"
        except:
            chg = "--"
        print(f"  {p.get('market','?')} {sid} {name:8s} {str(price):>8s}  {chg}  [{src}]")

    if not trading and valid_count == 0:
        errors = ["非交易時段；若資料仍為空，請手動觸發 workflow 或等待隔日 09:05 自動更新"]

    _write_out(live, now_str, now.strftime("%Y%m%d"), trading, errors, fetch_source)

def _write_out(prices, now_str, trade_date, trading, errors, fetch_source="unknown"):
    output = {
        "updated_at":    now_str,
        "trade_date":    trade_date,
        "is_trading":    trading,
        "fetch_source":  fetch_source,
        "prices":        prices,
        "fetch_errors":  errors,
    }
    os.makedirs(os.path.dirname(LIVE_PATH), exist_ok=True)
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"  💾 live.json 寫入完成（{len(prices)} 筆，來源：{fetch_source}）")

if __name__ == "__main__":
    main()
