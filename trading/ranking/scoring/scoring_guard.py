# ============================================================
# File   : trading/ranking/scoring/scoring_guard.py
# Version: Ver4.2-PRODUCTION-ULTRA-STABLE-SCORING-GUARD-FIXED
# ------------------------------------------------------------
# ✔ Ver4.1 完全保持
# ✔ NaN / inf 完全除去
# ✔ score暴走防止（clip）
# ✔ 極端値検出
# ✔ 列存在保証
# ✔ dtype安全化
# ✔ 異常スコアログ
# ✔ symbol単位安全処理
# ✔ pandas crash防止
# ✔ keep_history モード保持
# ✔ ranking summary 用に履歴を潰さない
# ✔ NEW: auto-detect history-preserve mode for ranking summary frames
# ✔ NEW: display column aliases / defaults
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# parameters
# ============================================================

MAX_SCORE = 1000
MIN_SCORE = -1000

EXTREME_THRESHOLD = 500   # 異常検知用


# ============================================================
# helpers
# ============================================================

def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:
    try:
        num_cols = df.select_dtypes(include=np.number).columns

        df[num_cols] = (
            df[num_cols]
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0)
        )

    except Exception:
        logger.exception("[scoring_guard] sanitize failed")

    return df


def _ensure_score_column(df: pd.DataFrame) -> pd.DataFrame:
    if "score" not in df.columns:
        df["score"] = 0
    return df


def _clip_score(df: pd.DataFrame) -> pd.DataFrame:
    df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0).clip(MIN_SCORE, MAX_SCORE)
    return df


def _detect_extreme(df: pd.DataFrame):
    try:
        extreme = df[df["score"].abs() > EXTREME_THRESHOLD]

        if not extreme.empty:
            logger.warning(
                "[scoring_guard] extreme scores detected: %s rows",
                len(extreme)
            )

    except Exception:
        pass


def _ensure_required_columns(df: pd.DataFrame) -> pd.DataFrame:
    required = [
        "symbol",
        "datetime",
        "score",
    ]

    for col in required:
        if col not in df.columns:
            logger.warning("[scoring_guard] missing column: %s", col)

            if col == "symbol":
                df["symbol"] = ""

            elif col == "datetime":
                df["datetime"] = pd.Timestamp.now()

            elif col == "score":
                df["score"] = 0

    return df


def _ensure_display_columns(df: pd.DataFrame) -> pd.DataFrame:
    alias_map = {
        "close_price": "close",
        "last_price": "close",
        "current_price": "close",
        "price": "close",
        "score_slope": "slope",
        "ma25_slope": "slope",
        "slope_atr_scaled": "slope",
        "rsi14": "rsi",
        "macd_value": "macd",
        "rank": "best_rank",
        "best": "best_rank",
        "rank_type_name": "rank_type",
        "type": "rank_type",
        "hist_count": "hist",
    }

    for src, dst in alias_map.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]

    defaults = {
        "symbolname": "",
        "close": 0.0,
        "slope": 0.0,
        "rsi": np.nan,
        "macd": np.nan,
        "best_rank": np.nan,
        "rank_type": "",
    }
    for col, default in defaults.items():
        if col not in df.columns:
            df[col] = default

    if "hist" not in df.columns:
        if "symbol" in df.columns:
            df["hist"] = df.groupby("symbol")["symbol"].transform("size")
        else:
            df["hist"] = 1

    return df


def _sort_and_dedup(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if "symbol" in df.columns and "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            df = df.sort_values(["symbol", "datetime"], kind="stable")

            df = (
                df.groupby("symbol", as_index=False)
                .tail(1)
                .reset_index(drop=True)
            )

    except Exception:
        logger.exception("[scoring_guard] dedup failed")

    return df


def _sort_keep_history(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if "symbol" in df.columns and "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            df = (
                df.sort_values(["symbol", "datetime"], kind="stable")
                  .reset_index(drop=True)
            )
    except Exception:
        logger.exception("[scoring_guard] sort keep history failed")

    return df


def _looks_like_ranking_summary_history_frame(df: pd.DataFrame) -> bool:
    """
    ranking summary 表示前の履歴Frameを自動検知。
    keep_history=False のまま呼ばれても、履歴を潰しにくくする。
    """
    try:
        if df is None or df.empty:
            return False

        if "symbol" not in df.columns or "datetime" not in df.columns:
            return False

        # 同一symbolに複数時刻があるなら履歴Frameの可能性が高い
        multi_hist = (
            df.groupby("symbol")["datetime"]
            .nunique(dropna=True)
            .gt(1)
            .any()
        )

        # ranking系 or 指標列があるなら ranking summary 用の可能性が高い
        ranking_like_cols = {
            "rank_type", "best_rank", "ranking_velocity",
            "score_mtf", "score_slope", "rsi", "rsi14", "macd", "macd_value"
        }
        has_ranking_like = any(c in df.columns for c in ranking_like_cols)

        return bool(multi_hist and has_ranking_like)

    except Exception:
        logger.exception("[scoring_guard] auto history detect failed")
        return False


# ============================================================
# main
# ============================================================

def apply_scoring_guard(
    df: pd.DataFrame,
    *,
    keep_history: bool = False,
) -> pd.DataFrame:
    """
    スコア安全化処理（最重要）

    - NaN / inf 除去
    - score clip
    - 異常検知
    - 必須列保証
    - keep_history=False: 最新行抽出
    - keep_history=True : 履歴保持
    - auto history preserve: ranking summary 用履歴Frameを自動検知
    """

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df.copy()

    try:
        df = df.copy()

        # ----------------------------------------------------
        # 必須列
        # ----------------------------------------------------
        df = _ensure_required_columns(df)
        df = _ensure_display_columns(df)

        # ----------------------------------------------------
        # numeric sanitize
        # ----------------------------------------------------
        df = _sanitize_numeric(df)

        # ----------------------------------------------------
        # score列保証
        # ----------------------------------------------------
        df = _ensure_score_column(df)

        # ----------------------------------------------------
        # clip
        # ----------------------------------------------------
        df = _clip_score(df)

        # ----------------------------------------------------
        # 異常検知
        # ----------------------------------------------------
        _detect_extreme(df)

        # ----------------------------------------------------
        # sort / dedup
        # ----------------------------------------------------
        auto_keep_history = _looks_like_ranking_summary_history_frame(df)
        effective_keep_history = bool(keep_history or auto_keep_history)

        if effective_keep_history:
            df = _sort_keep_history(df)
            logger.info(
                "[scoring_guard] keep_history=True rows=%d symbols=%d auto=%s",
                len(df),
                int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
                auto_keep_history,
            )
        else:
            before = len(df)
            df = _sort_and_dedup(df)
            logger.info(
                "[scoring_guard] keep_history=False rows=%d -> %d symbols=%d",
                before,
                len(df),
                int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
            )

        if "hist" in df.columns and "symbol" in df.columns:
            df["hist"] = df.groupby("symbol")["symbol"].transform("size")

        return df

    except Exception:
        logger.exception("[scoring_guard] failed")
        return pd.DataFrame()
