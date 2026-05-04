# ============================================================
# AI/decision/final_decision_ai.py
# FINAL_DECISION 推論AI（GO / HOLD / DELAY）
# ------------------------------------------------------------
# ✔ LightGBM 3クラス分類
# ✔ EXIT直前の最終判断
# ✔ next_check_sec を返す（HOLDTIME制御）
# ✔ exit_controller から安全に呼び出し可能
# ============================================================

from pathlib import Path
import joblib
import logging

logger = logging.getLogger(__name__)

# ============================================================
# PATH
# ============================================================

MODEL_PATH = Path("AI/model/final_decision_lgbm.pkl")

# ============================================================
# クラス定義
# ============================================================

LABEL_MAP = {
    0: "DELAY",
    1: "HOLD",
    2: "GO",
}

# HOLD / DELAY 時の次チェック秒
NEXT_CHECK_MAP = {
    "DELAY": 15,   # 15秒後に再判定
    "HOLD": 30,    # 30秒後に再判定
    "GO": 0,       # 即EXIT
}

# ============================================================
# 特徴量（train と完全一致）
# ============================================================

FEATURES = [
    "pnl_pct",
    "holding_seconds",

    "is_short_hold",
    "is_mid_hold",
    "is_long_hold",

    "exit_stop",
    "exit_timeout",
    "exit_ai",

    "is_buy",
]

_model = None


# ============================================================
# モデルロード（遅延）
# ============================================================

def _load_model():
    global _model
    if _model is None:
        if not MODEL_PATH.exists():
            raise FileNotFoundError(f"FINAL_DECISION model not found: {MODEL_PATH}")
        _model = joblib.load(MODEL_PATH)
    return _model


# ============================================================
# FINAL DECISION 推論
# ============================================================

def infer_final_decision(features: dict) -> dict:
    """
    FINAL_DECISION 推論

    Returns
    -------
    dict
        {
            "decision": "GO" | "HOLD" | "DELAY",
            "next_check_sec": int,
            "confidence": float
        }
    """

    try:
        model = _load_model()

        # ----------------------------------------
        # 特徴量整形
        # ----------------------------------------
        X = [[features.get(k, 0) for k in FEATURES]]

        proba = model.predict_proba(X)[0]
        label_idx = int(proba.argmax())
        decision = LABEL_MAP[label_idx]

        confidence = float(proba[label_idx])

        return {
            "decision": decision,
            "next_check_sec": NEXT_CHECK_MAP[decision],
            "confidence": confidence,
        }

    except Exception as e:
        logger.exception("❌ FINAL_DECISION inference failed")

        # 安全側フォールバック
        return {
            "decision": "HOLD",
            "next_check_sec": 30,
            "confidence": 0.0,
        }


# ============================================================
# ユーティリティ（EXIT_CONTROLLER 用）
# ============================================================

def should_exit(features: dict) -> bool:
    """
    EXITしてよいか（True=EXIT）
    """
    res = infer_final_decision(features)
    return res["decision"] == "GO"
