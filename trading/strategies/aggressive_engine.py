# ============================================================
# File   : trading/strategy/aggressive_engine.py
# Version: FINAL-AGGRESSIVE-ENGINE-V1
# ------------------------------------------------------------
# ✔ MTF強度ブースト
# ✔ 出来高爆発ブースト
# ✔ ランキング加速ブースト
# ✔ regime連動
# ✔ 可変ポジションサイズ
# ✔ EXIT強化
# ✔ バンディット報酬増幅対応
# ============================================================

from __future__ import annotations
from typing import Tuple
import math


# ============================================================
# ユーティリティ
# ============================================================

def _safe(row, key, default=0.0):
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
# MTF強度
# ============================================================

def mtf_strength(symbol, s3, s5):
    row3 = s3.get(symbol)
    row5 = s5.get(symbol)

    slope3 = _safe(row3, "ma75_slope")
    slope5 = _safe(row5, "ma75_slope")

    return slope3 * 0.6 + slope5 * 0.4


# ============================================================
# 出来高爆発検知
# ============================================================

def volume_explosion(symbol, s1):
    row = s1.get(symbol)
    if not row:
        return 0.0

    vol_slope = _safe(row, "volume_slope")
    rsi = _safe(row, "rsi")

    score = 0.0

    if vol_slope > 0.5:
        score += 0.5

    if rsi > 60 or rsi < 40:
        score += 0.3

    return score


# ============================================================
# ランキング加速ブースト
# ============================================================

def ranking_boost(symbol, ranking_snapshot):
    row = ranking_snapshot.get(symbol)
    if not row:
        return 0.0

    momentum = _safe(row, "ranking_momentum")
    persistence = _safe(row, "ranking_persistence")

    return momentum * 0.7 + persistence * 0.3


# ============================================================
# 攻撃型エントリー判定
# ============================================================

def aggressive_entry(
    symbol: str,
    side: str,
    summary_1,
    summary_3,
    summary_5,
    ranking_snapshot,
    regime: str,
) -> Tuple[bool, float]:

    row1 = summary_1.get(symbol)
    if not row1:
        return False, 1.0

    ai_score = _safe(row1, "score")

    # --- MTF強度 ---
    strength = mtf_strength(symbol, summary_3, summary_5)

    # --- 出来高ブースト ---
    vol_boost = volume_explosion(symbol, summary_1)

    # --- ランキングブースト ---
    rank_boost = ranking_boost(symbol, ranking_snapshot)

    # --- 攻撃倍率 ---
    aggression = 1.0
    aggression += abs(strength) * 2.0
    aggression += vol_boost
    aggression += rank_boost

    # regime補正
    if regime != "TREND_STRONG":
        aggression *= 0.6

    final_score = ai_score * aggression

    threshold = 12  # 攻撃型なので高め

    if side == "BUY":
        if final_score < threshold:
            return False, 1.0
    elif side == "SELL":
        if final_score > -threshold:
            return False, 1.0

    # --- 可変ポジションサイズ ---
    size_multiplier = min(2.5, 1 + abs(strength) * 3 + vol_boost)

    return True, size_multiplier


# ============================================================
# 攻撃型EXIT判定
# ============================================================

def aggressive_exit(
    symbol,
    side,
    summary_3,
    summary_5,
    current_profit,
):
    strength = mtf_strength(symbol, summary_3, summary_5)

    # --- 強逆行で即半分 ---
    if side == "BUY" and strength < -0.05:
        return "HALF_EXIT"

    if side == "SELL" and strength > 0.05:
        return "HALF_EXIT"

    # --- 利益伸ばし ---
    if current_profit > 0:
        return "TRAIL_EXPAND"

    return None


# ============================================================
# バンディット報酬増幅
# ============================================================

def bandit_reward_adjust(pnl, symbol, summary_3, summary_5):
    strength = abs(mtf_strength(symbol, summary_3, summary_5))
    return pnl * (1 + strength * 2.0)