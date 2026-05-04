# ============================================================
# File   : trading/scoring/core/scoring_core.py
# Version: Ver1.6-PRODUCTION-MODULAR-SCORING-CORE-MARKET-FILTER-PRIMARY
#          -LAZY-IMPORT-STABLE-FIXED
# ------------------------------------------------------------
# ✔ Ver1.5 完全保持
# ✔ utils.market_filter を最優先で使用
# ✔ ETF/ETN/REIT は symbol_flags.db 側 universe で最終除外
# ✔ prefix ETF filter は高速な補助フィルタとして維持
# ✔ detail_score_builder 統合
# ✔ MACD / RSI / MA / VWAP / volume / orderflow の detail bridge 維持
# ✔ プライム / スタンダード / グロースのみ
# ✔ NaN / inf 安全
# ✔ market time guard
# ✔ backward compatibility
# ✔ AI統合
# ✔ logging強化
# ✔ production safe
# ✔ FIX: heavy scoring imports を遅延 import 化
# ✔ FIX: scoring_main import failure の循環依存を回避
# ✔ FIX: duplicate scoring_main definition removed
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from utils.market_filter import filter_tradeable_dataframe
from utils.market_time import is_market_open

from trading.scoring.preprocess.normalize_columns import normalize_columns
from trading.scoring.preprocess.sanitize_numeric import sanitize_numeric

logger = logging.getLogger(__name__)

ETF_CODE_PREFIX = ("13", "15", "16", "17", "25")


def _safe_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    try:
        df = df.replace([np.inf, -np.inf], np.nan)
    except Exception:
        logger.debug("[SCORING] inf replace failed", exc_info=True)

    try:
        if isinstance(df.columns, pd.MultiIndex):
            df = df.copy()
            df.columns = [
                "_".join([str(x) for x in col if x not in ("", None)])
                for col in df.columns.to_flat_index()
            ]
    except Exception:
        logger.debug("[SCORING] multiindex flatten failed", exc_info=True)

    try:
        if df.columns.duplicated().any():
            df = df.loc[:, ~df.columns.duplicated(keep="last")].copy()
    except Exception:
        logger.debug("[SCORING] duplicate column cleanup failed", exc_info=True)

    return df


def _remove_etf_prefix_fast(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    try:
        before = len(df)

        sym = (
            df["symbol"]
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
        )

        mask = ~sym.str.startswith(ETF_CODE_PREFIX)
        out = df.loc[mask].copy()

        removed = before - len(out)
        if removed > 0:
            logger.info("[SCORING] fast ETF prefix removed=%s", removed)

        return out

    except Exception:
        logger.exception("[SCORING] fast ETF prefix filter failed")
        return df


def _filter_market_primary(df: pd.DataFrame) -> pd.DataFrame:
    try:
        before = len(df)

        out = filter_tradeable_dataframe(df)

        if out is None:
            logger.warning("[SCORING] market filter returned None -> keep original")
            return df

        removed = before - len(out)

        if removed > 0:
            logger.info("[SCORING] market filter removed=%s", removed)

        return out

    except Exception:
        logger.exception("[SCORING] market filter failed")
        return df


def _lazy_import_scoring_pipeline():
    from trading.scoring.core.scoring_pipeline import run_scoring_pipeline
    return run_scoring_pipeline


def _lazy_import_score_calculator():
    from trading.scoring.core.score_calculator import calculate_final_scores
    return calculate_final_scores


def _lazy_import_ai_score():
    from trading.scoring.ai.ai_score_engine import apply_ai_score
    return apply_ai_score


def _lazy_import_detail_builder():
    from trading.scoring.core.detail_score_builder import build_detail_scores
    return build_detail_scores


def scoring_main(
    df: pd.DataFrame,
    interval: str | int | None = None,
    force: bool = False,
) -> pd.DataFrame:
    """
    Main scoring entry point.

    Flow
    ----
    1) normalize/sanitize
    2) optional fast ETF prefix drop
    3) primary market universe filter via utils.market_filter
    4) detail score build from score_config.ini
    5) scoring pipeline
    6) final score calculator
    7) AI score
    """
    market_open = is_market_open()
    analysis_only = (not market_open) and (not force)

    logger.info(
        "[SCORING] start market_open=%s force=%s analysis_only=%s interval=%s",
        market_open,
        force,
        analysis_only,
        interval,
    )

    df = _safe_dataframe(df)

    if df.empty:
        logger.info("[SCORING] empty input")
        return df

    try:
        df_out = df.copy()

        df_out = normalize_columns(df_out)
        df_out = sanitize_numeric(df_out)

        if df_out is None or df_out.empty:
            logger.info("[SCORING] empty after sanitize_numeric")
            return pd.DataFrame()

        df_out = _remove_etf_prefix_fast(df_out)
        if df_out is None or df_out.empty:
            logger.info("[SCORING] empty after fast ETF prefix filter")
            return pd.DataFrame()

        df_out = _filter_market_primary(df_out)
        if df_out is None or df_out.empty:
            logger.info("[SCORING] empty after primary market filter")
            return pd.DataFrame()

        try:
            build_detail_scores = _lazy_import_detail_builder()
            df_out = build_detail_scores(df_out)
        except Exception:
            logger.exception("[SCORING] build_detail_scores failed")

        try:
            run_scoring_pipeline = _lazy_import_scoring_pipeline()
            try:
                df_out = run_scoring_pipeline(
                    df_out,
                    interval=interval,
                    analysis_only=analysis_only,
                )
            except TypeError:
                df_out = run_scoring_pipeline(
                    df_out,
                    interval=interval,
                )
        except Exception:
            logger.exception("[SCORING] run_scoring_pipeline failed")
            return pd.DataFrame()

        if df_out is None or df_out.empty:
            logger.info("[SCORING] empty after run_scoring_pipeline")
            return pd.DataFrame()

        try:
            calculate_final_scores = _lazy_import_score_calculator()
            df_out = calculate_final_scores(df_out, interval=interval)
        except TypeError:
            df_out = calculate_final_scores(df_out)
        except Exception:
            logger.exception("[SCORING] calculate_final_scores failed")
            return pd.DataFrame()

        if df_out is None or df_out.empty:
            logger.info("[SCORING] empty after calculate_final_scores")
            return pd.DataFrame()

        try:
            apply_ai_score = _lazy_import_ai_score()
            df_out = apply_ai_score(df_out)
        except Exception:
            logger.exception("[SCORING] AI scoring failed")

        logger.info(
            "[SCORING] completed rows=%s interval=%s",
            len(df_out),
            interval,
        )

        return df_out

    except Exception:
        logger.exception("❌ scoring_main error")
        return df


# 旧互換エイリアス
run_scoring_main = scoring_main

__all__ = [
    "scoring_main",
    "run_scoring_main",
]