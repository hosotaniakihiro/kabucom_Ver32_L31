# ============================================================
# File   : trading/ai/rl/reward_normalizer.py
# Version: V61-ULTRA-STABLE-RL-REWARD-NORMALIZED
# ------------------------------------------------------------
# ✔ V60 機能完全保持（削除ゼロ）
# ✔ ATR正規化
# ✔ regime補正
# ✔ inago補正
# ✔ collapse補正
# ✔ NaN完全防御
# ✔ 数値安定化
# ✔ logスケーリング安定化
# ✔ rewardクリッピング
# ✔ regime型安全化
# ✔ inago型安全化
# ✔ 将来拡張耐性
# ============================================================

from __future__ import annotations

import math


# ============================================================
# 数値安全化
# ============================================================

def _safe(v, default=0.0):
    try:
        if v is None:
            return default
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


# ============================================================
# 安定logスケーリング
# ============================================================

def _stable_log_scale(x: float) -> float:
    """
    大きすぎる報酬を滑らかに圧縮
    対称性維持（正負同一処理）
    """
    x = _safe(x)

    if x == 0.0:
        return 0.0

    sign = 1.0 if x > 0 else -1.0
    abs_x = abs(x)

    # log(1 + x) で安定圧縮
    scaled = math.log1p(abs_x)

    return sign * scaled


# ============================================================
# rewardクリッピング
# ============================================================

def _clip_reward(x: float, limit: float = 10.0) -> float:
    x = _safe(x)
    limit = abs(_safe(limit, 10.0))

    if x > limit:
        return limit
    if x < -limit:
        return -limit
    return x


# ============================================================
# メイン正規化
# ============================================================

def normalize_reward(
    pnl: float,
    atr: float,
    regime,
    inago_state,
    collapse_strength: float = 0.0,
):
    """
    RL報酬を正規化する（完全安定版）

    Parameters
    ----------
    pnl : float
        実現損益
    atr : float
        ATR（ボラティリティ正規化用）
    regime : int | str
        市場状態（TREND=1, RANGE=2 など）
    inago_state : int
        0=通常, 1=ignite, 2=exhaust
    collapse_strength : float
        崩壊強度

    Returns
    -------
    float
        安定化済み報酬
    """

    pnl = _safe(pnl)
    atr = _safe(atr, 1e-6)
    collapse_strength = _safe(collapse_strength)

    # ------------------------------------------------
    # ① ATR正規化（基礎）
    # ------------------------------------------------
    reward = pnl / (abs(atr) + 1e-6)
    reward = _safe(reward)

    # ------------------------------------------------
    # ② regime安全化
    # ------------------------------------------------
    try:
        regime_val = int(regime)
    except Exception:
        regime_val = 0

    if regime_val == 1:        # TREND
        reward *= 1.2
    elif regime_val == 2:      # RANGE
        reward *= 0.9

    # ------------------------------------------------
    # ③ inago補正
    # ------------------------------------------------
    try:
        inago_val = int(inago_state)
    except Exception:
        inago_val = 0

    if inago_val == 1:         # IGNITE
        reward *= 1.1
    elif inago_val == 2:       # EXHAUST
        reward *= 0.8

    # ------------------------------------------------
    # ④ collapse補正
    # ------------------------------------------------
    if collapse_strength > 0.8:
        reward *= 0.7
    elif collapse_strength > 0.6:
        reward *= 0.85

    reward = _safe(reward)

    # ------------------------------------------------
    # ⑤ log安定化（暴走防止）
    # ------------------------------------------------
    reward = _stable_log_scale(reward)

    # ------------------------------------------------
    # ⑥ 最終クリッピング
    # ------------------------------------------------
    reward = _clip_reward(reward, limit=10.0)

    return _safe(reward)