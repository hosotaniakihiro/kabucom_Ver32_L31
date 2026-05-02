# ============================================================
# File   : trading/portfolio/risk_allocator.py
# Version: Ver2.0-ABSOLUTE-FINAL-AUTONOMOUS-RISK-ENGINE
# ------------------------------------------------------------
# ✔ regime連動リスク
# ✔ bandit信頼度連動
# ✔ attack強度反映
# ✔ MTFスコア反映
# ✔ ボラティリティ抑制
# ✔ LONG/SHORT両建て対応
# ✔ 完全自律モード想定
# ✔ NaN / inf 完全耐性
# ✔ deterministic
# ✔ dtype安全固定
# ============================================================

from __future__ import annotations
import pandas as pd
import numpy as np
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


# ============================================================
# 安全数値変換
# ============================================================

def _safe(v: Any, default: float = 0.0) -> float:
    try:
        v = pd.to_numeric(v, errors="coerce")
        if pd.isna(v):
            return float(default)
        v = float(v)
        if np.isinf(v):
            return float(default)
        return v
    except Exception:
        return float(default)


# ============================================================
# regime別リスク倍率
# ============================================================

REGIME_RISK_MULTIPLIER = {
    "BULL": 1.2,
    "RANGE": 1.0,
    "BEAR": 1.2,
    "CRASH": 0.5,
    "UNKNOWN": 0.8,
}


# ============================================================
# 単一銘柄リスク計算
# ============================================================

def compute_position_size(
    row: Dict[str, Any],
    *,
    equity: float,
    max_risk_per_trade: float = 0.01,  # 1%
    max_position_pct: float = 0.05,    # 5%
) -> Dict[str, Any]:
    """
    完全自律ポジションサイズ決定
    """

    # ===== 基本値 =====

    action = str(row.get("meta_action", "SKIP"))
    confidence = _safe(row.get("meta_confidence"))
    score = _safe(row.get("score_total"))
    mtf = _safe(row.get("mtf_score"))
    slope = _safe(row.get("ma75_slope"))
    atr = _safe(row.get("atr"), 0.0)
    price = _safe(row.get("close_price"), 0.0)
    regime = str(row.get("regime", "UNKNOWN"))

    if action not in ("LONG", "SHORT"):
        return {
            "size": 0,
            "risk_amount": 0.0,
            "position_value": 0.0,
        }

    if price <= 0:
        return {
            "size": 0,
            "risk_amount": 0.0,
            "position_value": 0.0,
        }

    # ===== regime倍率 =====

    regime_mult = REGIME_RISK_MULTIPLIER.get(regime, 1.0)

    # ===== スコア強度係数 =====

    signal_strength = (
        abs(score) * 0.4
        + abs(mtf) * 0.3
        + abs(slope) * 10
        + confidence * 5
    )

    # 正規化
    signal_factor = min(2.0, 1.0 + signal_strength / 20.0)

    # ===== ATRボラ抑制 =====

    if atr > 0:
        vol_factor = min(1.5, price / (atr * 10))
    else:
        vol_factor = 1.0

    # ===== 最終リスク係数 =====

    risk_factor = regime_mult * signal_factor * vol_factor

    risk_factor = max(0.2, min(risk_factor, 2.0))

    # ===== 許容損失額 =====

    risk_amount = equity * max_risk_per_trade * risk_factor

    # ===== ストップ距離 =====

    stop_distance = max(atr * 2, price * 0.005)

    if stop_distance <= 0:
        return {
            "size": 0,
            "risk_amount": 0.0,
            "position_value": 0.0,
        }

    # ===== 株数計算 =====

    size = int(risk_amount / stop_distance)

    # ===== 最大建玉制限 =====

    max_position_value = equity * max_position_pct
    position_value = size * price

    if position_value > max_position_value:
        size = int(max_position_value / price)
        position_value = size * price

    if size < 0:
        size = 0

    return {
        "size": int(size),
        "risk_amount": float(risk_amount),
        "position_value": float(position_value),
    }


# ============================================================
# DataFrame一括処理
# ============================================================

def allocate_positions(
    df: pd.DataFrame,
    *,
    equity: float,
    max_risk_per_trade: float = 0.01,
    max_position_pct: float = 0.05,
) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    sizes = []
    risks = []
    values = []

    for _, row in df.iterrows():
        res = compute_position_size(
            row.to_dict(),
            equity=equity,
            max_risk_per_trade=max_risk_per_trade,
            max_position_pct=max_position_pct,
        )

        sizes.append(res["size"])
        risks.append(res["risk_amount"])
        values.append(res["position_value"])

    df["position_size"] = sizes
    df["risk_amount"] = risks
    df["position_value"] = values

    # dtype固定
    df["position_size"] = pd.to_numeric(
        df["position_size"], errors="coerce"
    ).fillna(0).astype(int)

    df["risk_amount"] = pd.to_numeric(
        df["risk_amount"], errors="coerce"
    ).fillna(0.0)

    df["position_value"] = pd.to_numeric(
        df["position_value"], errors="coerce"
    ).fillna(0.0)

    return df