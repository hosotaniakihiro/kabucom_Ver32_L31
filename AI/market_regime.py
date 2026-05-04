# ============================================================
# File   : pj/AI/market_regime.py
# Created: 2026-01-02
# ------------------------------------------------------------
# 市場地合い判定（AIクラスタ + ルールベース融合）
#
# return:
#   0 = 危険（TONOSAMA / ENTRY 完全停止）
#   1 = 弱い
#   2 = 通常
#   3 = 強い（イナゴ日）
#
# ・cluster_params が存在すれば AI 判定を最優先
# ・無い場合は指数 + 出来高 + 騰落のルール判定
# ============================================================

from global_state import global_data
from AI.build_cluster_params import assign_cluster


# ============================================================
# メイン
# ============================================================

def detect_market_regime() -> int:
    """
    市場地合いを 0〜3 で返す
    """

    # --------------------------------------------------------
    # ① AI クラスタ判定（最優先）
    # --------------------------------------------------------
    cluster_params = getattr(global_data, "cluster_params", None)

    if cluster_params:
        features = _build_feature_dict()
        try:
            return assign_cluster(features, cluster_params)
        except Exception:
            # AI 判定失敗時はルールベースへ
            pass

    # --------------------------------------------------------
    # ② ルールベース判定（フォールバック）
    # --------------------------------------------------------
    return _detect_market_regime_rule()


# ============================================================
# 特徴量構築（AI 用）
# ============================================================

def _build_feature_dict() -> dict:
    """
    global_data から市場特徴量を生成
    """

    idx = getattr(global_data, "market_index", {})

    nikkei_pct = idx.get("nikkei_change_pct")
    topix_pct = idx.get("topix_change_pct")
    adv = idx.get("advance", 0)
    dec = idx.get("decline", 0)
    volume_ratio = idx.get("market_volume_ratio", 1.0)

    # 旧構造 fallback
    nikkei = getattr(global_data, "market_nikkei", None)
    topix = getattr(global_data, "market_topix", None)

    if nikkei_pct is None and nikkei:
        nikkei_pct = nikkei.get("change_pct", 0)

    if topix_pct is None and topix:
        topix_pct = topix.get("change_pct", 0)

    nikkei_pct = nikkei_pct or 0.0
    topix_pct = topix_pct or 0.0

    advance_ratio = adv / max(adv + dec, 1)

    return {
        "nikkei_change_pct": nikkei_pct,
        "topix_change_pct": topix_pct,
        "market_volume_ratio": volume_ratio,
        "advance_ratio": advance_ratio,
    }


# ============================================================
# ルールベース判定
# ============================================================

def _detect_market_regime_rule() -> int:
    """
    AI が無い場合の安全な地合い判定
    """

    idx = getattr(global_data, "market_index", {})

    nikkei_pct = idx.get("nikkei_change_pct", 0)
    topix_pct = idx.get("topix_change_pct", 0)
    adv = idx.get("advance", 0)
    dec = idx.get("decline", 0)

    breadth = adv / max(adv + dec, 1)
    ret_avg = (nikkei_pct + topix_pct) / 2

    # 出来高（旧構造 fallback）
    vol_ratio = idx.get("market_volume_ratio", 1.0)

    nikkei = getattr(global_data, "market_nikkei", None)
    topix = getattr(global_data, "market_topix", None)

    if nikkei and topix and "market_volume_ratio" not in idx:
        vol_ratio = (
            nikkei.get("volume", 1)
            + topix.get("volume", 1)
        ) / 2

    # --------------------------------------------------------
    # 🚨 危険（最優先）
    # --------------------------------------------------------
    if ret_avg < -1.0 and breadth < 0.35:
        return 0

    # --------------------------------------------------------
    # 🔻 弱い
    # --------------------------------------------------------
    if ret_avg < -0.3:
        return 1

    # --------------------------------------------------------
    # 🔥 強い（イナゴ日）
    # --------------------------------------------------------
    if ret_avg > 0.8 and breadth > 0.6 and vol_ratio > 1.2:
        return 3

    # --------------------------------------------------------
    # ⚖ 通常
    # --------------------------------------------------------
    return 2
