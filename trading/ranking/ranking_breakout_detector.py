# ============================================================
# File   : trading/ranking/ranking_breakout_detector.py
# Version: Ver1.0-RANKING-BREAKOUT-DETECTOR-PRODUCTION
# ------------------------------------------------------------
# ✔ ranking_theme_heat を入力
# ✔ 急騰直前のブレイク候補検出
# ✔ momentum + acceleration + theme
# ✔ 価格ブレイク検出
# ✔ 出来高ブレイク検出
# ✔ NaN / inf 防御
# ✔ vectorized
# ✔ DataFrame in / out
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# utils
# ============================================================

def _safe(s):

    if s is None:
        return pd.Series(dtype=float)

    return (
        pd.to_numeric(s, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )


# ============================================================
# breakout detector
# ============================================================

def detect_ranking_breakout(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    # =========================================================
    # 必須列
    # =========================================================

    price = _safe(df.get("current_price"))
    price_delta = _safe(df.get("price_delta_1m"))
    volume = _safe(df.get("trading_volume"))
    vol_speed = _safe(df.get("volume_speed"))

    accel = _safe(df.get("acceleration_score_norm"))
    theme = _safe(df.get("theme_heat_norm"))

    # =========================================================
    # price breakout
    # =========================================================

    df["price_breakout"] = (price_delta > 0.01).astype(int)

    # =========================================================
    # volume breakout
    # =========================================================

    df["volume_breakout"] = (
        (vol_speed > 1.5) | (volume > volume.quantile(0.9))
    ).astype(int)

    # =========================================================
    # momentum
    # =========================================================

    df["momentum_score"] = (
        price_delta * 0.4
        + accel * 0.4
        + theme * 0.2
    )

    # =========================================================
    # breakout score
    # =========================================================

    df["breakout_score"] = (
        df["momentum_score"]
        + df["price_breakout"] * 0.5
        + df["volume_breakout"] * 0.5
    )

    # =========================================================
    # normalize
    # =========================================================

    m = df["breakout_score"].max()

    if m > 0:
        df["breakout_score_norm"] = df["breakout_score"] / m
    else:
        df["breakout_score_norm"] = 0

    # =========================================================
    # signal
    # =========================================================

    df["breakout_signal"] = (
        (df["breakout_score_norm"] > 0.6)
    ).astype(int)

    # =========================================================
    # sort
    # =========================================================

    df = df.sort_values(
        "breakout_score_norm",
        ascending=False
    )

    logger.info(
        "[ranking_breakout] candidates=%s",
        df["breakout_signal"].sum()
    )

    return df