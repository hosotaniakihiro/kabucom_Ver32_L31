# ============================================================
# File   : trading/summary/pipelines/signals_pipeline.py
# Version: Ver1.0-PRODUCTION-SIGNALS-PIPELINE
# ------------------------------------------------------------
# ✔ signals_engine integration
# ✔ dataframe guard integration
# ✔ datetime guard integration
# ✔ duplicate guard integration
# ✔ crash isolation
# ✔ symbol safety
# ✔ production logging
# ✔ real-time safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from trading.summary.utils.dataframe_sanitizer import sanitize_dataframe
from utils.dataframe_guard import sanitize_datetime
from trading.summary.utils.duplicate_guard import guard_duplicates

from trading.signals.signals_engine import evaluate_signals
from trading.signals.price_normalizer import normalize_dataframe

logger = logging.getLogger(__name__)


# ============================================================
# SIGNAL EVALUATION LOOP
# ============================================================

def _evaluate_signals(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    buy_signals = []
    short_signals = []
    decisions = []

    rows = len(df)

    for i in range(rows):

        try:

            curr = df.iloc[i].to_dict()

            prev = None
            if i > 0:
                prev = df.iloc[i - 1].to_dict()

            recent = df.iloc[: i + 1]

            signals = evaluate_signals(
                curr=curr,
                prev=prev,
                recent=recent
            )

            buy = signals.get("buy", [])
            short = signals.get("short", [])

            decision = None

            if len(buy) > len(short):
                decision = "BUY"

            elif len(short) > len(buy):
                decision = "SHORT"

            buy_signals.append(",".join(buy))
            short_signals.append(",".join(short))
            decisions.append(decision)

        except Exception:

            logger.warning(
                "[SIGNALS PIPELINE] evaluation failed row=%s",
                i
            )

            buy_signals.append("")
            short_signals.append("")
            decisions.append(None)

    df["buy_signals"] = buy_signals
    df["short_signals"] = short_signals
    df["signal_decision"] = decisions

    return df


# ============================================================
# MAIN PIPELINE
# ============================================================

def run_signals_pipeline(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        logger.debug(
            "[SIGNALS PIPELINE] start rows=%s",
            len(df)
        )

        # ----------------------------------------------------
        # sanitize dataframe
        # ----------------------------------------------------

        df = sanitize_dataframe(df)

        # ----------------------------------------------------
        # datetime guard
        # ----------------------------------------------------

        df = sanitize_datetime(df)

        # ----------------------------------------------------
        # duplicate guard
        # ----------------------------------------------------

        df = guard_duplicates(df)

        # ----------------------------------------------------
        # price normalization
        # ----------------------------------------------------

        try:
            df = normalize_dataframe(df)
        except Exception:
            logger.warning(
                "[SIGNALS PIPELINE] price normalize failed"
            )

        # ----------------------------------------------------
        # evaluate signals
        # ----------------------------------------------------

        df = _evaluate_signals(df)

        logger.debug(
            "[SIGNALS PIPELINE] end rows=%s",
            len(df)
        )

    except Exception:

        logger.exception(
            "[SIGNALS PIPELINE] pipeline crashed"
        )

    return df