# ============================================================
# AI/risk_ai_logger.py
# ============================================================

import csv
import os
import datetime as dt

LOG_PATH = "logs/risk_ai_log.csv"

HEADER = [
    "datetime",
    "event",
    "loss_streak",
    "intraday_pnl",
    "reason",
    "cooldown_min",
]


def log_risk_event(
    event: str,
    loss_streak: int,
    intraday_pnl: float,
    reason: str = "",
    cooldown_min: int | None = None,
):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    exists = os.path.exists(LOG_PATH)

    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(HEADER)

        writer.writerow([
            dt.datetime.now().isoformat(timespec="seconds"),
            event,
            loss_streak,
            round(intraday_pnl, 5),
            reason,
            cooldown_min,
        ])
