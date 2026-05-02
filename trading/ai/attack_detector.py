# ============================================================
# File   : trading/ai/attack_detector.py
# Version: Ver1.0-ABSOLUTE-FINAL-SCALP-ATTACK-AWARE
# ------------------------------------------------------------
# ✔ 急騰 / 急落 初動検知
# ✔ VWAPブレイク
# ✔ MA75傾き連動
# ✔ 出来高加速
# ✔ 1min主軸 + MTF補助
# ✔ NaN / object 完全耐性
# ✔ スキャル特化設計
# ============================================================

from __future__ import annotations
import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)


# ============================================================
# 安全数値化
# ============================================================

def _safe(v, default=0.0):
    try:
        v = pd.to_numeric(v, errors="coerce")
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


# ============================================================
# 単一行アタック判定（軽量版）
# ============================================================

def detect_attack_row(row: dict) -> dict:
    """
    単一バーに対するアタック検知
    戻り値:
        {
            "long_attack": bool,
            "short_attack": bool,
            "attack_strength": float
        }
    """

    slope = _safe(row.get("ma75_slope"))
    vwap_break = bool(row.get("vwap_break"))
    vwap_fail = bool(row.get("vwap_fail"))
    vol_slope = _safe(row.get("volume_slope"))
    rsi = _safe(row.get("rsi"))

    long_score = 0
    short_score = 0

    # ============================
    # LONG攻撃条件
    # ============================

    if slope > 0:
        long_score += 1

    if vwap_break:
        long_score += 2

    if vol_slope > 0:
        long_score += 1

    if rsi > 55:
        long_score += 1

    # ============================
    # SHORT攻撃条件
    # ============================

    if slope < 0:
        short_score += 1

    if vwap_fail:
        short_score += 2

    if vol_slope < 0:
        short_score += 1

    if rsi < 45:
        short_score += 1

    long_attack = long_score >= 3
    short_attack = short_score >= 3

    attack_strength = long_score - short_score

    return {
        "long_attack": long_attack,
        "short_attack": short_attack,
        "attack_strength": attack_strength,
    }


# ============================================================
# DataFrame全体判定（1min主軸）
# ============================================================

def detect_attack_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    DataFrame全体にattack列を付与
    """

    if df is None or df.empty:
        return df

    df = df.copy()

    long_list = []
    short_list = []
    strength_list = []

    for _, row in df.iterrows():
        res = detect_attack_row(row.to_dict())
        long_list.append(res["long_attack"])
        short_list.append(res["short_attack"])
        strength_list.append(res["attack_strength"])

    df["long_attack"] = long_list
    df["short_attack"] = short_list
    df["attack_strength"] = strength_list

    return df


# ============================================================
# MTF補強版（3min / 5min確認）
# ============================================================

def detect_attack_with_mtf(
    df_1min: pd.DataFrame,
    df_3min: pd.DataFrame | None = None,
    df_5min: pd.DataFrame | None = None,
) -> pd.DataFrame:

    if df_1min is None or df_1min.empty:
        return df_1min

    df_1min = detect_attack_df(df_1min)

    if df_3min is None or df_3min.empty:
        return df_1min

    df_3min = df_3min[["symbol", "ma75_slope"]].rename(
        columns={"ma75_slope": "ma75_slope_3"}
    )

    df = df_1min.merge(df_3min, on="symbol", how="left")

    df["ma75_slope_3"] = (
        pd.to_numeric(df["ma75_slope_3"], errors="coerce")
        .replace([np.inf, -np.inf], 0)
        .fillna(0)
    )

    # 3min方向一致時のみ攻撃強化
    df["long_attack"] = (
        df["long_attack"] &
        (df["ma75_slope_3"] > 0)
    )

    df["short_attack"] = (
        df["short_attack"] &
        (df["ma75_slope_3"] < 0)
    )

    return df


# ============================================================
# 攻撃レベル分類
# ============================================================

def classify_attack_level(strength: float) -> str:

    strength = _safe(strength)

    if strength >= 4:
        return "ULTRA_LONG"
    if strength >= 2:
        return "STRONG_LONG"
    if strength <= -4:
        return "ULTRA_SHORT"
    if strength <= -2:
        return "STRONG_SHORT"

    return "NEUTRAL"