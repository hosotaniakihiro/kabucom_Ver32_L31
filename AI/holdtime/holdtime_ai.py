# ============================================================
# AI/holdtime/holdtime_ai.py
# ------------------------------------------------------------
# ✔ 秒足AI専用 HOLDTIME 推定
# ✔ 5S / 10S 両対応
# ✔ ENTRY判断とは完全分離
# ✔ RISK_AI と自然連携
# ============================================================

from __future__ import annotations
import logging

logger = logging.getLogger("holdtime_ai")


# ============================================================
# 設定（実戦向け）
# ============================================================
HOLDTIME_CONFIG = {
    "5S": {
        "scale": 300,      # pred × 秒
        "min_sec": 10,
        "max_sec": 120,
    },
    "10S": {
        "scale": 450,
        "min_sec": 20,
        "max_sec": 240,
    },
}


# ============================================================
# メイン関数
# ============================================================
def estimate_holdtime_seconds(
    pred: float,
    timeframe: str,
    *,
    risk_level: float = 1.0,
) -> int:
    """
    pred:
      AI回帰値（将来リターン）
    timeframe:
      "5S" or "10S"
    risk_level:
      1.0 = 通常
      0.5 = 半分（RISK_AI 停止前）
    """

    cfg = HOLDTIME_CONFIG.get(timeframe)
    if not cfg:
        logger.warning(f"unknown timeframe={timeframe}")
        return 0

    if pred <= 0:
        return 0

    hold_sec = pred * cfg["scale"] * risk_level

    hold_sec = max(cfg["min_sec"], hold_sec)
    hold_sec = min(cfg["max_sec"], hold_sec)

    return int(hold_sec)
