# ============================================================
# pj/trading/market/market_crash_ai.py
# 市場クラッシュ検知 AI（DI対応・最終版）
# ============================================================

from pathlib import Path
import joblib

from trading.market.market_features import build_market_features

MODEL_PATH = Path("AI/model/market_crash_lgbm.pkl")
_model = None


def _load():
    global _model
    if _model is None and MODEL_PATH.exists():
        _model = joblib.load(MODEL_PATH)
    return _model


def is_market_danger(global_data) -> bool:
    """
    市場クラッシュ判定（DI方式）

    Args:
        global_data: GlobalData

    Returns:
        True  -> 市場危険（ENTRY 全停止）
        False -> 通常運用
    """

    features = build_market_features(global_data)
    if not features:
        # データ不足時は「安全側」に倒す（停止しない）
        return False

    model = _load()

    # --------------------------------------------------------
    # ① ルールベース即死判定（最優先）
    # --------------------------------------------------------
    if (
        features.get("down_ratio", 0) > 0.65
        and features.get("fast_ret_mean", 0) < -0.15
    ):
        return True

    # --------------------------------------------------------
    # ② AI 判定（モデルが存在する場合のみ）
    # --------------------------------------------------------
    if model:
        X = [[
            features.get("up_ratio", 0),
            features.get("down_ratio", 0),
            features.get("fast_ret_mean", 0),
            features.get("ranking_volume_sum", 0),
        ]]
        crash_prob = float(model.predict_proba(X)[0][1])
        return crash_prob >= 0.6

    return False
