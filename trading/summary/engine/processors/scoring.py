# ============================================================
# File   : trading/summary/engine/processors/scoring.py
# Version: Ver2.1-PRODUCTION-SCORING-PROCESSOR-SIGNATURE-FIX
# ------------------------------------------------------------
# ✔ safe_scoring / apply_scoring 提供
# ✔ run_scoring_pipeline の版差異吸収
# ✔ interval 引数あり/なし両対応
# ✔ force 引数あり/なし両対応
# ✔ 失敗時は元DFを返して全体継続
# ✔ scoring 成功時は scored df を返す
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from trading.scoring.core.scoring_pipeline import run_scoring_pipeline
except Exception:
    run_scoring_pipeline = None


def _ensure_df(obj: Any) -> pd.DataFrame:
    if isinstance(obj, pd.DataFrame):
        return obj.copy()
    try:
        return pd.DataFrame(obj).copy()
    except Exception:
        return pd.DataFrame()


def _call_with_fallbacks(fn: Callable, df: pd.DataFrame, interval: str | int):
    attempts = [
        ("run_scoring_pipeline(df, interval=interval)", lambda: fn(df, interval=interval)),
        ("run_scoring_pipeline(df, interval)", lambda: fn(df, interval)),
        ("run_scoring_pipeline(df, force=False)", lambda: fn(df, force=False)),
        ("run_scoring_pipeline(df)", lambda: fn(df)),
    ]

    last_type_error = None

    for label, caller in attempts:
        try:
            out = caller()
            logger.info("[SCORING PROCESSOR] succeeded via %s", label)
            return out
        except TypeError as e:
            last_type_error = e
            logger.warning("[SCORING PROCESSOR] signature mismatch via %s err=%s", label, e)
            continue

    if last_type_error is not None:
        raise last_type_error

    return None


def apply_scoring(df: pd.DataFrame, interval: str | int = "1min") -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    if run_scoring_pipeline is None:
        logger.warning("[SCORING PROCESSOR] run_scoring_pipeline unavailable")
        return df.copy()

    try:
        df_in = df.copy()

        df_scored = _call_with_fallbacks(run_scoring_pipeline, df_in, interval)
        df_scored = _ensure_df(df_scored)

        if df_scored.empty:
            logger.warning(
                "[SCORING PROCESSOR] scoring returned empty interval=%s -> keep original",
                interval,
            )
            return df_in

        logger.info(
            "[SCORING PROCESSOR] applied interval=%s rows=%s cols=%s",
            interval,
            len(df_scored),
            len(df_scored.columns),
        )
        return df_scored

    except Exception:
        logger.exception("[SCORING PROCESSOR] failed interval=%s", interval)
        return df.copy()


def safe_scoring(df: pd.DataFrame, interval: str | int = "1min") -> pd.DataFrame:
    try:
        return apply_scoring(df, interval=interval)
    except Exception:
        logger.exception("[SCORING PROCESSOR] safe_scoring fatal interval=%s", interval)
        return df.copy()