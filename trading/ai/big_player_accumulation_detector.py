# ============================================================
# File   : trading/ai/big_player_accumulation_detector.py
# Version: Ver1.0-BIG-PLAYER-ACCUMULATION-DETECTOR
# ------------------------------------------------------------
# ✔ 機関仕込み検出
# ✔ accumulation detection
# ✔ stealth volume detection
# ✔ turnover flow detection
# ✔ ranking pipeline compatible
# ✔ numpy vectorized
# ✔ NaN safe
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric
# ============================================================

def _safe(series):

    return (
        pd.to_numeric(series, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )


# ============================================================
# normalize helper
# ============================================================

def _normalize(series):

    try:

        m = series.abs().max()

        if m == 0:
            return series

        return series / (m + 1e-6)

    except Exception:

        return series


# ============================================================
# accumulation detection
# ============================================================

def detect_big_player_accumulation(df: pd.DataFrame) -> pd.DataFrame:
    """
    機関仕込み検出

    入力
    ----
    ranking / summary dataframe

    出力
    ----
    accumulation_score
    accumulation_probability
    accumulation_flag
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # features
        # ----------------------------------------------------

        volume_speed = _safe(df.get("volume_speed", 0))

        volume_delta = _safe(df.get("volume_delta_1m", 0))

        turnover = _safe(df.get("trading_value", 0))

        velocity = _safe(df.get("velocity_score", 0))

        price_delta = _safe(df.get("price_delta_1m", 0))

        institutional_prob = _safe(df.get("institutional_probability", 0))

        # ----------------------------------------------------
        # normalize
        # ----------------------------------------------------

        volume_n = _normalize(volume_speed)

        volume_delta_n = _normalize(volume_delta)

        turnover_n = _normalize(turnover)

        velocity_n = _normalize(velocity)

        price_n = _normalize(price_delta)

        # ----------------------------------------------------
        # stealth accumulation
        # ----------------------------------------------------

        quiet_price = price_n.abs() < 0.2

        slow_velocity = velocity_n < 0.4

        volume_build = (
            (volume_n > 0.4)
            & (volume_delta_n > 0.3)
        )

        institutional_hint = institutional_prob > 0.4

        # ----------------------------------------------------
        # accumulation score
        # ----------------------------------------------------

        accumulation_score = (
            0.30 * volume_n
            + 0.25 * volume_delta_n
            + 0.25 * turnover_n
            + 0.10 * (1 - velocity_n)
            + 0.10 * (1 - price_n.abs())
        )

        accumulation_score = accumulation_score.clip(0, 1)

        # ----------------------------------------------------
        # probability
        # ----------------------------------------------------

        accumulation_prob = 1 / (1 + np.exp(-6 * (accumulation_score - 0.4)))

        # ----------------------------------------------------
        # flag
        # ----------------------------------------------------

        accumulation_flag = (
            quiet_price
            & slow_velocity
            & volume_build
            & institutional_hint
        )

        df["accumulation_score"] = accumulation_score

        df["accumulation_probability"] = accumulation_prob

        df["accumulation_flag"] = accumulation_flag.astype(int)

        # ----------------------------------------------------
        # cache
        # ----------------------------------------------------

        try:
            global_data.big_player_accumulation = df
        except Exception:
            pass

        return df

    except Exception:

        logger.exception("[big_player_accumulation_detector]")

        return df


# ============================================================
# symbol lookup
# ============================================================

def get_accumulation_flag(symbol: str) -> int:

    try:

        df = getattr(global_data, "big_player_accumulation", None)

        if df is None or df.empty:
            return 0

        row = df[df["symbol"] == symbol]

        if row.empty:
            return 0

        return int(row["accumulation_flag"].iloc[0])

    except Exception:

        logger.exception("[get_accumulation_flag]")

        return 0