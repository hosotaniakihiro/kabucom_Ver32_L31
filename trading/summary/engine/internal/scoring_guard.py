# ============================================================
# File   : trading/summary/engine/internal/scoring_guard.py
# Version: Ver3.3-PRODUCTION-SCORING-GUARD-ULTRA-STABLE-NAN-PRESERVE-FINAL
# ------------------------------------------------------------
# ✔ Ver3.2 全機能完全保持（削除ゼロ）
# ✔ score列完全保証
# ✔ slope列完全保証
# ✔ mtf列完全保証
# ✔ NaN / inf 完全防御
# ✔ dtype安定化
# ✔ 欠損列自動生成
# ✔ score_total再構築を安全化
# ✔ score_buy / score_sell 優先で total 再計算
# ✔ score_slope / score_mtf の二重加算防止
# ✔ 表示軸の優先順位を修正
# ✔ mtf に score_mtf を直結しない
# ✔ slope に score_slope を直結しすぎない
# ✔ pandas alignment crash防止
# ✔ 本番耐性（どんな状態でも落ちない）
# ✔ NEW: slope / mtf / score_slope / score_mtf は NaN preserve
# ✔ NEW: mtf に slope_atr_scaled を流用しない
# ✔ NEW: mtf_alignment_bonus を最優先候補に採用
# ✔ NEW: display軸再構築でも mtf に slope を流用しない
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric
# ============================================================

def _safe_numeric(series: pd.Series) -> pd.Series:
    """
    確定列向け:
    - NaN/inf を 0.0 に寄せる
    - score_total / score_buy / score_sell / score 用
    """
    try:
        return (
            pd.to_numeric(series, errors="coerce")
            .replace([np.inf, -np.inf], 0.0)
            .fillna(0.0)
            .astype("float64")
        )
    except Exception:
        try:
            idx = series.index
            n = len(series)
        except Exception:
            idx = None
            n = 0

        return pd.Series(
            np.zeros(n),
            index=idx,
            dtype="float64",
        )


def _safe_numeric_nan(series: pd.Series) -> pd.Series:
    """
    表示/未成熟列向け:
    - NaN を保持する
    - slope / mtf / score_slope / score_mtf 用
    """
    try:
        return (
            pd.to_numeric(series, errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
            .astype("float64")
        )
    except Exception:
        try:
            idx = series.index
            n = len(series)
        except Exception:
            idx = None
            n = 0

        return pd.Series(
            np.full(n, np.nan),
            index=idx,
            dtype="float64",
        )


def _is_effectively_zero(series: pd.Series) -> bool:
    try:
        s = _safe_numeric_nan(series)
        if s.dropna().empty:
            return True
        return bool((s.fillna(0.0) == 0).all())
    except Exception:
        return True


# ============================================================
# slope保証
# ============================================================

def ensure_slope(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        slope_cols = [
            "slope_raw",
            "slope_atr_scaled",
            "slope_atr_scaled_3m",
            "slope_atr_scaled_5m",
        ]

        for col in slope_cols:
            if col not in df.columns:
                logger.warning(
                    "[SCORING GUARD] missing slope column → auto create: %s",
                    col,
                )
                df[col] = np.nan

            df[col] = _safe_numeric_nan(df[col])

        return df

    except Exception:
        logger.exception("[SCORING GUARD] ensure_slope failed")
        return df


# ============================================================
# score保証
# ============================================================

def ensure_score(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        score_cols = [
            "score",
            "score_buy",
            "score_sell",
            "score_total",
            "score_slope",
            "score_mtf",
        ]

        for col in score_cols:
            if col not in df.columns:
                logger.warning(
                    "[SCORING GUARD] missing score column → auto create: %s",
                    col,
                )

                if col == "score_slope":
                    if "score_slope" in df.columns:
                        df[col] = _safe_numeric_nan(df["score_slope"])
                    elif "slope" in df.columns:
                        df[col] = _safe_numeric_nan(df["slope"])
                    elif "slope_atr_scaled" in df.columns:
                        df[col] = _safe_numeric_nan(df["slope_atr_scaled"])
                    else:
                        df[col] = np.nan

                elif col == "score_mtf":
                    if "score_mtf" in df.columns:
                        df[col] = _safe_numeric_nan(df["score_mtf"])
                    elif "mtf" in df.columns:
                        df[col] = _safe_numeric_nan(df["mtf"])
                    elif "mtf_score" in df.columns:
                        df[col] = _safe_numeric_nan(df["mtf_score"])
                    elif "mtf_alignment_bonus" in df.columns:
                        df[col] = _safe_numeric_nan(df["mtf_alignment_bonus"])
                    else:
                        df[col] = np.nan

                else:
                    df[col] = 0.0

            # score列は0許容、派生表示列はNaN保持
            if col in ("score_slope", "score_mtf"):
                df[col] = _safe_numeric_nan(df[col])
            else:
                df[col] = _safe_numeric(df[col])

        return df

    except Exception:
        logger.exception("[SCORING GUARD] ensure_score failed")
        return df


# ============================================================
# mtf保証
# ============================================================

def ensure_mtf(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        mtf_cols = [
            "mtf",
            "mtf_score",
            "mtf_alignment_bonus",
        ]

        for col in mtf_cols:
            if col not in df.columns:
                logger.warning(
                    "[SCORING GUARD] missing mtf column → auto create: %s",
                    col,
                )

                if col == "mtf":
                    # 最優先は mtf_alignment_bonus
                    if "mtf_alignment_bonus" in df.columns and not _is_effectively_zero(df["mtf_alignment_bonus"]):
                        df[col] = _safe_numeric_nan(df["mtf_alignment_bonus"])
                    elif "mtf" in df.columns and not _is_effectively_zero(df["mtf"]):
                        df[col] = _safe_numeric_nan(df["mtf"])
                    elif "mtf_score" in df.columns and not _is_effectively_zero(df["mtf_score"]):
                        df[col] = _safe_numeric_nan(df["mtf_score"])
                    elif "score_mtf" in df.columns and not _is_effectively_zero(df["score_mtf"]):
                        df[col] = _safe_numeric_nan(df["score_mtf"])
                    else:
                        df[col] = np.nan
                else:
                    df[col] = np.nan

            df[col] = _safe_numeric_nan(df[col])

        return df

    except Exception:
        logger.exception("[SCORING GUARD] ensure_mtf failed")
        return df


# ============================================================
# score_total再構築
# ============================================================

def rebuild_score_total(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        buy = _safe_numeric(df["score_buy"]) if "score_buy" in df.columns else None
        sell = _safe_numeric(df["score_sell"]) if "score_sell" in df.columns else None
        current_total = _safe_numeric(df["score_total"]) if "score_total" in df.columns else None
        current_score = _safe_numeric(df["score"]) if "score" in df.columns else None

        if buy is not None and sell is not None:
            df["score_total"] = _safe_numeric(buy - sell)
        elif current_total is not None:
            df["score_total"] = current_total
        elif current_score is not None:
            df["score_total"] = current_score
        else:
            df["score_total"] = 0.0

        df["score_total"] = _safe_numeric(df["score_total"])

        if "score" not in df.columns:
            df["score"] = df["score_total"]
        else:
            score_now = _safe_numeric(df["score"])
            total_now = _safe_numeric(df["score_total"])
            df["score"] = score_now.where(score_now != 0, total_now)
            df["score"] = _safe_numeric(df["score"])

        return df

    except Exception:
        logger.exception("[SCORING GUARD] rebuild_score_total failed")
        return df


# ============================================================
# summary display compatibility
# ============================================================

def rebuild_display_axes(df: pd.DataFrame) -> pd.DataFrame:
    """
    表示用 score / slope / mtf を安全に整える。
    優先順位:
      slope -> slope_atr_scaled -> score_slope
      mtf   -> mtf_alignment_bonus -> mtf_score -> score_mtf
      score -> score_total
    注意:
      mtf に slope_atr_scaled を流用しない
    """
    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        # ----------------------------------------------------
        # slope
        # ----------------------------------------------------
        if "slope" in df.columns and not _is_effectively_zero(df["slope"]):
            df["slope"] = _safe_numeric_nan(df["slope"])
        elif "slope_atr_scaled" in df.columns and not _is_effectively_zero(df["slope_atr_scaled"]):
            df["slope"] = _safe_numeric_nan(df["slope_atr_scaled"])
        elif "score_slope" in df.columns and not _is_effectively_zero(df["score_slope"]):
            df["slope"] = _safe_numeric_nan(df["score_slope"])
        else:
            df["slope"] = np.nan

        # ----------------------------------------------------
        # mtf
        # ----------------------------------------------------
        if "mtf" in df.columns and not _is_effectively_zero(df["mtf"]):
            df["mtf"] = _safe_numeric_nan(df["mtf"])
        elif "mtf_alignment_bonus" in df.columns and not _is_effectively_zero(df["mtf_alignment_bonus"]):
            df["mtf"] = _safe_numeric_nan(df["mtf_alignment_bonus"])
        elif "mtf_score" in df.columns and not _is_effectively_zero(df["mtf_score"]):
            df["mtf"] = _safe_numeric_nan(df["mtf_score"])
        elif "score_mtf" in df.columns and not _is_effectively_zero(df["score_mtf"]):
            df["mtf"] = _safe_numeric_nan(df["score_mtf"])
        else:
            df["mtf"] = np.nan

        # ----------------------------------------------------
        # score
        # ----------------------------------------------------
        if "score" in df.columns and not _is_effectively_zero(df["score"]):
            df["score"] = _safe_numeric(df["score"])
        elif "score_total" in df.columns:
            df["score"] = _safe_numeric(df["score_total"])
        else:
            df["score"] = 0.0

        return df

    except Exception:
        logger.exception("[SCORING GUARD] rebuild_display_axes failed")
        return df


# ============================================================
# 最終統合
# ============================================================

def finalize_scoring(df: pd.DataFrame) -> pd.DataFrame:
    """
    スコア最終安定化
    """
    if df is None or df.empty:
        return df

    try:
        df = df.copy()

        df = ensure_slope(df)
        df = ensure_mtf(df)
        df = ensure_score(df)

        df = rebuild_score_total(df)
        df = rebuild_display_axes(df)

        for col in df.columns:
            if col in ("score", "score_buy", "score_sell", "score_total", "final_score", "display_score"):
                df[col] = _safe_numeric(df[col])
            elif (
                col.startswith("slope")
                or col.startswith("mtf")
                or col in ("score_slope", "score_mtf")
            ):
                df[col] = _safe_numeric_nan(df[col])

        return df

    except Exception:
        logger.exception("[SCORING GUARD] finalize failed")
        return df