# ============================================================
# pj/trading/exit/loss_guard_ai.py
# TONOSAMA 負け方専用 AI（損失最小化）
# ============================================================

from pathlib import Path
import joblib

MODEL_PATH = Path("AI/model/tonosama_loss_guard_lgbm.pkl")
_model = None


def _load():
    global _model
    if _model is None and MODEL_PATH.exists():
        _model = joblib.load(MODEL_PATH)
    return _model


def should_cut_loss(features: dict) -> bool:
    """
    return:
        True  -> 即 EXIT（負け確定回避）
        False -> 継続
    """

    model = _load()
    if not model:
        return False  # fallback（既存ルール任せ）

    X = [[
        features.get("fast_ret", 0),
        features.get("volume_speed", 0),
        features.get("ai_confidence", 0),
        features.get("hold_seconds", 0),
        features.get("pnl_pct", 0),
    ]]

    # 「このまま持つと負ける確率」
    lose_prob = float(model.predict_proba(X)[0][1])

    return lose_prob >= 0.65
