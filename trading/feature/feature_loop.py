# ============================================================
# File   : trading/features/feature_loop.py
# Version: Ver3.0.0-PRO-FEATURE-ENGINE-PIPELINE-INTEGRATED
# ------------------------------------------------------------
# ✔ Ver2.1.0 全機能保持（削除ゼロ）
# ✔ intraday_delta 消費
# ✔ symbol別履歴保持
# ✔ rolling MA生成
# ✔ slope計算
# ✔ return計算
# ✔ volume z-score
# ✔ ranking統合
# ✔ indicator_pipeline統合
# ✔ add_features統合
# ✔ 欠損完全耐性
# ✔ 重複完全排除（datetime厳密）
# ✔ 差分更新
# ✔ 再起動耐性
# ✔ global_data安全更新
# ✔ scheduler絶対停止しない
# ✔ メモリ制御強化
# ✔ NaN / inf 完全吸収
# ✔ ranking列衝突防止
# ✔ concat安全化
# ✔ 将来tick高速拡張耐性
# ============================================================

from __future__ import annotations

import time
import logging
import pandas as pd
import numpy as np

from global_state import global_data

# indicator pipeline
try:
    from trading.features.indicators.indicator_pipeline import apply_indicator_pipeline
except Exception:
    apply_indicator_pipeline = None

# feature injector
try:
    from trading.features.add_features import add_features
except Exception:
    add_features = None

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.5
MAX_HISTORY_PER_SYMBOL = 300


# ------------------------------------------------------------
# 内部履歴キャッシュ（再起動時リセット）
# ------------------------------------------------------------

_symbol_history: dict[str, pd.DataFrame] = {}


# ============================================================
# 内部ユーティリティ
# ============================================================

def _safe_numeric(series: pd.Series) -> pd.Series:

    return (
        pd.to_numeric(series, errors="coerce")
        .replace([np.inf, -np.inf], 0)
        .fillna(0.0)
    )


def _ensure_columns(df: pd.DataFrame) -> pd.DataFrame:

    required = ["symbol", "datetime", "close", "volume"]

    for col in required:
        if col not in df.columns:
            df[col] = 0.0

    return df


# ============================================================
# 基本Feature生成
# ============================================================

def _build_features(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    df = _ensure_columns(df)

    df["symbol"] = df["symbol"].astype(str)

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    df["close"] = _safe_numeric(df["close"])

    df["volume"] = _safe_numeric(df["volume"])

    df = df.dropna(subset=["symbol", "datetime"])

    df = (
        df.sort_values(["symbol", "datetime"])
        .drop_duplicates(subset=["symbol", "datetime"], keep="last")
    )

    # --------------------------------------------------------
    # return
    # --------------------------------------------------------

    df["ret1"] = (
        df.groupby("symbol")["close"]
        .pct_change()
        .replace([np.inf, -np.inf], 0)
        .fillna(0)
    )

    # --------------------------------------------------------
    # MA
    # --------------------------------------------------------

    df["ma5"] = (
        df.groupby("symbol")["close"]
        .rolling(5, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )

    df["ma25"] = (
        df.groupby("symbol")["close"]
        .rolling(25, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )

    # --------------------------------------------------------
    # slope
    # --------------------------------------------------------

    df["ma5_slope"] = (
        df.groupby("symbol")["ma5"]
        .diff()
        .fillna(0)
    )

    df["ma25_slope"] = (
        df.groupby("symbol")["ma25"]
        .diff()
        .fillna(0)
    )

    # --------------------------------------------------------
    # volume z-score
    # --------------------------------------------------------

    vol_mean = (
        df.groupby("symbol")["volume"]
        .rolling(20, min_periods=1)
        .mean()
        .reset_index(level=0, drop=True)
    )

    vol_std = (
        df.groupby("symbol")["volume"]
        .rolling(20, min_periods=1)
        .std()
        .reset_index(level=0, drop=True)
    )

    df["volume_z"] = (df["volume"] - vol_mean) / (vol_std + 1e-9)

    df["volume_z"] = (
        df["volume_z"]
        .replace([np.inf, -np.inf], 0)
        .fillna(0)
    )

    return df

# ============================================================
# ranking統合
# ============================================================

def _merge_ranking(df_feat: pd.DataFrame) -> pd.DataFrame:

    ranking_df = getattr(global_data, "ranking_snapshot_1min", None)

    if ranking_df is None:
        return df_feat

    if not isinstance(ranking_df, pd.DataFrame):
        return df_feat

    if ranking_df.empty:
        return df_feat

    ranking_df = ranking_df.copy()

    if "symbol" not in ranking_df.columns:
        return df_feat

    ranking_df["symbol"] = ranking_df["symbol"].astype(str)

    # ------------------------------------------------
    # 重複列削除（重要）
    # ------------------------------------------------

    ranking_df = ranking_df.loc[:, ~ranking_df.columns.duplicated()]

    # OHLCVは除外（壊れる原因）
    drop_cols = {"open","high","low","close","volume"}

    ranking_df = ranking_df[
        [c for c in ranking_df.columns if c not in drop_cols]
    ]

    df_feat = df_feat.merge(
        ranking_df,
        on="symbol",
        how="left",
        suffixes=("", "_ranking")
    )

    ranking_cols = [
        "rank_score",
        "rank_delta",
        "rank_strength",
        "ranking_momentum",
        "ranking_volume_boost",
    ]

    for col in ranking_cols:

        if col in df_feat.columns:

            df_feat[col] = _safe_numeric(df_feat[col])

    return df_feat


# ============================================================
# メインループ
# ============================================================

def feature_loop():

    logger.info("🟢 feature_loop started")

    while True:

        try:

            delta = getattr(global_data, "intraday_delta", None)

            if delta is None or not isinstance(delta, pd.DataFrame):
                time.sleep(POLL_INTERVAL)
                continue

            if delta.empty:
                global_data.intraday_delta = None
                time.sleep(POLL_INTERVAL)
                continue

            delta = delta.copy()

            # ------------------------------------------------
            # symbol別履歴更新
            # ------------------------------------------------

            for sym, group in delta.groupby("symbol"):

                sym = str(sym)

                hist = _symbol_history.get(sym)

                if hist is None:
                    hist = group.copy()
                else:
                    hist = pd.concat(
                        [hist, group],
                        ignore_index=True
                    )

                hist = (
                    hist.sort_values("datetime")
                    .drop_duplicates(subset=["datetime"], keep="last")
                )

                if len(hist) > MAX_HISTORY_PER_SYMBOL:
                    hist = hist.tail(MAX_HISTORY_PER_SYMBOL)

                _symbol_history[sym] = hist

            if not _symbol_history:
                global_data.intraday_delta = None
                time.sleep(POLL_INTERVAL)
                continue

            # ------------------------------------------------
            # 全履歴結合
            # ------------------------------------------------

            try:

                df_all = pd.concat(
                    _symbol_history.values(),
                    ignore_index=True
                )

            except ValueError:

                global_data.intraday_delta = None
                time.sleep(POLL_INTERVAL)
                continue

            if df_all.empty:

                global_data.intraday_delta = None
                time.sleep(POLL_INTERVAL)
                continue

            # ------------------------------------------------
            # 基本Feature生成
            # ------------------------------------------------

            df_feat = _build_features(df_all)

            # ------------------------------------------------
            # indicator pipeline
            # ------------------------------------------------

            if apply_indicator_pipeline is not None:

                try:
                    df_feat = apply_indicator_pipeline(df_feat)
                except Exception:
                    logger.exception("indicator_pipeline failed")

            # ------------------------------------------------
            # feature pipeline
            # ------------------------------------------------

            if add_features is not None:

                try:
                    df_feat = add_features(df_feat)
                except Exception:
                    logger.exception("add_features failed")

            # ------------------------------------------------
            # ranking統合
            # ------------------------------------------------

            df_feat = _merge_ranking(df_feat)

            # ------------------------------------------------
            # 最新行抽出
            # ------------------------------------------------

            df_latest = (
                df_feat.sort_values("datetime")
                .groupby("symbol")
                .tail(1)
                .reset_index(drop=True)
            )

            if df_latest.empty:

                global_data.intraday_delta = None
                time.sleep(POLL_INTERVAL)
                continue

            # ------------------------------------------------
            # global_data更新
            # ------------------------------------------------

            try:

                global_data.feature_cache = df_latest

            except Exception:

                setattr(global_data, "feature_cache", df_latest)

            global_data.intraday_delta = None

            logger.debug(
                "[FEATURE] rows=%d symbols=%d",
                len(df_latest),
                df_latest["symbol"].nunique()
            )

        except Exception:

            logger.exception("❌ feature_loop unexpected error")

        time.sleep(POLL_INTERVAL)