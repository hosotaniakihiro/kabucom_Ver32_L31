# ============================================================
# File   : AI/collapse/collapse_fusion.py
# Version: V1.0-FINAL-COLLAPSE-FUSION-ENGINE
# ------------------------------------------------------------
# ✔ tick / pre / regime 融合
# ✔ 重み自動正規化
# ✔ 0〜1保証
# ✔ Bandit重み差し替え対応
# ✔ フェイルセーフ設計
# ✔ exit_loop高頻度呼び出し前提（超軽量）
# ============================================================

import logging

logger = logging.getLogger(__name__)


# ============================================================
# ユーティリティ
# ============================================================

def _clip01(x: float) -> float:
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    return float(x)


def _normalize_weights(weights: tuple) -> tuple:
    total = sum(weights)
    if total <= 0:
        # fallback安全値
        return (1/3, 1/3, 1/3)
    return tuple(w / total for w in weights)


# ============================================================
# メイン融合関数
# ============================================================

def fuse_collapse_scores(
    tick_strength: float,
    pre_strength: float,
    regime_strength: float,
    weights: tuple = (0.4, 0.3, 0.3),
) -> float:
    """
    3層融合エンジン

    入力:
        tick_strength   : Tick collapse (0〜1)
        pre_strength    : 予兆AI (0〜1)
        regime_strength : Regime別collapse (0〜1)

    weights:
        (tick_w, pre_w, regime_w)
        将来Banditで動的変更可能

    出力:
        final_collapse_strength (0〜1)
    """

    try:
        # --------------------------------------------------------
        # 安全クリップ
        # --------------------------------------------------------
        tick_strength = _clip01(tick_strength)
        pre_strength = _clip01(pre_strength)
        regime_strength = _clip01(regime_strength)

        # --------------------------------------------------------
        # 重み正規化
        # --------------------------------------------------------
        w_tick, w_pre, w_regime = _normalize_weights(weights)

        # --------------------------------------------------------
        # 融合
        # --------------------------------------------------------
        score = (
            w_tick * tick_strength
            + w_pre * pre_strength
            + w_regime * regime_strength
        )

        return _clip01(score)

    except Exception:
        logger.exception("[CollapseFusion] fuse failed")
        return 0.0


# ============================================================
# 拡張融合（将来用）
# ============================================================

def fuse_extended(
    components: dict,
    weights: dict,
) -> float:
    """
    将来拡張用の汎用融合

    components:
        {
            "tick": 0.6,
            "pre": 0.4,
            "regime": 0.7,
            "index_shock": 0.2,
            ...
        }

    weights:
        {
            "tick": 0.4,
            "pre": 0.3,
            "regime": 0.3,
            "index_shock": 0.1,
        }

    出力:
        0〜1
    """

    try:
        if not components:
            return 0.0

        # 欠損重みは0扱い
        total_weight = sum(weights.get(k, 0) for k in components.keys())

        if total_weight <= 0:
            return 0.0

        score = 0.0

        for name, value in components.items():
            w = weights.get(name, 0.0)
            score += _clip01(value) * w

        score /= total_weight

        return _clip01(score)

    except Exception:
        logger.exception("[CollapseFusion] extended fuse failed")
        return 0.0