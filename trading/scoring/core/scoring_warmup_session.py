# ============================================================
# File   : trading/scoring/core/scoring_warmup_session.py
# Version: 1.1-FINAL-SESSION-NUMERIC-SAFE
# ------------------------------------------------------------
# ✔ 前日セッション強度統合（SEED_ONLY用）
# ✔ DB非依存（volumeベース疑似セッション）
# ✔ dict混入完全禁止
# ✔ 数値のみ加算
# ✔ score_total 無くても安全生成
# ✔ 欠損 / ゼロ除算 完全防御
# ✔ 1min / 3min / 5min 全対応
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd


# ============================================================
# 安全numeric変換
# ============================================================

def _to_numeric_safe(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype(float)


# ============================================================
# 安全除算
# ============================================================

def _safe_div(a, b):
    return a / np.where(b == 0, np.nan, b)


# ============================================================
# warmup session補正
# ============================================================

def scoring_warmup_session(
    df: pd.DataFrame,
    interval: int,
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # score_total 無い事故防止
    # --------------------------------------------------------
    if "score_total" not in df.columns:
        df["score_total"] = 0.0

    df["score_total"] = _to_numeric_safe(
        df["score_total"]
    ).fillna(0.0)

    # ========================================================
    # 1️⃣ 出来高ベース セッション強度
    # ========================================================
    if "volume" in df.columns:

        volume = _to_numeric_safe(df["volume"]).fillna(0.0)

        # rolling平均（直近20本）
        vol_mean = volume.rolling(20, min_periods=1).mean()

        # 強度比率
        vol_ratio = _safe_div(volume, vol_mean).fillna(1.0)

        # 強度補正（急増のみ評価）
        session_boost = (vol_ratio - 1.0).clip(0.0, 3.0)

        df["score_total"] += session_boost

    # ========================================================
    # 2️⃣ レンジ拡大補正（値幅拡大＝活発セッション）
    # ========================================================
    if {"high", "low", "close"}.issubset(df.columns):

        high = _to_numeric_safe(df["high"])
        low = _to_numeric_safe(df["low"])
        close = _to_numeric_safe(df["close"])

        range_size = (high - low).abs()
        range_mean = range_size.rolling(20, min_periods=1).mean()

        range_ratio = _safe_div(range_size, range_mean).fillna(1.0)

        range_boost = (range_ratio - 1.0).clip(0.0, 2.0)

        df["score_total"] += range_boost

    # ========================================================
    # スコア整形
    # ========================================================
    df["score_total"] = df["score_total"].fillna(0.0).clip(-20.0, 20.0)

    # BUY / SELL 再生成（数値のみ）
    df["score_buy"] = np.where(
        df["score_total"] > 0,
        df["score_total"],
        0.0
    ).astype(float)

    df["score_sell"] = np.where(
        df["score_total"] < 0,
        -df["score_total"],
        0.0
    ).astype(float)

    df["score_reasons"] = "warmup_session"

    return df