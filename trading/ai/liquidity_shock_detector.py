# ============================================================
# File: trading/ai/liquidity_shock_detector.py
# Ver1.0-PRO-LIQUIDITY-SHOCK-DETECTOR
# ------------------------------------------------------------
# ✔ volume spike
# ✔ price acceleration
# ✔ ranking velocity
# ✔ orderflow対応
# ✔ NaN完全防御
# ✔ ENTRY前検出
# ------------------------------------------------------------
# Notes:
#   - このモジュールはイナゴ/流動性ショック検出用。
#   - SUMMARY AI 通常エントリーの流動性判定は entry_pipeline 側で分離する。
# ============================================================

import logging

import numpy as np
import pandas as pd

from utils.alerts_util import send_discord_message

logger = logging.getLogger(__name__)


# ============================================================
# スコア計算
# ============================================================

def compute_liquidity_score(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    volume_ratio = pd.to_numeric(df.get("volume_ratio", 1), errors="coerce").fillna(1)
    price_change = pd.to_numeric(df.get("price_change", 0), errors="coerce").fillna(0)
    velocity = pd.to_numeric(df.get("velocity_score", 0), errors="coerce").fillna(0)
    rank_best = pd.to_numeric(df.get("rank_best", 50), errors="coerce").fillna(50)

    df["liquidity_score"] = (
        volume_ratio * 6
        + price_change * 10
        + velocity * 3
        + (30 - rank_best)
    )

    return df


# ============================================================
# ショック検出
# ============================================================

def detect_liquidity_shock(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or len(df) == 0:
        return pd.DataFrame()

    df = compute_liquidity_score(df)

    cond = (
        (df["volume_ratio"] > 3)
        & (df["price_change"] > 0.01)
        & (df["liquidity_score"] > 40)
    )

    result = df.loc[cond].copy()

    if len(result) > 0:

        for _, r in result.iterrows():
            embed = {
                "title": "💥 Liquidity Shock",
                "color": 15158332,
                "fields": [
                    {"name": "Symbol", "value": str(r["symbol"]), "inline": True},
                    {"name": "Price", "value": str(r.get("price", "")), "inline": True},
                    {"name": "Volume Ratio", "value": str(round(r.get("volume_ratio", 0), 2)), "inline": True},
                    {"name": "Score", "value": str(round(r["liquidity_score"], 2)), "inline": True},
                ],
            }

            send_discord_message(embeds=[embed])

    return result


# ============================================================
# ENTRYフィルター
# ============================================================

def allow_liquidity_entry(row: dict) -> bool:

    try:

        volume_ratio = row.get("volume_ratio", 1)
        price_change = row.get("price_change", 0)
        velocity = row.get("velocity_score", 0)

        if volume_ratio > 3 and price_change > 0.01 and velocity > 5:
            return True

        return False

    except Exception:

        return False
