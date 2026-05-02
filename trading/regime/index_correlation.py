# ============================================================
# File   : trading/regime/index_correlation.py
# Version: FINAL-INDEX-CORRELATION-ENGINE-V2
# ------------------------------------------------------------
# ✔ 指数方向一致ブースト
# ✔ 逆行抑制
# ✔ 強度加味
# ✔ regime連動
# ✔ ボラ補正
# ✔ 攻撃型倍率出力
# ✔ None完全耐性
# ✔ 将来拡張（β補正対応）
# ============================================================

from __future__ import annotations
from typing import Dict, Any
import math


# ============================================================
# 安全取得ユーティリティ
# ============================================================

def _safe(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
    if row is None:
        return default
    val = row.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except Exception:
        return default


# ============================================================
# 方向一致判定倍率
# ============================================================

def index_alignment_multiplier(
    symbol_row: Dict[str, Any],
    index_row: Dict[str, Any],
    *,
    regime: str = "RANGE",
    use_strength: bool = True,
) -> float:
    """
    指数と銘柄の方向一致度に応じて倍率を返す

    return:
        0.5 〜 1.8 程度の倍率
    """

    stock_slope = _safe(symbol_row, "ma75_slope")
    index_slope = _safe(index_row, "ma75_slope")

    stock_atr = _safe(symbol_row, "atr")
    index_atr = _safe(index_row, "atr")

    if stock_slope == 0 or index_slope == 0:
        return 1.0

    same_direction = stock_slope * index_slope > 0

    # --------------------------------------------------------
    # 基本倍率
    # --------------------------------------------------------

    if same_direction:
        base = 1.15
    else:
        base = 0.65

    # --------------------------------------------------------
    # 強度補正
    # --------------------------------------------------------

    if use_strength:
        strength = abs(stock_slope) * 0.6 + abs(index_slope) * 0.4
        base *= (1 + min(strength * 5, 0.6))  # 上限制御

    # --------------------------------------------------------
    # regime補正
    # --------------------------------------------------------

    if regime == "TREND_STRONG":
        if same_direction:
            base *= 1.2
        else:
            base *= 0.8

    elif regime == "LOW_VOL":
        base *= 0.9

    # --------------------------------------------------------
    # ボラ補正（指数が荒れている場合抑制）
    # --------------------------------------------------------

    if index_atr > stock_atr * 2:
        base *= 0.85

    return max(0.5, min(base, 1.8))


# ============================================================
# β補正（将来用）
# ============================================================

def beta_adjusted_multiplier(
    symbol_row: Dict[str, Any],
    index_row: Dict[str, Any],
    *,
    beta: float = 1.0,
) -> float:
    """
    β（指数感応度）を考慮した倍率

    beta > 1 → ハイベータ銘柄
    beta < 1 → ディフェンシブ
    """

    stock_slope = _safe(symbol_row, "ma75_slope")
    index_slope = _safe(index_row, "ma75_slope")

    if stock_slope * index_slope <= 0:
        return 0.8

    multiplier = 1 + abs(index_slope) * beta * 2

    return min(multiplier, 2.0)


# ============================================================
# 相関スコア（AI特徴量用）
# ============================================================

def index_correlation_feature(
    symbol_row: Dict[str, Any],
    index_row: Dict[str, Any],
) -> float:
    """
    -1.0 ～ +1.0 の連続値
    AI入力用特徴量
    """

    stock_slope = _safe(symbol_row, "ma75_slope")
    index_slope = _safe(index_row, "ma75_slope")

    if stock_slope == 0 or index_slope == 0:
        return 0.0

    sign = 1 if stock_slope * index_slope > 0 else -1
    magnitude = min(abs(stock_slope * index_slope) * 50, 1.0)

    return sign * magnitude


# ============================================================
# 攻撃型最終倍率統合
# ============================================================

def aggressive_index_multiplier(
    symbol_row: Dict[str, Any],
    index_row: Dict[str, Any],
    regime: str,
) -> float:
    """
    攻撃型エンジン用の最終指数補正倍率
    """

    base = index_alignment_multiplier(
        symbol_row,
        index_row,
        regime=regime,
        use_strength=True,
    )

    beta_mult = beta_adjusted_multiplier(symbol_row, index_row)

    final = base * beta_mult

    return max(0.4, min(final, 2.2))