

# ============================================================
# File   : trading/ai/cluster_router.py
# Version: Ver1.0-ABSOLUTE-FINAL-REGIME-AWARE-ROUTER
# ------------------------------------------------------------
# ✔ regime_detector 連動
# ✔ スキャル特化ルーティング
# ✔ ロング / ショート両対応
# ✔ NaN / object 完全耐性
# ✔ fallback完全保証
# ✔ Bandit拡張ポイント付き
# ============================================================

from __future__ import annotations
import pandas as pd
import logging

from trading.ai.regime_detector import detect_regime

logger = logging.getLogger(__name__)


# ============================================================
# クラスタ定義
# ============================================================

CLUSTER_MAP = {

    # 強トレンド → ブレイク追随
    "TREND_STRONG": {
        "long": "TREND_BREAK_LONG",
        "short": "TREND_BREAK_SHORT",
    },

    # 弱トレンド → 押し目 / 戻り
    "TREND_WEAK": {
        "long": "PULLBACK_LONG",
        "short": "PULLBACK_SHORT",
    },

    # レンジ → 逆張り
    "RANGE": {
        "long": "MEAN_REVERT_LONG",
        "short": "MEAN_REVERT_SHORT",
    },

    # ボラ拡大 → スキャル高速
    "VOLATILE": {
        "long": "VOLATILITY_SCALP_LONG",
        "short": "VOLATILITY_SCALP_SHORT",
    },

    # 崩壊 → 防御 or 空売り特化
    "COLLAPSE": {
        "long": "DEFENSIVE_NO_LONG",
        "short": "COLLAPSE_SHORT",
    },

    "UNKNOWN": {
        "long": "SAFE_DEFAULT_LONG",
        "short": "SAFE_DEFAULT_SHORT",
    },
}


# ============================================================
# ルーティング
# ============================================================

def route_cluster(
    df: pd.DataFrame,
    side: str,
) -> str:
    """
    現在のレジームに応じて
    適切なクラスタ名を返す

    side: "long" or "short"
    """

    if side not in ("long", "short"):
        logger.warning("[ROUTER] invalid side=%s", side)
        return "SAFE_DEFAULT_LONG"

    regime = detect_regime(df)

    cluster = (
        CLUSTER_MAP
        .get(regime, CLUSTER_MAP["UNKNOWN"])
        .get(side)
    )

    if cluster is None:
        logger.warning(
            "[ROUTER] cluster fallback regime=%s side=%s",
            regime,
            side,
        )
        return "SAFE_DEFAULT_LONG"

    logger.info(
        "[ROUTER] regime=%s side=%s → cluster=%s",
        regime,
        side,
        cluster,
    )

    return cluster


# ============================================================
# 将来用：Bandit連携ポイント
# ============================================================

def route_with_bandit_override(
    df: pd.DataFrame,
    side: str,
    bandit_choice: str | None = None,
) -> str:
    """
    バンディットが戦略を上書きしたい場合に使用
    """

    base_cluster = route_cluster(df, side)

    if bandit_choice:
        logger.info(
            "[ROUTER] bandit override %s → %s",
            base_cluster,
            bandit_choice,
        )
        return bandit_choice

    return base_cluster