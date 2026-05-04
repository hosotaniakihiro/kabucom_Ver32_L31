# ============================================================
# AI/company_ai_injector.py
# Ver24-FINAL-BUY-SELL-CACHED
# ------------------------------------------------------------
# ✔ AI 配下 import 正常
# ✔ BUY / SELL モデル分離
# ✔ company_info 読み込みは1回だけ（高速）
# ✔ モデル未存在でも 0.0 を返して安全継続
# ============================================================

import logging
from pathlib import Path
import threading

import lightgbm as lgb
import pandas as pd

from AI.company_info_loader import load_company_features

logger = logging.getLogger(__name__)

# ============================================================
# パス設定（このファイル基準）
# ============================================================

_THIS_DIR = Path(__file__).resolve().parent

MODEL_PATH_BUY  = _THIS_DIR / "company_info_model_buy.txt"
MODEL_PATH_SELL = _THIS_DIR / "company_info_model_sell.txt"

# ============================================================
# キャッシュ
# ============================================================

_model_buy = None
_model_sell = None
_model_lock = threading.Lock()

_company_df = None
_company_lock = threading.Lock()


# ============================================================
# company_info 特徴量（1回だけロード）
# ============================================================

def _load_company_df() -> pd.DataFrame | None:
    global _company_df

    with _company_lock:
        if _company_df is not None:
            return _company_df

        try:
            df = load_company_features()
            if df is None or df.empty:
                logger.warning("[AI] company_info empty")
                _company_df = None
            else:
                _company_df = df.set_index("symbol")
                logger.info(f"[AI] company_info loaded rows={len(_company_df)}")
        except Exception as e:
            logger.error("[AI] company_info load failed", exc_info=True)
            _company_df = None

        return _company_df


# ============================================================
# モデルロード（BUY / SELL）
# ============================================================

def _load_model(side: str):
    global _model_buy, _model_sell

    with _model_lock:
        if side == "SELL":
            if _model_sell is not None:
                return _model_sell
            path = MODEL_PATH_SELL
        else:
            if _model_buy is not None:
                return _model_buy
            path = MODEL_PATH_BUY

        if not path.exists():
            logger.warning(f"[AI] model not found ({side}) -> skip: {path}")
            return None

        try:
            model = lgb.Booster(model_file=str(path))
            logger.info(f"[AI] model loaded ({side}): {path}")
        except Exception:
            logger.error(f"[AI] model load failed ({side})", exc_info=True)
            return None

        if side == "SELL":
            _model_sell = model
        else:
            _model_buy = model

        return model


# ============================================================
# 推論
# ============================================================

def inject_company_ai(symbol: str, side: str = "BUY") -> float:
    """
    company_info AI 確率を返す

    Parameters
    ----------
    symbol : str
    side   : "BUY" | "SELL"

    Returns
    -------
    float : probability（失敗時は 0.0）
    """

    try:
        model = _load_model(side)
        if model is None:
            return 0.0

        df = _load_company_df()
        if df is None:
            return 0.0

        sym = str(symbol)
        if sym not in df.index:
            return 0.0

        X = df.loc[[sym]]  # DataFrame で渡す
        prob = float(model.predict(X)[0])
        return prob

    except Exception:
        logger.error(f"[AI-INJECT-ERROR] {symbol}", exc_info=True)
        return 0.0
