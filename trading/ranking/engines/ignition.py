# ============================================================
# File   : trading/ranking/engines/ignition.py
# Version: Ver4-PRODUCTION-ULTRA-STABLE-IGNITION
# ------------------------------------------------------------
# ✔ 初動検知（ignition）
# ✔ ブレイクアウト + 出来高急増
# ✔ レンジ圧縮 → 解放検知
# ✔ VWAP上抜け
# ✔ 直前モメンタム
# ✔ ダマシ排除（低流動性カット）
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

BREAKOUT_WINDOW = 20
VOL_WINDOW = 10
COMPRESSION_WINDOW = 10
MOM_WINDOW = 3


# ============================================================
# helpers
# ============================================================

def _safe_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce")
    return pd.Series(0, index=df.index)


def _sanitize(s: pd.Series) -> pd.Series:
    return (
        s.replace([np.inf, -np.inf], np.nan)
         .fillna(0)
    )


def _normalize(s: pd.Series) -> pd.Series:
    m = s.abs().max()
    if m > 0:
        return s / m
    return s


# ============================================================
# breakout
# ============================================================

def _breakout(df: pd.DataFrame) -> pd.Series:

    close = _safe_series(df, "close")

    high = (
        close.groupby(df["symbol"])
        .rolling(BREAKOUT_WINDOW)
        .max()
        .reset_index(level=0, drop=True)
    )

    breakout = (close >= high).astype(int)

    return breakout


# ============================================================
# volume spike
# ============================================================

def _volume_spike(df: pd.DataFrame) -> pd.Series:

    volume = _safe_series(df, "volume")

    ma = (
        volume.groupby(df["symbol"])
        .rolling(VOL_WINDOW)
        .mean()
        .reset_index(level=0, drop=True)
    )

    spike = volume / ma.replace(0, np.nan)

    return _sanitize(spike)


# ============================================================
# compression（レンジ収縮）
# ============================================================

def _compression(df: pd.DataFrame) -> pd.Series:

    high = _safe_series(df, "high")
    low = _safe_series(df, "low")

    range_ = high - low

    avg_range = (
        range_.groupby(df["symbol"])
        .rolling(COMPRESSION_WINDOW)
        .mean()
        .reset_index(level=0, drop=True)
    )

    comp = avg_range / (range_ + 1e-6)

    return _sanitize(comp)


# ============================================================
# vwap breakout
# ============================================================

def _vwap_break(df: pd.DataFrame) -> pd.Series:

    if "vwap" not in df.columns:
        return pd.Series(0, index=df.index)

    close = _safe_series(df, "close")
    vwap = _safe_series(df, "vwap")

    return (close > vwap).astype(int)


# ============================================================
# short momentum
# ============================================================

def _short_momentum(df: pd.DataFrame) -> pd.Series:

    close = _safe_series(df, "close")

    mom = (
        close.groupby(df["symbol"])
        .pct_change(MOM_WINDOW)
    )

    return _sanitize(mom)


# ============================================================
# liquidity guard
# ============================================================

def _liquidity_guard(df: pd.DataFrame) -> pd.Series:

    if "turnover" not in df.columns:
        return pd.Series(1, index=df.index)

    t = _safe_series(df, "turnover")

    return (t > 1e6).astype(int)


# ============================================================
# main
# ============================================================

def apply_ignition(
    df: pd.DataFrame,
    *,
    normalize: bool = True
) -> pd.DataFrame:
    """
    初動検知エンジン

    出力:
        df["ignition_score"]
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
        brk = _breakout(df)
        vol = _volume_spike(df)
        comp = _compression(df)
        vwap = _vwap_break(df)
        mom = _short_momentum(df)
        liq = _liquidity_guard(df)

        # ----------------------------------------------------
        # core signal
        # ----------------------------------------------------
        signal = (
            brk * 0.30 +
            vol * 0.25 +
            comp * 0.15 +
            vwap * 0.15 +
            mom * 0.15
        )

        signal = _sanitize(signal)

        # ----------------------------------------------------
        # liquidity guard
        # ----------------------------------------------------
        signal = signal * liq

        # ----------------------------------------------------
        # normalize
        # ----------------------------------------------------
        if normalize:
            signal = _normalize(signal)

        df["ignition_score"] = signal

        return df

    except Exception:

        logger.exception("[ignition] apply failed")

        df["ignition_score"] = 0
        return df


# ============================================================
# utility
# ============================================================

def latest_ignition(df: pd.DataFrame):

    if df is None or df.empty:
        return 0

    if "ignition_score" not in df.columns:
        return 0

    try:
        return float(df["ignition_score"].iloc[-1])
    except Exception:
        return 0