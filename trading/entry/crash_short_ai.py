# ============================================================
# File   : pj/trading/entry/crash_short_ai.py
# Created: 2026-01-02
# ------------------------------------------------------------
# 市場クラッシュ対応 SHORT（SELL）判定 AI
#
# 目的:
# ・地合い悪化時のみショートを許可
# ・誤爆（レンジ・反発初動）を防止
# ・TONOSAMA / イナゴ地合いでは完全停止
#
# return:
#   True  = SHORT 許可
#   False = SHORT 禁止
# ============================================================

from global_state import global_data
from AI.build_cluster_params import assign_cluster
from AI.market_regime import detect_market_regime
from AI.index_shock_detector import detect_index_shock  # ★ 追加


# ============================================================
# 設定（経験則ベース）
# ============================================================

# クラッシュ判定しきい値
CRASH_NIKKEI_PCT = -1.0
CRASH_TOPIX_PCT = -0.8
CRASH_BREADTH = 0.35

# ボラティリティ（急落時のみ）
MIN_VOLATILITY_INDEX = 0.3

# 出来高（投げが出ているか）
MIN_VOLUME_RATIO = 1.1


# ============================================================
# メイン
# ============================================================

def allow_crash_short(features: dict | None = None) -> bool:
    """
    市場状況から SHORT（SELL）を許可するか判定

    features:
        {
            "nikkei_change_pct": float,
            "topix_change_pct": float,
            "advance_ratio": float,
            "market_volume_ratio": float,
            "volatility_index": float,
        }
        ※ None の場合は global_data から生成
    """

    # --------------------------------------------------------
    # 🚨 0. 指数ショック（急反発）最優先ガード
    # --------------------------------------------------------
    # 上方向ショック中は SHORT は即禁止
    if detect_index_shock() == 2:
        return False

    # --------------------------------------------------------
    # ① 地合い regime 判定
    # --------------------------------------------------------
    regime = detect_market_regime()

    # 強い / 通常 地合いでは SHORT 禁止
    if regime >= 2:
        return False

    # --------------------------------------------------------
    # ② 特徴量構築
    # --------------------------------------------------------
    if features is None:
        features = _build_feature_dict()

    nikkei_pct = features.get("nikkei_change_pct", 0.0)
    topix_pct = features.get("topix_change_pct", 0.0)
    breadth = features.get("advance_ratio", 0.5)
    vol_ratio = features.get("market_volume_ratio", 1.0)
    volatility = features.get("volatility_index", 0.0)

    # --------------------------------------------------------
    # ③ 明確なクラッシュ条件（ルール）
    # --------------------------------------------------------
    crash_cond = (
        nikkei_pct <= CRASH_NIKKEI_PCT
        and topix_pct <= CRASH_TOPIX_PCT
        and breadth <= CRASH_BREADTH
        and vol_ratio >= MIN_VOLUME_RATIO
        and volatility >= MIN_VOLATILITY_INDEX
    )

    if not crash_cond:
        return False

    # --------------------------------------------------------
    # ④ AI クラスタによる最終確認（あれば）
    # --------------------------------------------------------
    cluster_params = getattr(global_data, "cluster_params", None)

    if cluster_params:
        try:
            cluster_regime = assign_cluster(features, cluster_params)

            # AI的に「危険（0）」または「弱い（1）」のみ許可
            if cluster_regime <= 1:
                return True
            else:
                return False

        except Exception:
            # AI 失敗時はルールベースに従う
            return True

    # --------------------------------------------------------
    # ⑤ AI が無い場合
    # --------------------------------------------------------
    return True


# ============================================================
# 特徴量構築
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
    volatility = idx.get("volatility_index", 0.0)

    # 旧構造 fallback
    nikkei = getattr(global_data, "market_nikkei", None)
    topix = getattr(global_data, "market_topix", None)

    if nikkei_pct is None and nikkei:
        nikkei_pct = nikkei.get("change_pct", 0.0)

    if topix_pct is None and topix:
        topix_pct = topix.get("change_pct", 0.0)

    nikkei_pct = nikkei_pct or 0.0
    topix_pct = topix_pct or 0.0

    advance_ratio = adv / max(adv + dec, 1)

    return {
        "nikkei_change_pct": nikkei_pct,
        "topix_change_pct": topix_pct,
        "advance_ratio": advance_ratio,
        "market_volume_ratio": volume_ratio,
        "volatility_index": volatility,
    }
