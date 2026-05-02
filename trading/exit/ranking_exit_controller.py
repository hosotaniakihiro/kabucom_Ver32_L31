# ============================================================
# File   : trading/exit/ranking_exit_controller.py
# Ver1.0-FINAL-RANKING-FAST-EXIT
# ------------------------------------------------------------
# ✔ ランキング由来ポジション専用 EXIT
# ✔ 通常ポジションと完全分離
# ✔ 時間・失速・TP/SL の3系統
# ✔ AI 未使用（まずは安全運用）
# ✔ exit_reason を必ず記録
# ============================================================

from __future__ import annotations

import time
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

# ============================================================
# 設定値（後で config 化可）
# ============================================================

# 最大保有時間（秒）
RANKING_MAX_HOLD_SEC = 60        # ★まずは60秒推奨

# 利確 / 損切（％）
RANKING_TAKE_PROFIT = 0.004     # +0.4%
RANKING_STOP_LOSS   = -0.003    # -0.3%

# 失速判定
MIN_FAST_RET = 0.0              # マイナスに転じたら即
MIN_ACCEL    = 0.0              # 加速が死んだら即


# ============================================================
def should_exit_ranking_position(
    position: Dict[str, Any],
    latest_features: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    ランキング由来ポジションの EXIT 判定

    Args:
        position:
            {
                symbol,
                entry_price,
                entry_time (timestamp),
                side,
                source="RANKING",
                ...
            }

        latest_features:
            {
                price,
                fast_ret,
                accel,
                ...
            }

    Returns:
        {
            "exit": bool,
            "reason": str | None
        }
    """

    # --------------------------------------------------------
    # ガード
    # --------------------------------------------------------
    if position is None:
        return {"exit": False, "reason": None}

    if position.get("source") != "RANKING":
        return {"exit": False, "reason": None}

    now = time.time()

    entry_time = position.get("entry_time")
    entry_price = position.get("entry_price")

    if entry_time is None or entry_price is None:
        logger.warning("[RANKING EXIT] entry_time / entry_price missing")
        return {"exit": False, "reason": None}

    holding_sec = now - entry_time

    # ========================================================
    # ① 時間 EXIT（最優先）
    # ========================================================
    if holding_sec >= RANKING_MAX_HOLD_SEC:
        return {
            "exit": True,
            "reason": "RANKING_TIMEOUT"
        }

    # --------------------------------------------------------
    # 最新価格
    # --------------------------------------------------------
    if latest_features is None:
        return {"exit": False, "reason": None}

    price = latest_features.get("price")
    if price is None:
        return {"exit": False, "reason": None}

    # --------------------------------------------------------
    # リターン計算
    # --------------------------------------------------------
    if position.get("side") == "BUY":
        ret = (price - entry_price) / entry_price
    else:
        ret = (entry_price - price) / entry_price

    # ========================================================
    # ② 利確 / 損切
    # ========================================================
    if ret >= RANKING_TAKE_PROFIT:
        return {
            "exit": True,
            "reason": "RANKING_TAKE_PROFIT"
        }

    if ret <= RANKING_STOP_LOSS:
        return {
            "exit": True,
            "reason": "RANKING_STOP_LOSS"
        }

    # ========================================================
    # ③ 失速 EXIT（ランキングの本質）
    # ========================================================
    fast_ret = latest_features.get("fast_ret")
    accel    = latest_features.get("accel")

    if fast_ret is not None and fast_ret < MIN_FAST_RET:
        return {
            "exit": True,
            "reason": "RANKING_MOMENTUM_LOST_FAST_RET"
        }

    if accel is not None and accel < MIN_ACCEL:
        return {
            "exit": True,
            "reason": "RANKING_MOMENTUM_LOST_ACCEL"
        }

    # --------------------------------------------------------
    return {
        "exit": False,
        "reason": None
    }
