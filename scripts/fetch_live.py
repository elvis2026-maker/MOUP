#!/usr/bin/env python3
"""
盤中參考清單產生腳本 V19
==============================
V19 重大簡化：放棄「盤中即時報價」這個功能定位。

  為什麼：
    FinMind 免費 tier 完全沒有可用的盤中資料源：
      - TaiwanStockPrice（日K）要等週一至五 17:30 才更新
      - TaiwanStockKBar（分K）、taiwan_stock_tick_snapshot（即時）
        都需要付費 sponsor 會員
      - 證交所 / 櫃買中心官方 OpenAPI 從本工具部署的境外機房連不上
    過去 V9~V16 嘗試了多種方式硬做「盤中即時確認」，
    但本質上都只能顯示昨收價，卻包裝成「即時報價」，容易誤導使用者
    誤判進場時機。

  V19 新定位：
    這支腳本不再嘗試呼叫任何「今日報價」API。
    它只做一件事：把 stocks.json 的精選清單整理成「盤中參考卡」，
    內容只有「昨收、漲幅、MA20、評分」這些盤後就已經確定的數據，
    明確標示「請至證券 APP 查看即時報價」，不再假裝是即時資料。

  使用方式：
    這支腳本可以排程每天執行一次（例如盤後資料更新完後順便跑一次），
    不需要再像以前一樣每30分鐘跑一次盤中排程
    （對應的 .github/workflows/fetch-live.yml 已大幅簡化排程頻率）。
"""

import json, os
from datetime import datetime, timezone, timedelta

TZ_TW       = timezone(timedelta(hours=8))
STOCKS_PATH = os.path.join(os.path.dirname(__file__), "../data/stocks.json")
LIVE_PATH   = os.path.join(os.path.dirname(__file__), "../data/live.json")

def tw_now():
    return datetime.now(TZ_TW)

# V30 新增：版權聲明中繼資料，理由同 fetch_twse.py，附加在 live.json 裡一併流通
def _copyright_meta():
    return {
        "source": "MOUP - Taiwan Warrant Intelligence System",
        "author": "Elvis Liu",
        "notice": "本資料僅供本站展示使用，禁止未經授權之重製、轉載、商業利用，或作為第三方應用程式的資料來源／API 使用。",
        "generated_by": "fetch_live.py"
    }

def is_trading_now():
    now = tw_now()
    if now.weekday() >= 5: return False
    t = now.hour * 60 + now.minute
    return 9 * 60 <= t <= 13 * 60 + 30

def main():
    now     = tw_now()
    now_str = now.strftime("%Y/%m/%d %H:%M:%S")
    trading = is_trading_now()

    print(f"[{now_str} 台灣時間] fetch_live V19（盤中參考卡，非即時）")

    if not os.path.exists(STOCKS_PATH):
        _write(now_str, now.strftime("%Y%m%d"), trading, [], ["stocks.json 不存在，請先執行每日盤後抓資料"])
        return

    with open(STOCKS_PATH, encoding="utf-8") as f:
        stocks_data = json.load(f)

    stocks = stocks_data.get("stocks", [])

    if not stocks:
        _write(now_str, now.strftime("%Y%m%d"), trading, [],
               ["stocks.json 無候選股，請先執行每日盤後抓資料 workflow"])
        return

    # V35：盤中參考卡改版 —— 不再只是精選清單的重複資料，
    # 加入波動度/乖離率/連續天數/距高點/法人連買天數/除權息倒數/處置股警示，
    # 這些對「開盤前決定要不要進場買權證」比單純複製漲跌幅有意義。
    ref_cards = []
    for s in stocks:
        risk       = s.get("risk", {}) or {}
        risk_flags = s.get("risk_flags", {}) or {}
        inst       = s.get("inst", {}) or {}
        ref_cards.append({
            "sid":        s["sid"],
            "name":       s.get("name", ""),
            "market":     s.get("market", "tse"),
            "close":      s.get("close", 0),       # 昨收（盤後資料的收盤價）
            "change_pct": s.get("change_pct", 0),  # 昨日漲幅
            "ma20":       s.get("ma20"),
            "score":      s.get("score", 0),
            "prob":       s.get("prob", ""),
            "prob_level": s.get("prob_level", ""),
            # V35 新增：權證選股輔助指標
            "volatility_level":   risk.get("volatility_level"),
            "volatility_pct":     risk.get("volatility_pct"),
            "bias20_pct":         risk.get("bias20_pct"),
            "overheated":         risk.get("overheated", False),
            "up_streak_days":     risk.get("up_streak_days"),
            "dist_from_high_pct": risk.get("dist_from_high_pct"),
            "is_new_high":        risk.get("is_new_high", False),
            "buy_streak_days":    inst.get("buy_streak_days"),
            "ex_dividend_days":   risk_flags.get("ex_dividend_days"),
            "disposition_active": risk_flags.get("disposition_active", False),
        })
        print(f"  - {s['sid']} {s.get('name',''):8s} 昨收 {s.get('close')}  評分 {s.get('score')}")

    print(f"  💾 live.json 寫入完成（{len(ref_cards)} 張盤中參考卡，全部為昨收資料）")

    _write(now_str, now.strftime("%Y%m%d"), trading, ref_cards, [])

def _write(now_str, trade_date, trading, ref_cards, errors):
    output = {
        "_meta":       _copyright_meta(),
        "updated_at":  now_str,
        "trade_date":  trade_date,
        "is_trading":  trading,
        "is_live_data": False,   # V19：明確標示這不是即時資料，前端依此決定文案
        "ref_cards":   ref_cards,
        "fetch_errors": errors,
    }
    os.makedirs(os.path.dirname(LIVE_PATH), exist_ok=True)
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
