# ============================================================
# File   : AI/index_shock_detector.py
# ------------------------------------------------------------
# 指数ショック検知モジュール
#
# 目的:
# ・指数の急落 / 急反発を最速検知
# ・誤エントリー / 逆張り事故を防止
# ・market_regime / crash_short_ai / exit と連動
#
# return:
#   0 = 通常
#   1 = 下方向ショック（急落）
#   2 = 上方向ショック（急反発）
# ============================================================

from typing import Optional, Dict, Any
import logging

from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# 設定（経験則）
# ============================================================

# 直近変化率しきい値（％）
DOWN_SHOCK_PCT = -0.6     # 急落
UP_SHOCK_PCT   = 0.6      # 急反発

# 騰落レシオ（投げ / 踏み上げ）
DOWN_SHOCK_BREADTH = 0.30
UP_SHOCK_BREADTH   = 0.70

# 出来高（パニック / 踏み上げ）
MIN_VOLUME_RATIO = 1.3

# 判定に使う直近本数
LOOKBACK = 3


# ============================================================
# メイン
# ============================================================

def detect_index_shock(
    features: Optional[Dict[str, Any]] = None
) -> int:
    """
    指数ショックを検知

    return:
        0 = 通常
        1 = 下方向ショック（急落）
        2 = 上方向ショック（急反発）
    """

    try:
        # ----------------------------------------------------
        # 特徴量構築
        # ----------------------------------------------------
        if features is None:
            features = _build_feature_dict()

        nikkei_pct_series = features.get("nikkei_change_pct_series") or []
        breadth_series = features.get("advance_ratio_series") or []
        vol_ratio = float(features.get("market_volume_ratio", 1.0))

        if len(nikkei_pct_series) < LOOKBACK:
            return 0

        # ----------------------------------------------------
        # 直近変化
        # ----------------------------------------------------
        try:
            recent_change = (
                nikkei_pct_series[-1] - nikkei_pct_series[-LOOKBACK]
            )
        except Exception:
            return 0

        recent_breadth = (
            breadth_series[-1]
            if breadth_series and breadth_series[-1] is not None
            else 0.5
        )

        logger.debug(
            "[INDEX_SHOCK_CHECK] "
            f"Δpct={recent_change:.3f} "
            f"breadth={recent_breadth:.3f} "
            f"vol_ratio={vol_ratio:.2f}"
        )

        # ----------------------------------------------------
        # 下方向ショック（急落）
        # ----------------------------------------------------
        if (
            recent_change <= DOWN_SHOCK_PCT
            and recent_breadth <= DOWN_SHOCK_BREADTH
            and vol_ratio >= MIN_VOLUME_RATIO
        ):
            logger.warning("[INDEX_SHOCK] DOWN detected")
            return 1

        # ----------------------------------------------------
        # 上方向ショック（急反発）
        # ----------------------------------------------------
        if (
            recent_change >= UP_SHOCK_PCT
            and recent_breadth >= UP_SHOCK_BREADTH
            and vol_ratio >= MIN_VOLUME_RATIO
        ):
            logger.warning("[INDEX_SHOCK] UP detected")
            return 2

        return 0

    except Exception:
        logger.exception("[INDEX_SHOCK_FATAL]")
        return 0


# ============================================================
# 特徴量構築
# ============================================================

def _build_feature_dict() -> Dict[str, Any]:
    """
    global_data から指数ショック用特徴量を生成
    """

    idx = getattr(global_data, "market_index", None)
    if not isinstance(idx, dict):
        return {
            "nikkei_change_pct_series": [],
            "advance_ratio_series": [],
            "market_volume_ratio": 1.0,
        }

    nikkei_series = idx.get("nikkei_change_pct_series") or []
    adv_series = idx.get("advance_series") or []
    dec_series = idx.get("decline_series") or []

    volume_ratio = float(idx.get("market_volume_ratio", 1.0))

    # --------------------------------------------------------
    # 騰落レシオ series
    # --------------------------------------------------------
    breadth_series = []
    for a, d in zip(adv_series, dec_series):
        try:
            a = float(a)
            d = float(d)
            breadth_series.append(a / max(a + d, 1))
        except Exception:
            breadth_series.append(0.5)

    return {
        "nikkei_change_pct_series": nikkei_series,
        "advance_ratio_series": breadth_series,
        "market_volume_ratio": volume_ratio,
    }
