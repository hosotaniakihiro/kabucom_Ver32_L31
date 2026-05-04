# ============================================================
# pj/trading/entry/ignition/ai_boost.py
# 殿様イナゴ AI 補助推論（方式選択・オーケストレーター）
# Ver26.1-FINAL-ORCHESTRATOR
# ------------------------------------------------------------
# ✔ 発注は絶対にしない
# ✔ ENTRY 可否は「物理ゲート + 方式選択」まで
# ✔ 最終 ENTRY 判断は entry_controller が行う
# ✔ 旧 LGBM は参考値（学習・分析用途）のみ
# ✔ 将来 AI 追加を前提とした安全設計
# ============================================================

from pathlib import Path
import logging
import joblib

from global_state import global_data

# ★ 方式選択AI（最重要）
from AI.ai_mode_selector import decide_entry_mode

logger = logging.getLogger(__name__)

# ============================================================
# 特徴量定義（学習・分析互換用）
# ============================================================

FEATURES = [
    "volume_speed",   # ランキング出来高速度
    "fast_ret",       # 初動リターン [%]
    "rank_position",  # ランキング順位
    "price",          # 現在価格
    "spread",         # 板スプレッド
    "entry_second",   # ENTRY 秒
]

# ============================================================
# 旧 LightGBM（参考値専用 / analysis only）
# ============================================================

BASE_DIR = Path(__file__).resolve().parents[3]
MODEL_PATH = BASE_DIR / "AI" / "model" / "tonosama_entry_lgbm.pkl"

_legacy_model = None


def _load_legacy_model():
    """旧 LightGBM を参考値用途としてのみロード"""
    global _legacy_model
    if _legacy_model is not None:
        return _legacy_model

    if not MODEL_PATH.exists():
        return None

    try:
        _legacy_model = joblib.load(MODEL_PATH)
        logger.info("[ai_boost] legacy LGBM loaded (analysis only)")
        return _legacy_model
    except Exception:
        logger.exception("[ai_boost] legacy LGBM load failed")
        _legacy_model = None
        return None


# ============================================================
# 旧互換：上昇確率（summary / exit 用）
# ============================================================

def ai_predict_up(symbol: str) -> float:
    """
    AI による上昇確率（0.0 ～ 1.0）
    値が無ければ 0.5（中立）
    """
    try:
        prob = getattr(global_data, "ai_up_prob", {}).get(symbol)
        return float(prob) if prob is not None else 0.5
    except Exception:
        return 0.5


# ============================================================
# 殿様イナゴ ENTRY 前 AI 補助推論（方式選択）
# ============================================================

def infer_tonosama_entry(
    symbol: str,
    fast_ret: float,
    volume_speed: float,
    entry_second: int = 0,
):
    """
    殿様イナゴ ENTRY 前の AI 補助判定（方式選択まで）

    Returns
    -------
    dict
        {
            "ok": bool,
            "entry_mode": "BREAKOUT" | "PULLBACK" | "SKIP",
            "reason": str,
            "features": dict,
            "ai_confidence": float | None
        }
    """

    symbol = str(symbol)

    # --------------------------------------------------------
    # 特徴量収集
    # --------------------------------------------------------
    price = float(
        getattr(global_data, "latest_price", {}).get(symbol, 0.0) or 0.0
    )
    spread = float(
        getattr(global_data, "latest_spread", {}).get(symbol, 0.0) or 0.0
    )
    rank_pos = int(
        getattr(global_data, "latest_rank_position", {}).get(symbol, 999) or 999
    )

    features = {
        "volume_speed": float(volume_speed),
        "fast_ret": float(fast_ret),
        "rank_position": rank_pos,
        "price": price,
        "spread": spread,
        "entry_second": int(entry_second) if entry_second is not None else 0,
    }

    # --------------------------------------------------------
    # ① 物理ゲート（AI以前の安全装置）
    # --------------------------------------------------------
    if features["volume_speed"] < 5_000:
        return {
            "ok": False,
            "entry_mode": "SKIP",
            "reason": "PHYSICAL_LOW_VOLUME",
            "features": features,
            "ai_confidence": None,
        }

    if features["fast_ret"] < 0.15:
        return {
            "ok": False,
            "entry_mode": "SKIP",
            "reason": "PHYSICAL_WEAK_FAST_RET",
            "features": features,
            "ai_confidence": None,
        }

    # --------------------------------------------------------
    # ② 方式選択AI（最重要）
    # --------------------------------------------------------
    try:
        entry_mode = decide_entry_mode(symbol)
    except Exception:
        logger.exception("[ai_boost] mode selector failed")
        entry_mode = "SKIP"

    if entry_mode == "SKIP":
        return {
            "ok": False,
            "entry_mode": "SKIP",
            "reason": "AI_MODE_SKIP",
            "features": features,
            "ai_confidence": None,
        }

    # --------------------------------------------------------
    # entry_mode を features に正式追加
    # --------------------------------------------------------
    features["entry_mode"] = entry_mode
    features["entry_mode_id"] = 1 if entry_mode == "BREAKOUT" else 0

    # --------------------------------------------------------
    # ③ 旧 LightGBM（参考値・学習ログ用）
    # --------------------------------------------------------
    ai_confidence = None
    legacy = _load_legacy_model()

    if legacy:
        try:
            X = [[features[k] for k in FEATURES]]
            ai_confidence = float(legacy.predict_proba(X)[0][1])
        except Exception:
            ai_confidence = None

    # --------------------------------------------------------
    # 方式選択まで OK（ENTRY 可否は決めない）
    # --------------------------------------------------------
    return {
        "ok": True,
        "entry_mode": entry_mode,
        "reason": "MODE_SELECTED",
        "features": features,
        "ai_confidence": ai_confidence,  # analysis only
    }
