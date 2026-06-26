#!/usr/bin/env python3
"""
V5 新增：每日盤後抓完 stocks.json 後，重置 live.json 為初始狀態
目的：清除前一天殘留的 prices，避免前端顯示過期報價
"""

import json, os
from datetime import datetime, timezone, timedelta

TZ_TW = timezone(timedelta(hours=8))
LIVE_PATH = os.path.join(os.path.dirname(__file__), "../data/live.json")

def main():
    now = datetime.now(TZ_TW)
    now_str = now.strftime("%Y/%m/%d %H:%M:%S")
    trade_date = now.strftime("%Y%m%d")

    output = {
        "updated_at":   now_str,
        "trade_date":   trade_date,
        "is_trading":   False,
        "prices":       {},
        "fetch_errors": [f"盤後重置完成（{now_str}），盤中資料將於隔日 09:05 後自動更新"]
    }

    os.makedirs(os.path.dirname(LIVE_PATH), exist_ok=True)
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"✅ live.json 已重置（{now_str}）")

if __name__ == "__main__":
    main()
