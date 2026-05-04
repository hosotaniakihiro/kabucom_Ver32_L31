# ============================================================
# AI/inference/model_loader.py
# ------------------------------------------------------------
# ✔ LightGBM model ローダー（推論専用）
# ✔ キャッシュ付き（毎回 load しない）
# ✔ モデル未存在・破損でも Runtime を落とさない
# ✔ ENTRY / HOLDTIME / EXIT 共通利用
# ============================================================

import logging
from pathlib import Path
import joblib
from threading import Lock
from typing import Optional, Any

logger = logging.getLogger(__name__)

# ============================================================
# モデル格納ディレクトリ
# ============================================================

MODEL_DIR = Path("AI/models")
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# 内部キャッシュ
# ============================================================

_MODEL_CACHE: dict[str, Optional[Any]] = {}
_MODEL_LOCK = Lock()


# ============================================================
# モデルロード（公開API）
# ============================================================

def load_model(timeframe: str):
    """
    指定時間足の LightGBM モデルを取得する（キャッシュ付き）

    Parameters
    ----------
    timeframe : str
        例: "1M", "2M", "5M", "10S", "1S"

    Returns
    -------
    model or None
        LGBMRegressor / Booster / sklearn wrapper
        失敗時は None（Runtime を止めない）
    """

    tf = str(timeframe).upper().strip()

    with _MODEL_LOCK:

        # 既にロード済み
        if tf in _MODEL_CACHE:
            return _MODEL_CACHE[tf]

        model_path = MODEL_DIR / f"model_{tf}.pkl"

        if not model_path.exists():
            logger.warning(
                f"[AI_MODEL_MISSING] {model_path}"
            )
            _MODEL_CACHE[tf] = None
            return None

        try:
            model = joblib.load(model_path)
            _MODEL_CACHE[tf] = model

            logger.info(
                f"[AI_MODEL_LOADED] {tf} -> {model_path}"
            )
            return model

        except Exception:
            logger.exception(
                f"[AI_MODEL_LOAD_ERROR] {model_path}"
            )
            _MODEL_CACHE[tf] = None
            return None


# ============================================================
# キャッシュクリア（再学習後用）
# ============================================================

def clear_model_cache(timeframe: Optional[str] = None):
    """
    モデルキャッシュをクリアする（再学習後用）

    Parameters
    ----------
    timeframe : str or None
        None の場合は全クリア
    """

    with _MODEL_LOCK:
        if timeframe is None:
            _MODEL_CACHE.clear()
            logger.info("[AI_MODEL_CACHE_CLEARED] all")
            return

        tf = str(timeframe).upper().strip()

        if tf in _MODEL_CACHE:
            _MODEL_CACHE.pop(tf, None)
            logger.info(
                f"[AI_MODEL_CACHE_CLEARED] {tf}"
            )
