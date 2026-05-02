# ============================================================
# File   : trading/ranking/engines/entry_timing.py
# Version: Ver4-PRODUCTION-ULTRA-STABLE-ENTRY-TIMING
# ------------------------------------------------------------
# ✔ エントリータイミング判定
# ✔ ignition / velocity / slope の合流点検出
# ✔ 押し目 vs ブレイク判定
# ✔ VWAP位置
# ✔ 短期過熱回避
# ✔ ボラ適正チェック
# ✔ groupby安全処理
# ✔ NaN / inf 完全防御
# ✔ 正規化
# ✔ pandas crash防止
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# parameters
# ============================================================

OVERHEAT_THRESHOLD = 0.05     # 短期上げすぎ判定
PULLBACK_THRESHOLD = -0.02    # 押し目
MIN_VOLATILITY = 0.002        # 動かなすぎ排除


# ============================================================
# helpers
# ============================================================

def _safe(df, col):
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(0, index=df.index)


def _sanitize(s):
    return s.replace([np.inf, -np.inf], np.nan).fillna(0)


def _normalize(s):
    m = s.abs().max()
    if m > 0:
        return s / m
    return s


# ============================================================
# price position（VWAP位置）
# ============================================================

def _vwap_position(df):

    if "vwap" not in df.columns:
        return pd.Series(0, index=df.index)

    close = _safe(df, "close")
    vwap = _safe(df, "vwap")

    pos = (close - vwap) / vwap.replace(0, np.nan)

    return _sanitize(pos)


# ============================================================
# short return（過熱検知）
# ============================================================

def _short_return(df):

    close = _safe(df, "close")

    ret = (
        close.groupby(df["symbol"])
        .pct_change(3)
    )

    return _sanitize(ret)


# ============================================================
# pullback判定
# ============================================================

def _pullback_score(ret):

    # 軽い押し目が一番良い
    score = np.where(
        (ret > PULLBACK_THRESHOLD) & (ret < 0),
        1,
        0
    )

    return pd.Series(score, index=ret.index)


# ============================================================
# breakout継続
# ============================================================

def _breakout_follow(df):

    if "ignition_score" not in df.columns:
        return pd.Series(0, index=df.index)

    ign = _safe(df, "ignition_score")

    # ignitionが強いほど継続期待
    return _sanitize(ign)


# ============================================================
# overheating filter
# ============================================================

def _overheat_penalty(ret):

    penalty = np.where(ret > OVERHEAT_THRESHOLD, -1, 0)

    return pd.Series(penalty, index=ret.index)


# ============================================================
# volatility filter
# ============================================================

def _volatility_filter(df):

    if "volatility" not in df.columns:
        return pd.Series(1, index=df.index)

    vol = _safe(df, "volatility")

    return (vol > MIN_VOLATILITY).astype(int)


# ============================================================
# main
# ============================================================

def apply_entry_timing(
    df: pd.DataFrame,
    *,
    normalize: bool = True
) -> pd.DataFrame:
    """
    エントリータイミング判定

    出力:
        df["entry_timing_score"]
    """

    if df is None or df.empty:
        return df

    try:

        df = df.copy()

        # ----------------------------------------------------
        # sort
        # ----------------------------------------------------
        if "symbol" in df.columns and "datetime" in df.columns:
            df = df.sort_values(["symbol", "datetime"])

        # ----------------------------------------------------
        # components
        # ----------------------------------------------------
        ret = _short_return(df)
        vwap_pos = _vwap_position(df)
        pullback = _pullback_score(ret)
        breakout = _breakout_follow(df)
        overheat = _overheat_penalty(ret)
        vol_ok = _volatility_filter(df)

        slope = _safe(df, "score_slope")
        velocity = _safe(df, "ranking_velocity")

        # ----------------------------------------------------
        # core logic
        # ----------------------------------------------------
        score = (
            breakout * 0.30 +     # 初動継続
            pullback * 0.25 +     # 押し目
            slope * 0.20 +        # トレンド
            velocity * 0.15 +     # 注目度
            vwap_pos * 0.10       # 強さ
        )

        score = score + overheat * 0.3

        # ----------------------------------------------------
        # volatility gate
        # ----------------------------------------------------
        score = score * vol_ok

        score = _sanitize(score)

        # ----------------------------------------------------
        # normalize
        # ----------------------------------------------------
        if normalize:
            score = _normalize(score)

        df["entry_timing_score"] = score

        return df

    except Exception:

        logger.exception("[entry_timing] apply failed")

        df["entry_timing_score"] = 0
        return df


# ============================================================
# utility
# ============================================================

def latest_entry_timing(df):

    if df is None or df.empty:
        return 0

    if "entry_timing_score" not in df.columns:
        return 0

    try:
        return float(df["entry_timing_score"].iloc[-1])
    except Exception:
        return 0