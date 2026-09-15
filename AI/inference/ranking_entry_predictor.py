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
import os

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# model path
# ------------------------------------------------------------
# 以前は Path("AI/model/ranking_entry_lgbm.pkl") という相対パスで、
# cwd 依存だった。リポジトリルート基準の絶対パスへ解決する。
# env RANKING_ENTRY_MODEL_PATH で明示指定できる。
_REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_FILENAME = "ranking_entry_lgbm.pkl"


def _model_path_candidates() -> list[Path]:
    out: list[Path] = []
    try:
        env = os.getenv("RANKING_ENTRY_MODEL_PATH")
        if env and str(env).strip():
            out.append(Path(str(env).strip()))
    except Exception:
        logger.exception("[RANKING AI] RANKING_ENTRY_MODEL_PATH read failed")
    for rel in (
        Path("AI") / "model" / MODEL_FILENAME,
        Path("AI") / "models" / MODEL_FILENAME,
    ):
        out.append(_REPO_ROOT / rel)
        out.append(Path.cwd() / rel)
    # 重複除去 (順序維持)
    seen: set[str] = set()
    uniq: list[Path] = []
    for c in out:
        k = str(c)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def resolve_model_file() -> Path | None:
    for c in _model_path_candidates():
        try:
            if c.exists():
                return c
        except Exception:
            logger.exception("[RANKING AI] model path check failed path=%s", c)
    return None


_MISSING_LOGGED = False


def model_available() -> bool:
    """モデルが実在するか。無いことを 1 回だけログに残す。"""
    global _MISSING_LOGGED
    if _model is not None:
        return True
    found = resolve_model_file()
    if found is not None:
        return True
    if not _MISSING_LOGGED:
        _MISSING_LOGGED = True
        logger.error(
            "[RANKING AI] model=ABSENT ranking entry LGBM モデルが見つからない。"
            " 以後この銘柄群の ranking AI 推論は行わず fail-open で通す "
            "(confidence は AI 出力ではない)。探した場所=%s "
            "env RANKING_ENTRY_MODEL_PATH で明示指定できる。",
            [str(x) for x in _model_path_candidates()],
        )
    return False


MODEL_FILE = resolve_model_file() or (_REPO_ROOT / "AI" / "model" / MODEL_FILENAME)

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

    found = resolve_model_file()
    if found is None:
        raise FileNotFoundError(
            "ranking entry model not found (model=ABSENT): %s"
            % [str(x) for x in _model_path_candidates()]
        )

    obj = joblib.load(found)

    _model = obj.get("model")
    _label_encoder = obj.get("label_encoder")

    if _model is None or _label_encoder is None:
        raise ValueError("❌ invalid model file (missing model or label_encoder)")

    logger.info("✅ ranking_entry_lgbm model loaded path=%s", found)


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
def _derive_features(row: dict) -> dict:
    """価格スケールに依存しない比率特徴量を row に足す (学習側 train_ranking_entry.py の
    derive() と同じ名前・同じ式)。既にあるキーは上書きしない。"""
    out = dict(row or {})

    def f(k):
        try:
            v = float(out.get(k))
            return v if not (math.isnan(v) or math.isinf(v)) else 0.0
        except Exception:
            return 0.0

    close = f("close") or f("current_price") or f("price")

    def gap(k):
        v = f(k)
        return (close / v - 1.0) if (v > 0 and close > 0) else 0.0

    out.setdefault("ma5_gap", gap("ma5"))
    out.setdefault("ma25_gap", gap("ma25"))
    out.setdefault("ma75_gap", gap("ma75"))
    m5, m25 = f("ma5"), f("ma25")
    out.setdefault("ma5_ma25_gap", (m5 / m25 - 1.0) if (m5 > 0 and m25 > 0) else 0.0)
    for k in ("macd", "signal", "hist"):
        out.setdefault(f"{k}_pct", (f(k) / close) if close > 0 else 0.0)
    out.setdefault("vwap_gap", gap("vwap"))
    out.setdefault("log_volume", math.log1p(max(f("volume"), 0.0)))
    if "minute_of_day" not in out:
        try:
            import datetime as _dt
            dtv = out.get("datetime")
            if not isinstance(dtv, _dt.datetime):
                dtv = _dt.datetime.fromisoformat(str(dtv).replace("T", " ")[:19]) if dtv else _dt.datetime.now()
            out["minute_of_day"] = dtv.hour * 60 + dtv.minute
        except Exception:
            out["minute_of_day"] = 0
    return out


def _legacy_signal_required() -> bool:
    """旧 ma75_slope ベースの前段シグナルを必須にするか。
    2026-09-15: ランキング行に ma75_slope が無いと常に NONE になり、entry_gate が
    ranking_ai_mismatch で全件ブロックしてしまうため、既定では要求しない。"""
    v = os.getenv("RANKING_AI_LEGACY_SIGNAL_REQUIRED", "0")
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


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
    row = _derive_features(row)

    if _legacy_signal_required() and not ranking_signal(row):
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
