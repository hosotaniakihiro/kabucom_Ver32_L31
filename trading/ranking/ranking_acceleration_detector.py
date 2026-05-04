# ============================================================
# trading/ranking/ranking_acceleration_detector.py
# ------------------------------------------------------------
# ✔ ranking_ma_1min ベース初動加速検出
# ✔ snapshot 非依存（stateless）
# ✔ ranking / tonosama / PUSH 昇格共通
# ============================================================

from typing import Dict, List


# ============================================================
# 定数
# ============================================================
PUSH_PROMOTION_SCORE = 6
MIN_VOLUME_SPEED = 500.0
MIN_PRICE_ACCEL = 0.003   # 0.3%


# ============================================================
# ユーティリティ
# ============================================================
def _price_acceleration(rows: List[Dict]) -> float:
    """
    直近2本の close 変化率
    """
    if len(rows) < 2:
        return 0.0

    prev = rows[-2].get("close")
    curr = rows[-1].get("close")

    if not prev or not curr or prev <= 0:
        return 0.0

    return (curr - prev) / prev


# ============================================================
# メイン
# ============================================================
def detect_ranking_acceleration(
    *,
    symbol: str,
    ma_rows: List[Dict],
    trend_score: int,
    volume_speed: float,
) -> Dict | None:
    """
    初動加速を検出した場合のみ dict を返す
    """

    if not ma_rows or len(ma_rows) < 2:
        return None

    # --------------------------------------------------------
    # volume
    # --------------------------------------------------------
    if volume_speed < MIN_VOLUME_SPEED:
        return None

    # --------------------------------------------------------
    # price acceleration
    # --------------------------------------------------------
    accel = _price_acceleration(ma_rows)
    if accel < MIN_PRICE_ACCEL:
        return None

    # --------------------------------------------------------
    # promotion score
    # --------------------------------------------------------
    promotion_score = trend_score

    if accel >= 0.01:
        promotion_score += 2
    elif accel >= 0.005:
        promotion_score += 1

    if volume_speed >= 2000:
        promotion_score += 2
    elif volume_speed >= 1000:
        promotion_score += 1

    if promotion_score < PUSH_PROMOTION_SCORE:
        return None

    # --------------------------------------------------------
    # OK
    # --------------------------------------------------------
    return {
        "symbol": symbol,
        "promotion_score": promotion_score,
        "price_accel": accel,
        "volume_speed": volume_speed,
    }
