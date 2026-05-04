import csv
import os
from datetime import datetime

CSV_PATH = "AI/data/tonosama_train.csv"
os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)

def log_tonosama_trade(entry, exit_info):
    """
    entry:
      - symbol
      - market
      - rank_type
      - volume_speed
      - spread
      - tick_speed
      - entry_time
      - entry_price

    exit_info:
      - max_profit_rate
      - ret_60s
      - ret_120s
    """

    success = int(
        exit_info["max_profit_rate"] >= 0.003
        or exit_info.get("ret_60s", 0) >= 0.003
    )

    row = [
        entry["symbol"],
        entry["market"],
        entry["rank_type"],
        entry["volume_speed"],
        entry["spread"],
        entry["tick_speed"],
        exit_info.get("ret_60s", 0),
        exit_info.get("ret_120s", 0),
        success,
    ]

    write_header = not os.path.exists(CSV_PATH)

    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "symbol","market","rank_type","volume_speed",
                "spread","tick_speed",
                "ret_60s","ret_120s","success"
            ])
        writer.writerow(row)
