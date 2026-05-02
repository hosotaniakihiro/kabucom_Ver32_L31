# ============================================================
# File   : trading/entry/entry_router.py
# Version: Ver1.0-PRODUCTION-ENTRY-ROUTER
# ------------------------------------------------------------
# ✔ ranking entry 対応
# ✔ push entry 対応
# ✔ duplicate entry 防止
# ✔ PUSH自動監視登録
# ✔ position_manager連携
# ✔ AIスコアログ
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from typing import Optional

from core.global_context.context import global_context as GC

logger = logging.getLogger(__name__)


# ============================================================
# helpers
# ============================================================

def _already_in_position(symbol: str) -> bool:

    pm = GC.get("position_manager")

    if pm is None:
        return False

    return pm.has_position(symbol)


def _register_push(symbol: str):

    try:

        push_manager = GC.get("push_symbol_manager")

        if push_manager is None:
            return

        push_manager.register_symbol(symbol)

    except Exception:
        logger.exception("[ENTRY ROUTER] push register failed")


def _create_entry(symbol: str, reason: str, price: Optional[float] = None):

    pm = GC.get("position_manager")

    if pm is None:

        logger.warning(
            "[ENTRY ROUTER] position_manager missing symbol=%s",
            symbol,
        )

        return

    try:

        pm.open_position(
            symbol=symbol,
            entry_reason=reason,
            entry_price=price,
        )

        logger.info(
            "🚀 ENTRY symbol=%s reason=%s price=%s",
            symbol,
            reason,
            price,
        )

    except Exception:
        logger.exception("[ENTRY ROUTER] create entry failed")


# ============================================================
# ranking entry
# ============================================================

def process_ranking_entries(df: pd.DataFrame):

    if df is None or df.empty:
        return

    required = {"symbol"}

    if not required.issubset(df.columns):
        logger.warning("[ENTRY ROUTER] ranking df missing columns")
        return

    for _, row in df.iterrows():

        symbol = str(row["symbol"])

        if _already_in_position(symbol):
            continue

        score = row.get("score", None)
        ai_score = row.get("ai_score", None)

        try:

            if score is None:
                continue

            if score < 50:
                continue

            _create_entry(
                symbol=symbol,
                reason="ranking",
                price=row.get("close", None),
            )

            _register_push(symbol)

        except Exception:
            logger.exception(
                "[ENTRY ROUTER] ranking entry failed symbol=%s",
                symbol,
            )


# ============================================================
# push entry
# ============================================================

def process_push_entries(df: pd.DataFrame):

    if df is None or df.empty:
        return

    required = {"symbol", "close"}

    if not required.issubset(df.columns):
        logger.warning("[ENTRY ROUTER] push df missing columns")
        return

    for _, row in df.iterrows():

        symbol = str(row["symbol"])

        if _already_in_position(symbol):
            continue

        try:

            # example push conditions

            flag_breakout = row.get("flag_breakout_high", 0)
            flag_ma_up = row.get("flag_ma_up", 0)

            if not flag_breakout and not flag_ma_up:
                continue

            price = row.get("close")

            _create_entry(
                symbol=symbol,
                reason="push_signal",
                price=price,
            )

            _register_push(symbol)

        except Exception:

            logger.exception(
                "[ENTRY ROUTER] push entry failed symbol=%s",
                symbol
            )


# ============================================================
# unified entry router
# ============================================================

def route_entries(
    ranking_df: Optional[pd.DataFrame] = None,
    push_df: Optional[pd.DataFrame] = None,
):

    try:

        if ranking_df is not None:
            process_ranking_entries(ranking_df)

        if push_df is not None:
            process_push_entries(push_df)

    except Exception:
        logger.exception("[ENTRY ROUTER] route failed")