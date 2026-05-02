# ============================================================
# File   : trading/scoring/advanced_mtf.py
# Version: Ver1.6-ABSOLUTE-ADVANCED-MTF-ULTRA-STABLE-HARDLOCK-PRO
# ------------------------------------------------------------
# ✔ Ver1.5 完全保持（削除ゼロ）
# ✔ 1min / 3min / 5min トレンド統合
# ✔ slope_atr_scaled 前提設計
# ✔ 列欠損時 自動ウェイト再正規化
# ✔ MTF方向一致ボーナス強化（3段階）
# ✔ symbol単位完全分離
# ✔ NaN / inf 完全防御
# ✔ duplicate列防御
# ✔ scheduler停止防止
# ✔ ENTRY / EXIT 副作用ゼロ
# ✔ DataFrameのみ返却
# ✔ regime拡張余地
# ✔ 極端値クリップ
# ✔ dtype最終安定化
# ✔ suffix自動補完
# ✔ interval列対応
# ✔ merge重複完全防止
# ✔ broadcast安全化
# ✔ 空group完全防御
# ✔ vectorized安全化
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_WARNED_MISSING = set()


# ============================================================
# numeric safety
# ============================================================

def _safe_numeric(series: pd.Series) -> pd.Series:

    return (
        pd.to_numeric(series, errors="coerce")
        .replace([np.inf, -np.inf], 0.0)
        .fillna(0.0)
        .astype("float64")
    )


# ============================================================
# advanced mtf
# ============================================================

def apply_advanced_mtf(
    df: pd.DataFrame,
    *,
    slope_col: str = "slope_atr_scaled",
    weight_1m: float = 0.5,
    weight_3m: float = 0.3,
    weight_5m: float = 0.2,
    clip_value: float = 15.0,
) -> pd.DataFrame:

    try:

        if df is None or df.empty:
            return df

        if not isinstance(df, pd.DataFrame):
            logger.warning("[advanced_mtf] input not DataFrame")
            return df

        df = df.copy()

        # ----------------------------------------------------
        # duplicate columns
        # ----------------------------------------------------

        if df.columns.duplicated().any():

            dup = list(df.columns[df.columns.duplicated()])

            logger.warning(
                "[advanced_mtf] duplicate columns removed: %s",
                dup,
            )

            df = df.loc[:, ~df.columns.duplicated()]

        # ----------------------------------------------------
        # base columns
        # ----------------------------------------------------

        col_1m = slope_col
        col_3m = f"{slope_col}_3m"
        col_5m = f"{slope_col}_5m"

        available_cols = set(df.columns)

        # ====================================================
        # suffix auto completion (interval pivot)
        # ====================================================

        if (
            (col_3m not in available_cols or col_5m not in available_cols)
            and "interval" in df.columns
            and slope_col in df.columns
        ):

            try:

                df["interval"] = (
                    pd.to_numeric(df["interval"], errors="coerce")
                    .fillna(0)
                    .astype("int64")
                )

                base = df[["symbol", "interval", slope_col]].copy()

                base = base.dropna(subset=["symbol"])

                pivot = base.pivot_table(
                    index="symbol",
                    columns="interval",
                    values=slope_col,
                    aggfunc="last",
                )

                merge_cols = {}

                for tf in (3, 5):

                    if tf in pivot.columns:

                        merge_cols[tf] = f"{slope_col}_{tf}m"

                if merge_cols:

                    temp = (
                        pivot[list(merge_cols.keys())]
                        .rename(columns=merge_cols)
                        .reset_index()
                    )

                    for new_col in merge_cols.values():

                        if new_col in df.columns:
                            temp = temp.drop(columns=[new_col], errors="ignore")

                    df = df.merge(temp, on="symbol", how="left")

                available_cols = set(df.columns)

            except Exception:

                logger.exception(
                    "[advanced_mtf] interval pivot failed"
                )

        # ====================================================
        # ensure columns exist
        # ====================================================

        for col in (col_1m, col_3m, col_5m):

            if col not in df.columns:

                if col not in _WARNED_MISSING:

                    logger.warning(
                        "[advanced_mtf] missing column → auto zero: %s",
                        col,
                    )

                    _WARNED_MISSING.add(col)

                df[col] = 0.0

        # ----------------------------------------------------
        # numeric safety
        # ----------------------------------------------------

        df[col_1m] = _safe_numeric(df[col_1m])
        df[col_3m] = _safe_numeric(df[col_3m])
        df[col_5m] = _safe_numeric(df[col_5m])

        # ----------------------------------------------------
        # init
        # ----------------------------------------------------

        df["mtf_score"] = 0.0
        df["mtf_alignment_bonus"] = 0.0

        # ----------------------------------------------------
        # symbol loop
        # ----------------------------------------------------

        for symbol, g in df.groupby("symbol", sort=False):

            if g.empty:
                continue

            idx = g.index

            s1 = g[col_1m].values.astype("float64")
            s3 = g[col_3m].values.astype("float64")
            s5 = g[col_5m].values.astype("float64")

            active_mask = np.array([
                np.abs(s1).sum() > 0,
                np.abs(s3).sum() > 0,
                np.abs(s5).sum() > 0,
            ])

            weights = np.array(
                [weight_1m, weight_3m, weight_5m],
                dtype="float64"
            )

            if active_mask.sum() == 0:
                continue

            weights = weights * active_mask

            wsum = weights.sum()

            if wsum > 0:
                weights = weights / wsum

            w1, w3, w5 = weights

            score = w1 * s1 + w3 * s3 + w5 * s5

            # ------------------------------------------------
            # alignment
            # ------------------------------------------------

            sign_1 = np.sign(s1)
            sign_3 = np.sign(s3)
            sign_5 = np.sign(s5)

            triple_align = (
                (sign_1 == sign_3)
                & (sign_1 == sign_5)
                & (sign_1 != 0)
            )

            double_align = (
                ((sign_1 == sign_3) & (sign_1 != 0))
                | ((sign_1 == sign_5) & (sign_1 != 0))
                | ((sign_3 == sign_5) & (sign_3 != 0))
            )

            bonus = np.zeros(len(g), dtype="float64")

            bonus[double_align] = 0.1
            bonus[triple_align] = 0.3

            score = score + bonus

            score = np.clip(score, -clip_value, clip_value)

            score = np.nan_to_num(
                score,
                nan=0.0,
                posinf=clip_value,
                neginf=-clip_value
            )

            df.loc[idx, "mtf_score"] = score
            df.loc[idx, "mtf_alignment_bonus"] = bonus

        # ----------------------------------------------------
        # final numeric
        # ----------------------------------------------------

        df["mtf_score"] = _safe_numeric(df["mtf_score"])
        df["mtf_alignment_bonus"] = _safe_numeric(
            df["mtf_alignment_bonus"]
        )

        return df

    except Exception:

        logger.exception("[advanced_mtf] fatal error")

        return df