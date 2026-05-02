# ============================================================
# File   : trading/summary/summary_mtf_join.py
# Ver1.0-PRODUCTION-MTF-JOIN-FINAL
# ------------------------------------------------------------
# ✔ 1min に 3min / 5min の最新値を安全JOIN
# ✔ 欠損完全防御
# ✔ NaN / inf 完全防御
# ✔ symbol 正規化
# ✔ 列自動生成
# ✔ 実運用安定版
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)


# ============================================================
# 安全数値化
# ============================================================

def _safe_numeric(series: pd.Series) -> pd.Series:
    return (
        pd.to_numeric(series, errors="coerce")
        .replace([float("inf"), float("-inf")], 0.0)
        .fillna(0.0)
    )


# ============================================================
# symbol 正規化
# ============================================================

def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    if "symbol" in df.columns:
        df["symbol"] = (
            df["symbol"]
            .astype(str)
            .str.strip()
            .str.replace(".0", "", regex=False)
        )

    return df


# ============================================================
# 最新バー抽出（銘柄ごと）
# ============================================================

def _extract_latest_per_symbol(
    df: pd.DataFrame,
    columns: list[str],
    suffix: str,
) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame(columns=["symbol"] + [
            f"{col}_{suffix}" for col in columns
        ])

    df = df.copy()

    if "datetime" not in df.columns:
        logger.warning("[MTF] datetime missing in higher timeframe")
        return pd.DataFrame()

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])

    if df.empty:
        return pd.DataFrame()

    # 銘柄ごと最新バー
    df_last = (
        df.sort_values("datetime")
        .groupby("symbol")
        .tail(1)
    )

    keep_cols = ["symbol"] + columns
    keep_cols = [c for c in keep_cols if c in df_last.columns]

    df_last = df_last[keep_cols].copy()

    # suffix付与
    rename_map = {
        col: f"{col}_{suffix}"
        for col in columns
        if col in df_last.columns
    }

    df_last = df_last.rename(columns=rename_map)

    return df_last


# ============================================================
# メインJOIN関数
# ============================================================

def apply_mtf_join(
    summary_1min: pd.DataFrame,
    summary_3min: pd.DataFrame | None,
    summary_5min: pd.DataFrame | None,
    *,
    columns_to_join: list[str] | None = None,
) -> pd.DataFrame:
    """
    1min に 3min / 5min の最新値を JOIN する

    Parameters
    ----------
    summary_1min : 1分足
    summary_3min : 3分足
    summary_5min : 5分足
    columns_to_join : JOIN対象列（未指定時は slope_atr_scaled のみ）
    """

    if summary_1min is None or summary_1min.empty:
        return summary_1min

    if columns_to_join is None:
        columns_to_join = ["slope_atr_scaled"]

    df_1m = _normalize_symbol(summary_1min)

    # ========================================================
    # 3MIN JOIN
    # ========================================================

    if summary_3min is not None and not summary_3min.empty:

        df_3m_last = _extract_latest_per_symbol(
            _normalize_symbol(summary_3min),
            columns_to_join,
            suffix="3m",
        )

        if not df_3m_last.empty:
            df_1m = df_1m.merge(
                df_3m_last,
                on="symbol",
                how="left",
            )

    # ========================================================
    # 5MIN JOIN
    # ========================================================

    if summary_5min is not None and not summary_5min.empty:

        df_5m_last = _extract_latest_per_symbol(
            _normalize_symbol(summary_5min),
            columns_to_join,
            suffix="5m",
        )

        if not df_5m_last.empty:
            df_1m = df_1m.merge(
                df_5m_last,
                on="symbol",
                how="left",
            )

    # ========================================================
    # 欠損防御
    # ========================================================

    for col in columns_to_join:
        col_3m = f"{col}_3m"
        col_5m = f"{col}_5m"

        if col_3m not in df_1m.columns:
            df_1m[col_3m] = 0.0

        if col_5m not in df_1m.columns:
            df_1m[col_5m] = 0.0

        df_1m[col_3m] = _safe_numeric(df_1m[col_3m])
        df_1m[col_5m] = _safe_numeric(df_1m[col_5m])

    return df_1m


# ============================================================
# 将来拡張用：複数列JOINテンプレ
# ============================================================

def apply_mtf_join_extended(
    summary_1min: pd.DataFrame,
    summary_3min: pd.DataFrame | None,
    summary_5min: pd.DataFrame | None,
) -> pd.DataFrame:
    """
    将来的に複数列をJOINしたい場合はこちらを使用
    """

    columns = [
        "slope_atr_scaled",
        # 例:
        # "ma25_slope",
        # "ma75_slope",
        # "volume_slope",
        # "vwap_slope",
    ]

    return apply_mtf_join(
        summary_1min,
        summary_3min,
        summary_5min,
        columns_to_join=columns,
    )