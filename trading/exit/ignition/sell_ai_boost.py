# ============================================================
# pj/trading/exit/ignition/sell_ai_boost.py
# SELL 方式選択 AI（オーケストレーター）
# ------------------------------------------------------------
# ✔ 発注は絶対にしない
# ✔ SELL 可否は決めない（方式選択のみ）
# ✔ controller が最終判断
# ✔ ENTRY の ai_boost と完全対称
# ============================================================

import logging

logger = logging.getLogger(__name__)

# ============================================================
# SELL 方式定義
# ============================================================
SELL_MODES = ("TAKE_PROFIT", "STOP", "TRAIL", "SKIP")


# ============================================================
# メインAPI：SELL方式選択
# ============================================================
def infer_sell_mode(features: dict) -> dict:
    """
    SELL 方式選択（最終判断はしない）

    Args:
        features (dict):
            {
                "profit_rate": float,     # 含み益率（%）
                "drawdown_rate": float,   # 含み損率（%）
                "hold_seconds": int,      # 保持秒数
                "volume_speed": float,    # 出来高速度
                "volatility": float,      # ボラティリティ
                "trend_strength": float,  # トレンド強度
            }

    Returns:
        dict:
            {
                "ok": bool,                 # 方式選択まで通過したか
                "sell_mode": str,           # TAKE_PROFIT / STOP / TRAIL / SKIP
                "reason": str,
                "features": dict,
                "ai_confidence": float | None
            }
    """

    # -------------------------------
    # 安全な取得
    # -------------------------------
    profit = float(features.get("profit_rate", 0.0))
    drawdown = float(features.get("drawdown_rate", 0.0))
    hold_sec = int(features.get("hold_seconds", 0))
    vol_speed = float(features.get("volume_speed", 0.0))
    vol = float(features.get("volatility", 0.0))
    trend = float(features.get("trend_strength", 0.0))

    # ========================================================
    # ① 物理・即時ガード（方式選択以前）
    # ========================================================
    # 板・出来高が薄すぎる → 何もしない
    if vol_speed <= 0:
        return _skip("NO_VOLUME", features)

    # 保持が極端に短い → 即SELLしない
    if hold_sec < 5:
        return _skip("TOO_EARLY", features)

    # ========================================================
    # ② STOP（損切り優先）
    # ========================================================
    # 一定以上の含み損は最優先で STOP
    if drawdown <= -0.30:
        return _ok("STOP", "HARD_STOP", features)

    # ========================================================
    # ③ TAKE_PROFIT（利確）
    # ========================================================
    # 十分な含み益 + 伸び鈍化
    if profit >= 0.50 and trend < 0.2:
        return _ok("TAKE_PROFIT", "PROFIT_WEAK_TREND", features)

    # ========================================================
    # ④ TRAIL（伸ばす）
    # ========================================================
    # 含み益があり、トレンドが強い → トレール
    if profit > 0 and trend >= 0.4:
        return _ok("TRAIL", "STRONG_TREND", features)

    # ========================================================
    # ⑤ デフォルト：見送り
    # ========================================================
    return _skip("NO_CLEAR_SIGNAL", features)


# ============================================================
# 内部ユーティリティ
# ============================================================
def _ok(mode: str, reason: str, features: dict) -> dict:
    logger.debug(f"[SELL_MODE] {mode} reason={reason}")
    return {
        "ok": True,
        "sell_mode": mode,
        "reason": reason,
        "features": features,
        "ai_confidence": None,  # 方式選択では数値化しない
    }


def _skip(reason: str, features: dict) -> dict:
    logger.debug(f"[SELL_MODE] SKIP reason={reason}")
    return {
        "ok": False,
        "sell_mode": "SKIP",
        "reason": reason,
        "features": features,
        "ai_confidence": None,
    }
