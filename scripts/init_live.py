#!/usr/bin/env python3
"""每日盤後重置 live.json"""
import json, os
from datetime import datetime, timezone, timedelta

TZ_TW     = timezone(timedelta(hours=8))
LIVE_PATH = os.path.join(os.path.dirname(__file__), "../data/live.json")

def main():
    now     = datetime.now(TZ_TW)
    now_str = now.strftime("%Y/%m/%d %H:%M:%S")
    output  = {
        "updated_at":   now_str,
        "trade_date":   now.strftime("%Y%m%d"),
        "is_trading":   False,
        "prices":       {},
        "fetch_errors": [f"盤後重置（{now_str}），盤中資料將於次日 09:05 後更新"],
    }
    os.makedirs(os.path.dirname(LIVE_PATH), exist_ok=True)
    with open(LIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"✅ live.json 重置完成")

if __name__ == "__main__":
    main()
