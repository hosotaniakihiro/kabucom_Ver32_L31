# ============================================================
# File   : trading/summary/notify/tonosama_notifier.py
# Version: Ver1.0-PRODUCTION-TONOSAMA-NOTIFIER
# ------------------------------------------------------------
# ✔ 殿様イナゴ検出
# ✔ Discord通知
# ✔ 重複通知防止
# ✔ DataFrame安全
# ✔ NaN防御
# ✔ rate limit
# ✔ logger
# ✔ 本番安定版
# ============================================================

from __future__ import annotations

import logging
import time
import pandas as pd

from utils.alerts_util import send_discord_notify
from trading.signals.factors.tonosama import detect_tonosama

logger = logging.getLogger(__name__)

# ============================================================
# parameter
# ============================================================

DISCORD_INTERVAL = 0.4

# 同じ銘柄の再通知防止
_notified_symbols = set()


# ============================================================
# safe
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
# reset cache（市場開始時など）
# ============================================================

def reset_tonosama_cache():

    global _notified_symbols

    _notified_symbols.clear()


# ============================================================
# notify tonosama
# ============================================================

def notify_tonosama(df):

    try:

        if df is None:
            return

        if not isinstance(df, pd.DataFrame):
            return

        if df.empty:
            return

        # ----------------------------------------------------
        # detect
        # ----------------------------------------------------

        rows = detect_tonosama(df)

        if not rows:
            return

        # ----------------------------------------------------
        # notify
        # ----------------------------------------------------

        for r in rows:

            try:

                symbol = _safe(r.get("symbol"))

                if not symbol:
                    continue

                # 重複通知防止
                if symbol in _notified_symbols:
                    continue

                name = _safe(r.get("symbolname"), symbol)

                rsi = _safe(r.get("rsi"), 0)
                volume = _safe(r.get("volume"), 0)
                price = _safe(r.get("close", r.get("price", 0)), 0)

                msg = (
                    "🐛 殿様イナゴ検出\n"
                    f"{name} ({symbol})\n"
                    f"RSI={rsi}\n"
                    f"volume={volume:,}\n"
                    f"price={price}"
                )

                send_discord_notify(msg)

                _notified_symbols.add(symbol)

                time.sleep(DISCORD_INTERVAL)

            except Exception:

                logger.exception("tonosama notify row failed")

    except Exception:

        logger.exception("notify_tonosama failed")