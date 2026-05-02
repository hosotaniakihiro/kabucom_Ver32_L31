# ============================================================
# File   : trading/ai/regime_detector.py
# Version: Ver1.0-ABSOLUTE-FINAL-SCALP-REGIME-DETECTOR
# ------------------------------------------------------------
# ✔ slope / ATR / RSI / volume_slope 使用
# ✔ NaN / inf 完全耐性
# ✔ pandas2 safe
# ✔ スキャル特化レジーム分類
# ✔ object dtype 完全排除
# ✔ 市場指数 / 個別銘柄 両対応
# ============================================================

from __future__ import annotations
import pandas as pd
import numpy as np
import logging

logger = logging.getLogger(__name__)


# ============================================================
# 数値安全化
# ============================================================

def _safe_float(v, default=0.0):
    try:
        v = float(v)
        if np.isinf(v) or np.isnan(v):
            return default
        return v
    except Exception:
        return default


# ============================================================
# レジーム判定コア
# ============================================================

def detect_regime(df: pd.DataFrame) -> str:
    """
    市場レジームを判定する

    Returns:
        "TREND_STRONG"
        "TREND_WEAK"
        "RANGE"
        "VOLATILE"
        "COLLAPSE"
        "UNKNOWN"
    """

    if df is None or df.empty:
        return "UNKNOWN"

    try:
        row = df.sort_values("datetime").iloc[-1]
    except Exception:
        return "UNKNOWN"

    slope = _safe_float(row.get("ma75_slope", 0))
    atr = _safe_float(row.get("atr", 0))
    rsi = _safe_float(row.get("rsi", 50))
    vol_slope = _safe_float(row.get("volume_slope", 0))
    close = _safe_float(row.get("close_price", row.get("close", 0)))

    if close <= 0:
        return "UNKNOWN"

    # ============================================================
    # ① 崩壊検知（最優先）
    # ============================================================
    if slope < -0.8 and rsi < 40:
        return "COLLAPSE"

    # ============================================================
    # ② ボラ急拡大
    # ============================================================
    # ATRを価格比で評価
    atr_ratio = atr / close if close else 0

    if atr_ratio > 0.03:
        return "VOLATILE"

    # ============================================================
    # ③ 強トレンド
    # ============================================================
    if slope > 0.5 and rsi > 55 and vol_slope > 0:
        return "TREND_STRONG"

    # ============================================================
    # ④ 弱トレンド
    # ============================================================
    if abs(slope) > 0.15:
        return "TREND_WEAK"

    # ============================================================
    # ⑤ レンジ
    # ============================================================
    if abs(slope) < 0.05 and 45 <= rsi <= 55:
        return "RANGE"

    # fallback
    return "TREND_WEAK"


# ============================================================
# スコア化（数値版レジーム強度）
# ============================================================

def regime_strength_score(df: pd.DataFrame) -> float:
    """
    レジームの強度を数値で返す（AI用）
    """

    regime = detect_regime(df)

    mapping = {
        "TREND_STRONG": 2.0,
        "TREND_WEAK": 1.0,
        "RANGE": 0.5,
        "VOLATILE": 1.5,
        "COLLAPSE": -2.0,
        "UNKNOWN": 0.0,
    }

    return mapping.get(regime, 0.0)


# ============================================================
# ログ付き判定（デバッグ用）
# ============================================================

def detect_regime_with_log(df: pd.DataFrame) -> str:

    regime = detect_regime(df)

    logger.info("[REGIME] detected=%s", regime)

    return regime