# ============================================================
# File: AI/feature/ranking_score_direct.py
# Version: Ver1.1-FINAL-RANKING-SCORE-DIRECT-CANONICAL
# ------------------------------------------------------------
# ranking_session × MTF → ENTRY final_score 直結スコア
#
# ✔ ranking_session_features / mtf_ranking_fusion と完全整合
# ✔ tanh 正規化で暴発・異常値を完全抑制
# ✔ 欠損・NaN・inf・型事故 完全耐性
# ✔ ENTRY / EXIT / 学習 共通使用（READ ONLY）
# ✔ 他 AI（即益・構造・地合い）と安全に加算可能
# ✔ SHAP / 学習用に単調・連続性を保証
# ============================================================

import math
from typing import Dict


# ============================================================
# チューニングパラメータ（設計正本）
# ============================================================

# ranking_base_score に掛ける感度係数（α）
# 大きいほどランキングの影響が鋭くなる
RANKING_GAIN = 2.0

# final_score に加算する際の重み
# 1.0 = 他 AI と同格
RANKING_WEIGHT = 1.0


# ============================================================
# internal helpers（副作用なし）
# ============================================================

def _safe_float(v, default: float = 0.0) -> float:
    """
    数値変換の最終防波堤
    """
    try:
        v = float(v)
        if math.isfinite(v):
            return v
        return default
    except Exception:
        return default


# ============================================================
# ranking 直結スコア生成
# ============================================================

def build_ranking_direct_score(features: Dict) -> Dict[str, float]:
    """
    ranking_session_features + mtf_ranking_fusion から
    ENTRY / EXIT 用の ranking 直結スコアを生成する

    Parameters
    ----------
    features : dict
        既存の AI feature dict（READ ONLY 前提）

    Returns
    -------
    dict
        ranking スコア関連の追加特徴量
    """

    # --------------------------------------------------------
    # 必須特徴量（完全耐性）
    # --------------------------------------------------------
    rank_ret = _safe_float(
        features.get("ranking_session_rank_ret"), 0.0
    )
    rank_quality = _safe_float(
        features.get("ranking_session_quality"), 0.3
    )
    mtf_alignment = _safe_float(
        features.get("ranking_mtf_alignment"), 0.0
    )

    # --------------------------------------------------------
    # 基本スコア
    #
    # ranking_base_score =
    #   結果 × 信頼度 × 環境整合
    # --------------------------------------------------------
    ranking_base_score = (
        rank_ret
        * rank_quality
        * mtf_alignment
    )

    # --------------------------------------------------------
    # 正規化（暴発防止）
    #
    # tanh により [-1, +1] に収束
    # --------------------------------------------------------
    ranking_score_normalized = math.tanh(
        ranking_base_score * RANKING_GAIN
    )

    # --------------------------------------------------------
    # ENTRY / EXIT 用 final_score
    # --------------------------------------------------------
    ranking_score_final = (
        ranking_score_normalized
        * RANKING_WEIGHT
    )

    # --------------------------------------------------------
    # 追加特徴量（完全副作用ゼロ）
    # --------------------------------------------------------
    return {
        # 生スコア（学習・分析用）
        "ranking_base_score": ranking_base_score,

        # 正規化後（SHAP・比較用）
        "ranking_score_normalized": ranking_score_normalized,

        # ENTRY final_score / EXIT 判定に直接使用
        "ranking_score_final": ranking_score_final,
    }


# ============================================================
# standalone debug（任意・副作用なし）
# ============================================================

if __name__ == "__main__":
    # 疑似 feature での単体テスト
    test_features = {
        "ranking_session_rank_ret": 0.035,
        "ranking_session_quality": 0.7,
        "ranking_mtf_alignment": 0.8,
    }

    out = build_ranking_direct_score(test_features)

    print("[DEBUG] ranking_score_direct")
    for k, v in out.items():
        print(f"  {k}: {v:.6f}")