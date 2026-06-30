# ============================================================
# File   : trading/entry/pipeline/imports.py
# Function:
#   - entry pipeline の遅延 import / optional import 集約
#   - import 失敗時も scheduler を止めない
# ------------------------------------------------------------
# Version: Ver40-PRODUCTION-ENTRY-PIPELINE-IMPORTS-OPTIONAL-TORCH
# ============================================================

from __future__ import annotations

import logging
from typing import Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# global_data
# ============================================================

try:
    from global_state import global_data

    logger.info("[IMPORT OK] global_data loaded")

except Exception:
    global_data = None
    logger.exception("[IMPORT FAIL] global_data import failed")


# ============================================================
# market filter
# ============================================================

try:
    from utils.market_filter import get_tradeable_symbols

    logger.info("[IMPORT OK] get_tradeable_symbols loaded")

except Exception:
    get_tradeable_symbols = None
    logger.exception("[IMPORT FAIL] get_tradeable_symbols import failed")


# ============================================================
# tonosama
# ============================================================

try:
    from trading.ai.tonosama_detector import allow_tonosama_entry

    logger.info("[IMPORT OK] tonosama guard loaded")

except Exception:
    logger.exception("[IMPORT FAIL] tonosama guard import failed")

    def allow_tonosama_entry(symbol: str) -> bool:
        return True


# ============================================================
# summary
# ============================================================

try:
    from trading.summary.summary_controller import summary_controller

    logger.info("[IMPORT OK] summary_controller loaded")

except Exception:
    summary_controller = None
    logger.exception("[IMPORT FAIL] summary_controller import failed")

try:
    from trading.summary.position_filter import can_entry_symbol

    logger.info("[IMPORT OK] can_entry_symbol loaded")

except Exception:
    logger.exception("[IMPORT FAIL] can_entry_symbol import failed")

    def can_entry_symbol(symbol, side, source="", with_reason=False):
        if with_reason:
            return True, ""
        return True


# ============================================================
# top candidates
# ============================================================

try:
    from trading.summary.top_candidates import (
        prepare_buy_sell_top_df,
        collect_ai_entry_candidates,
        log_ai_entry_candidates,
    )

    logger.info("[IMPORT OK] top_candidates AI candidate APIs loaded")

except Exception:
    collect_ai_entry_candidates = None
    log_ai_entry_candidates = None

    logger.exception("[IMPORT FAIL] top_candidates AI candidate APIs import failed; fallback active")

    def prepare_buy_sell_top_df(
        df: pd.DataFrame,
        buy_top_n: int = 10,
        sell_top_n: int = 10,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        top_candidates import 失敗時の最低限 fallback。
        """

        try:
            work = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()

            if work.empty or "symbol" not in work.columns:
                return pd.DataFrame(), pd.DataFrame()

            if "score_buy" not in work.columns:
                if "score" in work.columns:
                    work["score_buy"] = work["score"]
                else:
                    work["score_buy"] = 0.0

            if "score_sell" not in work.columns:
                work["score_sell"] = 0.0

            work["score_buy"] = pd.to_numeric(work["score_buy"], errors="coerce").fillna(0.0)
            work["score_sell"] = pd.to_numeric(work["score_sell"], errors="coerce").fillna(0.0)

            buy_df = (
                work[work["score_buy"] > 0]
                .sort_values(["score_buy", "score_sell"], ascending=[False, True])
                .drop_duplicates(subset=["symbol"], keep="first")
                .head(int(buy_top_n))
                .copy()
            )

            sell_df = (
                work[work["score_sell"] > 0]
                .sort_values(["score_sell", "score_buy"], ascending=[False, True])
                .drop_duplicates(subset=["symbol"], keep="first")
                .head(int(sell_top_n))
                .copy()
            )

            if not buy_df.empty:
                buy_df["signal"] = "BUY"

            if not sell_df.empty:
                sell_df["signal"] = "SELL"

            return buy_df, sell_df

        except Exception:
            return pd.DataFrame(), pd.DataFrame()


# ============================================================
# legacy entry handler
# ============================================================

try:
    from trading.handlers.entry_handler import (
        place_entry_buy,
        place_entry_sell,
    )

    logger.info("[IMPORT OK] legacy entry_handler loaded")

except Exception:
    logger.exception("[IMPORT FAIL] legacy entry_handler import failed")

    def place_entry_buy(*args, **kwargs):
        return None

    def place_entry_sell(*args, **kwargs):
        return None


# ============================================================
# entry controller
# ============================================================

try:
    from trading.handlers.entry_controller import (
        run_entry_pipeline as run_entry_controller,
    )

    logger.info("[IMPORT OK] entry_controller loaded")

except Exception:
    run_entry_controller = None
    logger.exception("[IMPORT FAIL] entry_controller import failed")


# ============================================================
# market regime
# ============================================================

try:
    from trading.market.regime_filter import (
        detect_market_regime,
        allow_entry,
    )

    logger.info("[IMPORT OK] market regime filter loaded")

except Exception:
    logger.exception("[IMPORT FAIL] market regime filter import failed")

    def detect_market_regime():
        return None

    def allow_entry() -> bool:
        return True


# ============================================================
# ranking entry
# ============================================================

try:
    from trading.ranking.entry_from_ranking import (
        run_ranking_entry_pipeline,
    )

    logger.info("[IMPORT OK] ranking entry pipeline loaded")

except Exception:
    run_ranking_entry_pipeline = None
    logger.exception("[IMPORT FAIL] ranking entry pipeline import failed")


# ============================================================
# AI trading engine
# ============================================================

try:
    from trading.core.trading_engine import trading_engine

    logger.info("[IMPORT OK] AI trading_engine loaded")

except ModuleNotFoundError as e:
    trading_engine = None
    if str(getattr(e, "name", "")) == "torch" or "torch" in str(e):
        logger.warning(
            "[IMPORT OPTIONAL] AI trading_engine skipped because optional torch is not installed. "
            "Summary-AI / ranking / tonosama entry can continue without RL trading_engine."
        )
    else:
        logger.exception("[IMPORT FAIL] AI trading_engine import failed")

except Exception:
    trading_engine = None
    logger.exception("[IMPORT FAIL] AI trading_engine import failed")


# ============================================================
# AI enricher
# ============================================================

try:
    from trading.entry.ai_enricher import enrich_pending_entries_with_ai

    logger.info("[IMPORT OK] AI enricher loaded")

except Exception:
    enrich_pending_entries_with_ai = None
    logger.exception("[IMPORT FAIL] AI enricher import failed")


# ============================================================
# pending manager
# ============================================================

try:
    from trading.entry.pending_manager import (
        get_bucket,
        replace_bucket,
    )

    logger.info("[IMPORT OK] pending_manager loaded")

except Exception:
    get_bucket = None
    replace_bucket = None
    logger.exception("[IMPORT FAIL] pending_manager import failed")
