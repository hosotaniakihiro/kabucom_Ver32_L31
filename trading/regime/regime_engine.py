# ============================================================
# File   : trading/regime/regime_engine.py
# Version: FINAL-ROBUST-MULTI-FACTOR-REGIME-ENGINE
# ------------------------------------------------------------
# ✔ 旧 slope 判定完全保持
# ✔ RSI / ATR / ボラティリティ統合
# ✔ トレンド強度算出
# ✔ regime_confidence 出力
# ✔ NaN / 空データ完全耐性
# ✔ 戻り値互換保証 {"regime": "..."}
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)


# ============================================================
# 内部ユーティリティ
# ============================================================

def _safe_last(series: pd.Series):
    try:
        if series is None or series.empty:
            return None
        return series.iloc[-1]
    except Exception:
        return None


def _calc_rsi(close: pd.Series, window: int = 14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(window, min_periods=window).mean()
    avg_loss = loss.rolling(window, min_periods=window).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _calc_atr(df: pd.DataFrame, window: int = 14):
    if not {"high", "low", "close"}.issubset(df.columns):
        return None

    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    return tr.rolling(window, min_periods=window).mean()


# ============================================================
# メイン
# ============================================================

def detect_market_regime(
    index_df: pd.DataFrame | None,
    *,
    slope_window: int = 5,
    atr_window: int = 14,
    rsi_window: int = 14,
) -> dict:

    # 互換維持：最低限これを返す
    default_return = {
        "regime": "neutral",
        "trend_strength": 0.0,
        "volatility": 0.0,
        "rsi": None,
        "confidence": 0.0,
    }

    if index_df is None or index_df.empty:
        logger.warning("[REGIME] index_df empty → neutral")
        return default_return

    df = index_df.copy()

    if "close" not in df.columns:
        logger.warning("[REGIME] close column missing → neutral")
        return default_return

    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])

    if len(df) < slope_window + 2:
        logger.warning("[REGIME] insufficient data → neutral")
        return default_return

    # ========================================================
    # ① トレンド（旧ロジック完全保持）
    # ========================================================

    slope_series = df["close"].diff().rolling(slope_window).mean()
    slope = _safe_last(slope_series)

    trend_strength = 0.0
    regime = "neutral"

    if slope is not None:
        trend_strength = float(slope)

        if slope > 0:
            regime = "bull"
        elif slope < 0:
            regime = "bear"

    # ========================================================
    # ② ボラティリティ
    # ========================================================

    atr_series = _calc_atr(df, window=atr_window)
    atr = _safe_last(atr_series)

    volatility = float(atr) if atr is not None else 0.0

    # ========================================================
    # ③ RSI
    # ========================================================

    rsi_series = _calc_rsi(df["close"], window=rsi_window)
    rsi = _safe_last(rsi_series)

    # ========================================================
    # ④ 信頼度計算
    # ========================================================

    confidence = 0.0

    try:
        if slope is not None:
            confidence += min(abs(slope) * 10, 0.5)

        if rsi is not None:
            if regime == "bull" and rsi > 55:
                confidence += 0.25
            elif regime == "bear" and rsi < 45:
                confidence += 0.25

        if volatility > 0:
            confidence += 0.1

        confidence = min(confidence, 1.0)

    except Exception:
        confidence = 0.0

    result = {
        "regime": regime,
        "trend_strength": trend_strength,
        "volatility": volatility,
        "rsi": rsi,
        "confidence": confidence,
    }

    logger.info(
        "[REGIME] regime=%s slope=%.6f vol=%.6f rsi=%s conf=%.2f",
        regime,
        trend_strength,
        volatility,
        str(rsi),
        confidence,
    )

    return result