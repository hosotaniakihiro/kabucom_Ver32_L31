# ============================================================
# AI/infer/realtime_predictor.py
# ------------------------------------------------------------
# ✔ 時間足別モデル推論
# ✔ 落ちない設計（None返却）
# ✔ entry_controller 専用
# ============================================================

from pathlib import Path
from typing import Dict, Optional
import joblib
import pandas as pd
import logging

from global_state import global_data

logger = logging.getLogger(__name__)

MODEL_DIR = Path("AI/models")

# 使用する時間足（確定）
TIMEFRAMES = ["1M", "2M", "10S", "1S"]

# キャッシュ（起動後1回だけロード）
_MODEL_CACHE = {}

FEATURE_COLS = [
    "ret",
    "body",
    "range",
    "vol_ratio",
    "fast_ret",
]

# ============================================================
# モデル取得（キャッシュ）
# ============================================================
def _get_model(tf: str):
    if tf in _MODEL_CACHE:
        return _MODEL_CACHE[tf]

    path = MODEL_DIR / f"model_{tf}.pkl"
    if not path.exists():
        logger.warning(f"[AI] model not found: {path}")
        _MODEL_CACHE[tf] = None
        return None

    try:
        model = joblib.load(path)
        _MODEL_CACHE[tf] = model
        logger.info(f"[AI] model loaded: {tf}")
        return model
    except Exception:
        logger.exception(f"[AI] failed to load model: {tf}")
        _MODEL_CACHE[tf] = None
        return None


# ============================================================
# メインAPI
# ============================================================
def predict_multi_tf(symbol: str) -> Dict[str, Optional[float]]:
    """
    戻り値例:
    {
        "1M": 0.0021,
        "2M": -0.0008,
        "10S": 0.0012,
        "1S": -0.3,
    }
    """
    preds = {}

    for tf in TIMEFRAMES:
        model = _get_model(tf)
        if model is None:
            preds[tf] = None
            continue

        # 最新サマリー取得（既存 global_data 前提）
        df = global_data.get_latest_summary(tf, symbol)
        if df is None or df.empty:
            preds[tf] = None
            continue

        try:
            X = df[FEATURE_COLS].astype(float)
            pred = float(model.predict(X)[0])
            preds[tf] = pred
        except Exception:
            logger.exception(f"[AI] predict failed: {symbol} {tf}")
            preds[tf] = None

    return preds
