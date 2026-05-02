# ============================================================
# File   : trading/summary/notify/discord_notifier.py
# Version: Ver1.0-PRODUCTION-DISCORD-NOTIFIER
# ------------------------------------------------------------
# ✔ ENTRY通知
# ✔ 汎用通知
# ✔ DataFrame / list 両対応
# ✔ レート制限軽減
# ✔ NaN / None 防御
# ✔ logger
# ✔ alerts_util互換
# ✔ 本番安定版
# ============================================================

from __future__ import annotations

import logging
import time
import pandas as pd

from utils.alerts_util import send_discord_notify

logger = logging.getLogger(__name__)

# ============================================================
# rate limit control
# ============================================================

DISCORD_INTERVAL = 0.4


# ============================================================
# utility
# ============================================================

def _normalize_rows(rows):

    if rows is None:
        return []

    if isinstance(rows, pd.DataFrame):
        return [r for _, r in rows.iterrows()]

    if isinstance(rows, list):
        return rows

    return []


# ============================================================
# safe value
# ============================================================

def _safe(v, default=""):

    if v is None:
        return default

    try:
        if pd.isna(v):
            return default
    except Exception:
        pass

    return v


# ============================================================
# ENTRY SIGNAL NOTIFY
# ============================================================

def notify_entry_signals(rows):

    try:

        rows = _normalize_rows(rows)

        if not rows:
            return

        for r in rows:

            try:

                symbol = _safe(r.get("symbol"))
                name = _safe(r.get("symbolname"), symbol)

                score = _safe(
                    r.get("score", r.get("score_total", 0)),
                    0
                )

                rsi = _safe(r.get("rsi"), 0)

                price = _safe(
                    r.get("close", r.get("close_price", 0)),
                    0
                )

                msg = (
                    "📈 ENTRY SIGNAL\n"
                    f"{name} ({symbol})\n"
                    f"score={score}\n"
                    f"RSI={rsi}\n"
                    f"price={price}"
                )

                send_discord_notify(msg)

                time.sleep(DISCORD_INTERVAL)

            except Exception:

                logger.exception("entry notify failed")

    except Exception:

        logger.exception("notify_entry_signals failed")


# ============================================================
# GENERIC MESSAGE
# ============================================================

def notify_message(msg: str):

    try:

        if not msg:
            return

        send_discord_notify(msg)

    except Exception:

        logger.exception("notify_message failed")