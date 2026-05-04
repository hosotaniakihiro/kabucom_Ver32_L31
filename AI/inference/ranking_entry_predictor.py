# ============================================================
# AI/inference/ranking_entry_predictor.py
# ------------------------------------------------------------
# Ranking Entry AI Predictor
#
# ✔ LightGBM マルチクラス分類（BUY / SELL / NONE）
# ✔ entry_gate から row(dict) をそのまま受け取る
# ✔ feature list は model.feature_name() を絶対正とする
# ✔ lazy load（初回のみモデルロード）
# ✔ ranking 専用 MA75 slope ロジックを内包
# ✔ ranking × volume slope ロジックを統合（NEW）
# ✔ NaN / None / inf 完全ガード
# ✔ confidence（最大確率）を返却
# ============================================================

from pathlib import Path
import joblib
import numpy as np
import logging
import math

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# model path
# ------------------------------------------------------------
MODEL_FILE = Path("AI/model/ranking_entry_lgbm.pkl")

# ------------------------------------------------------------
# singleton
# ------------------------------------------------------------
_model = None
_label_encoder = None


# ------------------------------------------------------------
def _load_model():
    """
    モデル & LabelEncoder をロード（初回のみ）
    """
    global _model, _label_encoder

    if not MODEL_FILE.exists():
        raise FileNotFoundError(f"❌ ranking entry model not found: {MODEL_FILE}")

    obj = joblib.load(MODEL_FILE)

    _model = obj.get("model")
    _label_encoder = obj.get("label_encoder")

    if _model is None or _label_encoder is None:
        raise ValueError("❌ invalid model file (missing model or label_encoder)")

    logger.info("✅ ranking_entry_lgbm model loaded")


# ------------------------------------------------------------
def _safe_float(v, *, feature_name: str = "") -> float:
    """
    None / NaN / inf を完全に 0.0 に正規化
    """
    try:
        if v is None:
            return 0.0
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return 0.0
        return v
    except Exception:
        logger.debug(f"[AI] feature cast failed -> {feature_name}={v}")
        return 0.0


# ------------------------------------------------------------
def _build_feature_vector(row: dict, feature_names: list[str]) -> np.ndarray:
    """
    model.feature_name() を元に feature vector を構築
    """
    values = []

    for f in feature_names:
        if f not in row:
            logger.debug(f"[AI] missing feature -> {f} (fallback=0.0)")
            values.append(0.0)
            continue

        values.append(_safe_float(row.get(f), feature_name=f))

    return np.array([values], dtype=float)


# ------------------------------------------------------------
def ranking_ma_signal(row: dict) -> bool:
    """
    既存：MA75 slope ベースのランキングシグナル
    （後方互換用・削除しない）
    """

    slope = row.get("ma75_slope")
    conf = row.get("ma75_conf_ranking")

    try:
        slope = float(slope)
    except Exception:
        return False

    if slope >= 0.0015:
        return True

    if slope > 0:
        if conf is None:
            return True
        try:
            if float(conf) >= 0.4:
                return True
        except Exception:
            return True

    return False


# ------------------------------------------------------------
def ranking_signal(row: dict) -> bool:
    """
    ★ NEW：ランキング最終シグナル

    - MA75 slope
    - MA75 conf（ranking）
    - volume slope
    を総合判定
    """

    slope = row.get("ma75_slope")
    conf = row.get("ma75_conf_ranking")
    vol = row.get("volume_slope")

    if slope is None:
        return False

    try:
        slope_v = float(slope)
    except Exception:
        return False

    # --------------------------------------------------
    # ① 出来高伴う上昇は最優先
    # --------------------------------------------------
    try:
        if (
            slope_v > row.get("close", 0) * 0.001
            and vol is not None
            and float(vol) > 0
        ):
            return True
    except Exception:
        pass

    # --------------------------------------------------
    # ② conf が低くても角度が強ければ OK
    # --------------------------------------------------
    try:
        if slope_v > 0 and conf is not None and float(conf) > 0.4:
            return True
    except Exception:
        return True

    return False


# ------------------------------------------------------------
def predict_entry(row: dict) -> dict:
    """
    ranking entry AI 推論
    """
    global _model, _label_encoder

    if _model is None:
        _load_model()

    # --------------------------------------------------------
    # ranking 最終シグナル（前段フィルタ）
    # --------------------------------------------------------
    if not ranking_signal(row):
        return {
            "action": "NONE",
            "confidence": 0.0,
            "probabilities": {
                "BUY": 0.0,
                "SELL": 0.0,
                "NONE": 1.0,
            },
        }

    # --------------------------------------------------------
    # feature vector 作成
    # --------------------------------------------------------
    feature_names = list(_model.feature_name())
    X = _build_feature_vector(row, feature_names)

    # --------------------------------------------------------
    # predict probability
    # --------------------------------------------------------
    try:
        if hasattr(_model, "predict_proba"):
            proba = _model.predict_proba(X)[0]
        else:
            proba = _model.predict(X)[0]
    except Exception as e:
        raise RuntimeError(f"❌ ranking entry predict failed: {e}")

    # --------------------------------------------------------
    # decode result
    # --------------------------------------------------------
    idx = int(np.argmax(proba))
    action = _label_encoder.inverse_transform([idx])[0]
    confidence = float(proba[idx])

    prob_map = {
        label: float(proba[i])
        for i, label in enumerate(_label_encoder.classes_)
    }

    logger.info(
        "[RANKING_AI] action=%s conf=%.3f slope=%s conf_rk=%s vol_slope=%s",
        action,
        confidence,
        row.get("ma75_slope"),
        row.get("ma75_conf_ranking"),
        row.get("volume_slope"),
    )

    return {
        "action": action,
        "confidence": confidence,
        "probabilities": prob_map,
    }


# ------------------------------------------------------------
# CLI debug
# ------------------------------------------------------------
if __name__ == "__main__":
    dummy_row = {
        "rank": 1,
        "rank_diff": -1,
        "ranking_score": 90.0,
        "volume_speed": 3.1,
        "price_change_pct": 2.0,
        "price_velocity": 1.0,
        "vwap_distance": 0.5,
        "spread": 4,
        "atr": 14,
        "atr_ratio": 0.9,
        "volatility_1m": 0.6,
        "index_return_1m": 0.12,
        "index_volatility": 0.3,
        "is_breakout": 1,
        "is_pullback": 0,

        # ranking 専用
        "ma75_slope": 0.0012,
        "ma75_conf_ranking": 0.45,
        "volume_slope": 1200,
        "close": 1000,
    }

    r = predict_entry(dummy_row)
    print(r)
