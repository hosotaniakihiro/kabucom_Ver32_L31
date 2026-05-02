# ============================================================
# File   : trading/ranking/ranking_liquidity_filter.py
# Version: Ver1.0-LIQUIDITY-FILTER-PRODUCTION
# ------------------------------------------------------------
# ✔ breakout候補から流動性フィルター
# ✔ 売買代金
# ✔ 出来高
# ✔ スプレッド
# ✔ 板厚
# ✔ NaN / inf 完全防御
# ✔ vectorized
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
# liquidity filter
# ============================================================

def apply_ranking_liquidity_filter(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    turnover = _safe(df.get("trading_value"))
    volume = _safe(df.get("trading_volume"))
    price = _safe(df.get("current_price"))

    spread = _safe(df.get("spread"))
    board_thickness = _safe(df.get("board_thickness"))

    # =========================================================
    # basic liquidity
    # =========================================================

    df["liq_turnover"] = turnover
    df["liq_volume"] = volume

    # =========================================================
    # liquidity score
    # =========================================================

    df["liquidity_score"] = (
        np.log1p(turnover) * 0.5 +
        np.log1p(volume) * 0.3 +
        (1 / (spread + 0.01)) * 0.1 +
        board_thickness * 0.1
    )

    # =========================================================
    # normalize
    # =========================================================

    m = df["liquidity_score"].max()

    if m > 0:
        df["liquidity_norm"] = df["liquidity_score"] / m
    else:
        df["liquidity_norm"] = 0

    # =========================================================
    # filter
    # =========================================================

    df["liquidity_pass"] = (
        (turnover > 50_000_000) &
        (volume > 10_000)
    ).astype(int)

    # =========================================================
    # sort
    # =========================================================

    df = df.sort_values(
        ["liquidity_pass", "liquidity_norm"],
        ascending=False
    )

    logger.info(
        "[ranking_liquidity] pass=%s / total=%s",
        df["liquidity_pass"].sum(),
        len(df)
    )

    return df