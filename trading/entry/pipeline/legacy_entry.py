# ============================================================
# File   : trading/entry/pipeline/legacy_entry.py
# Function:
#   - legacy summary entry
#   - legacy direct AI entry
# ------------------------------------------------------------
# Version: Ver39-PRODUCTION-ENTRY-PIPELINE-LEGACY-ENTRY
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from .imports import (
    global_data,
    trading_engine,
    place_entry_buy,
    place_entry_sell,
)

from .guards import (
    is_etf,
    pass_market_regime_guard,
    pass_symbol_guards,
)

from .utils import (
    safe_symbol,
    safe_float,
    normalize_side,
)

logger = logging.getLogger(__name__)


def run_summary_entry(interval: int):
    """
    legacy summary entry。

    注意:
      - Ver39 でも互換のため保持。
      - 通常は run_entry_pipeline(source="summary") を使う。
    """

    try:
        if global_data is None:
            logger.warning("[SUMMARY ENTRY][LEGACY] global_data unavailable")
            return None

        df = global_data.get_latest_summary()

        if df is None or df.empty:
            logger.debug("[SUMMARY ENTRY][LEGACY] empty interval=%s", interval)
            return None

        logger.info("[SUMMARY ENTRY][LEGACY] interval=%s rows=%d", interval, len(df))

        if not pass_market_regime_guard(log_prefix="[SUMMARY ENTRY][LEGACY]"):
            return None

        for _, row in df.iterrows():
            symbol = safe_symbol(row.get("symbol"))

            if is_etf(symbol):
                logger.debug("[ENTRY BLOCK] ETF skipped %s", symbol)
                continue

            side = normalize_side(row.get("signal"))
            reason = row.get("reason", "SUMMARY")

            if side not in ("BUY", "SELL"):
                continue

            ok, reason_ng = pass_symbol_guards(
                symbol=symbol,
                side=side,
                source="SUMMARY",
                log_prefix="[SUMMARY ENTRY][LEGACY]",
            )

            if not ok:
                logger.debug(
                    "[SUMMARY ENTRY SKIP][LEGACY] %s %s reason=%s",
                    symbol,
                    side,
                    reason_ng,
                )
                continue

            name = getattr(global_data, "symbol_name_map", {}).get(symbol, "")

            logger.info(
                "📊 SUMMARY ENTRY[LEGACY] %s %s (%s)",
                side,
                symbol,
                reason,
            )

            if side == "BUY":
                place_entry_buy(symbol, name, None, reason)
            else:
                place_entry_sell(symbol, name, None, reason)

    except Exception:
        logger.exception("[SUMMARY ENTRY][LEGACY] fatal error → ignored")

    return None


def run_ai_entry():
    """
    push_df 最新1件を直接 trading_engine に渡す legacy AI entry。
    """

    try:
        if trading_engine is None:
            logger.debug("[AI ENTRY] trading_engine unavailable")
            return None

        if global_data is None:
            logger.warning("[AI ENTRY] global_data unavailable")
            return None

        if not pass_market_regime_guard(log_prefix="[AI ENTRY]"):
            return None

        push_df = global_data.get_push_df()

        if push_df is None or push_df.empty:
            logger.debug("[AI ENTRY] push_df empty")
            return None

        row = push_df.iloc[-1]

        symbol = safe_symbol(row.get("symbol"))

        if is_etf(symbol):
            logger.debug("[AI ENTRY BLOCK] ETF skipped %s", symbol)
            return None

        ok, reason_ng = pass_symbol_guards(
            symbol=symbol,
            side="BUY",
            source="AI",
            log_prefix="[AI ENTRY]",
        )

        if not ok:
            logger.debug("[AI ENTRY BLOCK] symbol=%s reason=%s", symbol, reason_ng)
            return None

        price = safe_float(row.get("price", row.get("current_price", row.get("close", 0.0))), 0.0)

        trades = [
            {
                "price": price,
                "size": row.get("volume", 1),
                "side": "BUY",
            }
        ]

        price_col = "price"

        if price_col not in push_df.columns:
            if "current_price" in push_df.columns:
                price_col = "current_price"
            elif "close" in push_df.columns:
                price_col = "close"

        prices = pd.to_numeric(push_df[price_col], errors="coerce").dropna().tail(10).tolist()

        orderbook = {
            "best_bid": price - 0.1,
            "best_ask": price + 0.1,
            "bids": [(price - 0.1, 1000)],
            "asks": [(price + 0.1, 1000)],
        }

        portfolio_state = {
            "risk": 0.01,
            "volatility": 0.01,
            "drawdown": -0.001,
            "daily_pnl": 0,
            "correlation": 0.2,
        }

        trading_engine.process_market_tick(
            symbol=symbol,
            orderbook=orderbook,
            trades=trades,
            prices=prices,
            portfolio_state=portfolio_state,
        )

    except Exception:
        logger.exception("[AI ENTRY] failure")

    return None