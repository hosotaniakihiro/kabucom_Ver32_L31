# ============================================================
# File   : alerts/smartmoney_alert.py
# Version: Ver1.0-PRO-SMARTMONEY-ALERT
# ------------------------------------------------------------
# ✔ SMART MONEY検出通知
# ✔ Discord embed
# ✔ 日本語表示
# ✔ ranking情報
# ✔ volume / velocity
# ✔ DataFrame / dict / list 両対応
# ✔ NaN安全
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from alerts.discord_alert_engine import send_symbol_alert

logger = logging.getLogger(__name__)


# ============================================================
# normalize rows
# ============================================================

def _normalize_rows(rows):

    try:

        if rows is None:
            return []

        if isinstance(rows, pd.DataFrame):
            return [r for _, r in rows.iterrows()]

        if isinstance(rows, list):
            return rows

        if isinstance(rows, dict):
            return [rows]

        return []

    except Exception:

        logger.exception("[smartmoney_alert] normalize failed")

        return []


# ============================================================
# safe getter
# ============================================================

def _safe(row, key, default=None):

    try:

        if isinstance(row, dict):
            return row.get(key, default)

        if hasattr(row, "get"):
            return row.get(key, default)

        return default

    except Exception:

        return default


# ============================================================
# main alert
# ============================================================

def notify_smartmoney(rows):

    try:

        rows = _normalize_rows(rows)

        if not rows:
            return

        for r in rows:

            try:

                symbol = _safe(r, "symbol")
                symbolname = _safe(r, "symbolname", "")

                if not symbol:
                    continue

                price = _safe(r, "price")

                score = _safe(r, "smartmoney_score")

                ranking_type = _safe(r, "ranking_type")
                rank = _safe(r, "rank")

                volume_ratio = _safe(r, "volume_ratio")
                velocity = _safe(r, "velocity_score")

                send_symbol_alert(
                    "SMARTMONEY",
                    symbol=symbol,
                    symbolname=symbolname,
                    price=price,
                    score=score,
                    ranking_type=ranking_type,
                    rank=rank,
                    volume_ratio=volume_ratio,
                    velocity=velocity,
                )

            except Exception:

                logger.exception("[smartmoney_alert] row failed")

    except Exception:

        logger.exception("[smartmoney_alert] notify failed")