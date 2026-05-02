# ============================================================
# File   : trading/exit/ai_exit_gate.py
# Version: Ver28.0-AI-ACTIVE-EXIT-GATE-DAILY-CACHE
# ------------------------------------------------------------
# 【概要】
#   AI主導EXIT判定。
#
# 【REV28.0 変更点】
#   ✔ daily_signal_cache 由来の日足情報をEXIT判定に利用
#   ✔ daily_exit_warn=True の場合、EXIT寄りに補正
#   ✔ daily_sell_score / daily_score を weakness に反映
#   ✔ モデルありの場合も daily特徴量は features に含まれる
#   ✔ モデルなしヒューリスティックでも日足悪化を考慮
#
# 【重要】
#   - このファイルではDBを直接読まない
#   - 日足情報は exit_loop.py 側で features に注入される
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_exit_model = None


# ============================================================
# env
# ============================================================

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


AI_EXIT_ENABLED = _env_bool("AI_EXIT_ENABLED", True)
AI_EXIT_ACTIVE_MODE = _env_bool("AI_EXIT_ACTIVE_MODE", True)
AI_EXIT_MIN_CONFIDENCE = _env_float("AI_EXIT_MIN_CONFIDENCE", 0.70)

AI_EXIT_TAKE_PROFIT_PCT = _env_float("AI_EXIT_TAKE_PROFIT_PCT", 1.2)
AI_EXIT_STOP_LOSS_PCT = _env_float("AI_EXIT_STOP_LOSS_PCT", -0.8)

# 日足EXIT補助
AI_EXIT_DAILY_ENABLED = _env_bool("AI_EXIT_DAILY_ENABLED", True)

# daily_exit_warn=True のとき、どの程度の確信度でEXIT候補にするか
AI_EXIT_DAILY_WARN_CONFIDENCE = _env_float("AI_EXIT_DAILY_WARN_CONFIDENCE", 0.86)

# 含み益がこの値以上なら、日足悪化で利確/撤退しやすくする
AI_EXIT_DAILY_WARN_MIN_PROFIT_PCT = _env_float("AI_EXIT_DAILY_WARN_MIN_PROFIT_PCT", -0.20)

# daily_sell_score がこの値以上なら日足売り優勢と見る
AI_EXIT_DAILY_SELL_SCORE_THRESHOLD = _env_float("AI_EXIT_DAILY_SELL_SCORE_THRESHOLD", 4.0)

# daily_score がこの値以下なら日足弱化と見る
AI_EXIT_DAILY_SCORE_WEAK_THRESHOLD = _env_float("AI_EXIT_DAILY_SCORE_WEAK_THRESHOLD", -1.5)


# ============================================================
# model
# ============================================================

def set_exit_model(model) -> None:
    global _exit_model
    _exit_model = model


# ============================================================
# helpers
# ============================================================

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _safe_bool(x: Any, default: bool = False) -> bool:
    try:
        if x is None:
            return bool(default)
        if isinstance(x, bool):
            return x
        s = str(x).strip().lower()
        if s in {"1", "true", "yes", "on", "y", "ok"}:
            return True
        if s in {"0", "false", "no", "off", "n", "ng", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _safe_str(x: Any, default: str = "") -> str:
    try:
        if x is None:
            return default
        return str(x)
    except Exception:
        return default


def _build_feature_df(features: Dict[str, Any]) -> pd.DataFrame:
    safe = {str(k): _safe_float(v) for k, v in dict(features or {}).items()}
    df = pd.DataFrame([safe])
    df = df.replace([np.inf, -np.inf], 0.0).fillna(0.0)
    return df


def _normalize_decision(
    *,
    allow_exit: bool,
    confidence: float,
    reason: str,
    exit_type: str = "AI_EXIT",
    model_used: str = "HEURISTIC",
) -> Dict[str, Any]:
    confidence = max(0.0, min(1.0, _safe_float(confidence)))
    return {
        "allow_exit": bool(allow_exit),
        "confidence": confidence,
        "reason": str(reason or ""),
        "exit_type": str(exit_type or "AI_EXIT"),
        "model_used": str(model_used or "UNKNOWN"),
    }


def _build_daily_reason(
    *,
    daily_score: float,
    daily_buy_score: float,
    daily_sell_score: float,
    daily_exit_warn: bool,
    daily_ok_sell: bool,
    daily_reason: str,
) -> str:
    parts = [
        f"daily_score={daily_score:.2f}",
        f"daily_buy={daily_buy_score:.2f}",
        f"daily_sell={daily_sell_score:.2f}",
        f"daily_exit_warn={daily_exit_warn}",
        f"daily_ok_sell={daily_ok_sell}",
    ]

    if daily_reason:
        parts.append(f"daily_reason={daily_reason}")

    return " ".join(parts)


# ============================================================
# main
# ============================================================

def ai_exit_decision(
    *,
    symbol: str,
    side: str,
    pnl: float,
    features: Optional[Dict[str, Any]] = None,
    price: float = 0.0,
    entry_price: float = 0.0,
    holding_seconds: float = 0.0,
    rule_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    AI主導EXIT判定。

    allow_exit=True  → EXIT実行候補
    allow_exit=False → HOLD
    """

    if not AI_EXIT_ENABLED:
        return _normalize_decision(
            allow_exit=False,
            confidence=0.0,
            reason="AI_EXIT_DISABLED",
            exit_type="HOLD",
            model_used="DISABLED",
        )

    try:
        symbol = _safe_str(symbol)
        side = _safe_str(side).upper()
        features = dict(features or {})

        price = _safe_float(price)
        entry_price = _safe_float(entry_price)
        pnl = _safe_float(pnl)
        holding_seconds = _safe_float(holding_seconds)

        if entry_price > 0 and price > 0:
            if side == "BUY":
                profit_pct = (price - entry_price) / entry_price * 100.0
            else:
                profit_pct = (entry_price - price) / entry_price * 100.0
        else:
            profit_pct = _safe_float(features.get("profit_pct"))

        score_buy = _safe_float(features.get("score_buy"))
        score_sell = _safe_float(features.get("score_sell"))
        rsi = _safe_float(features.get("rsi"))
        macd = _safe_float(features.get("macd"))
        signal = _safe_float(features.get("signal"))
        slope = _safe_float(features.get("slope"))
        mtf = _safe_float(features.get("mtf"))
        collapse_prob = _safe_float(features.get("collapse_prob"))

        macd_hist = macd - signal

        # ----------------------------------------------------
        # daily cache features
        # ----------------------------------------------------
        daily_score = _safe_float(features.get("daily_score"))
        daily_buy_score = _safe_float(features.get("daily_buy_score"))
        daily_sell_score = _safe_float(features.get("daily_sell_score"))
        daily_exit_warn = _safe_bool(features.get("daily_exit_warn"))
        daily_ok_sell = _safe_bool(features.get("daily_ok_sell"))
        daily_reason = _safe_str(features.get("daily_reason"))

        daily_reason_text = _build_daily_reason(
            daily_score=daily_score,
            daily_buy_score=daily_buy_score,
            daily_sell_score=daily_sell_score,
            daily_exit_warn=daily_exit_warn,
            daily_ok_sell=daily_ok_sell,
            daily_reason=daily_reason,
        )

        # ----------------------------------------------------
        # 1. モデルがある場合
        # ----------------------------------------------------
        if _exit_model is not None:
            df = _build_feature_df(features)
            proba = _safe_float(_exit_model.predict_proba(df)[0][1])

            # モデル確率に日足悪化を軽く上乗せ
            if AI_EXIT_DAILY_ENABLED:
                daily_boost = 0.0

                if daily_exit_warn:
                    daily_boost += 0.08

                if daily_sell_score >= AI_EXIT_DAILY_SELL_SCORE_THRESHOLD:
                    daily_boost += 0.05

                if daily_score <= AI_EXIT_DAILY_SCORE_WEAK_THRESHOLD:
                    daily_boost += 0.05

                proba = min(1.0, proba + daily_boost)

            allow = proba >= AI_EXIT_MIN_CONFIDENCE

            logger.info(
                "[AI_EXIT GATE] model symbol=%s side=%s pnl=%.4f profit_pct=%.3f "
                "proba=%.3f allow=%s daily_score=%.2f daily_sell=%.2f daily_warn=%s",
                symbol,
                side,
                pnl,
                profit_pct,
                proba,
                allow,
                daily_score,
                daily_sell_score,
                daily_exit_warn,
            )

            return _normalize_decision(
                allow_exit=allow,
                confidence=proba,
                reason=f"model_exit_proba={proba:.3f} {daily_reason_text}",
                exit_type="AI_MODEL_EXIT",
                model_used="MODEL_DAILY",
            )

        # ----------------------------------------------------
        # 2. モデルなしの安全ヒューリスティック
        # ----------------------------------------------------

        # 2-1. 強制損切り
        if profit_pct <= AI_EXIT_STOP_LOSS_PCT:
            return _normalize_decision(
                allow_exit=True,
                confidence=0.95,
                reason=f"AI_STOP_LOSS profit_pct={profit_pct:.2f} {daily_reason_text}",
                exit_type="AI_STOP_LOSS",
            )

        # 2-2. 急落・崩壊検知
        if collapse_prob >= 0.80:
            return _normalize_decision(
                allow_exit=True,
                confidence=0.92,
                reason=f"AI_COLLAPSE_EXIT collapse={collapse_prob:.2f} {daily_reason_text}",
                exit_type="AI_COLLAPSE_EXIT",
            )

        weakness = 0

        if side == "BUY":
            if score_sell > score_buy:
                weakness += 1
            if slope < 0:
                weakness += 1
            if mtf < 0:
                weakness += 1
            if macd_hist < 0:
                weakness += 1
            if rsi >= 72:
                weakness += 1

            # 日足悪化を weakness に加点
            if AI_EXIT_DAILY_ENABLED:
                if daily_exit_warn:
                    weakness += 2
                if daily_ok_sell:
                    weakness += 1
                if daily_sell_score >= AI_EXIT_DAILY_SELL_SCORE_THRESHOLD:
                    weakness += 1
                if daily_score <= AI_EXIT_DAILY_SCORE_WEAK_THRESHOLD:
                    weakness += 1

        else:
            if score_buy > score_sell:
                weakness += 1
            if slope > 0:
                weakness += 1
            if mtf > 0:
                weakness += 1
            if macd_hist > 0:
                weakness += 1
            if rsi <= 28:
                weakness += 1

            # 売りポジションの場合は日足買い優勢なら撤退寄り
            if AI_EXIT_DAILY_ENABLED:
                if daily_score >= abs(AI_EXIT_DAILY_SCORE_WEAK_THRESHOLD):
                    weakness += 1
                if daily_buy_score > daily_sell_score:
                    weakness += 1

        # ----------------------------------------------------
        # 2-3. 日足EXIT警戒による撤退
        # ----------------------------------------------------
        if AI_EXIT_DAILY_ENABLED and side == "BUY":
            if daily_exit_warn and profit_pct >= AI_EXIT_DAILY_WARN_MIN_PROFIT_PCT:
                conf = max(
                    AI_EXIT_DAILY_WARN_CONFIDENCE,
                    min(0.96, 0.72 + weakness * 0.05),
                )

                return _normalize_decision(
                    allow_exit=True,
                    confidence=conf,
                    reason=(
                        f"AI_DAILY_EXIT_WARN profit_pct={profit_pct:.2f} "
                        f"weakness={weakness} {daily_reason_text}"
                    ),
                    exit_type="AI_DAILY_EXIT_WARN",
                    model_used="HEURISTIC_DAILY",
                )

            if daily_sell_score >= AI_EXIT_DAILY_SELL_SCORE_THRESHOLD and weakness >= 3:
                conf = min(0.95, 0.74 + weakness * 0.04)

                return _normalize_decision(
                    allow_exit=True,
                    confidence=conf,
                    reason=(
                        f"AI_DAILY_SELL_SCORE_EXIT profit_pct={profit_pct:.2f} "
                        f"weakness={weakness} {daily_reason_text}"
                    ),
                    exit_type="AI_DAILY_SELL_SCORE_EXIT",
                    model_used="HEURISTIC_DAILY",
                )

        # ----------------------------------------------------
        # 2-4. 利確
        # ----------------------------------------------------
        if profit_pct >= AI_EXIT_TAKE_PROFIT_PCT and weakness >= 2:
            conf = min(0.95, 0.70 + weakness * 0.06)
            return _normalize_decision(
                allow_exit=True,
                confidence=conf,
                reason=(
                    f"AI_TAKE_PROFIT profit_pct={profit_pct:.2f} "
                    f"weakness={weakness} sell={score_sell:.2f} buy={score_buy:.2f} "
                    f"slope={slope:.3f} mtf={mtf:.3f} rsi={rsi:.1f} "
                    f"{daily_reason_text}"
                ),
                exit_type="AI_TAKE_PROFIT",
            )

        # ----------------------------------------------------
        # 2-5. トレンド崩壊
        # ----------------------------------------------------
        if profit_pct > 0 and weakness >= 4:
            return _normalize_decision(
                allow_exit=True,
                confidence=0.84,
                reason=(
                    f"AI_TREND_BREAK profit_pct={profit_pct:.2f} weakness={weakness} "
                    f"slope={slope:.3f} mtf={mtf:.3f} {daily_reason_text}"
                ),
                exit_type="AI_TREND_BREAK",
            )

        # ----------------------------------------------------
        # 2-6. 既存ルールをAIが確認
        # ----------------------------------------------------
        if rule_reason:
            return _normalize_decision(
                allow_exit=True,
                confidence=0.75,
                reason=f"RULE_EXIT_CONFIRMED_BY_AI rule={rule_reason} {daily_reason_text}",
                exit_type="AI_RULE_CONFIRM",
            )

        # ----------------------------------------------------
        # 2-7. HOLD
        # ----------------------------------------------------
        return _normalize_decision(
            allow_exit=False,
            confidence=0.60,
            reason=(
                f"AI_HOLD profit_pct={profit_pct:.2f} weakness={weakness} "
                f"buy={score_buy:.2f} sell={score_sell:.2f} {daily_reason_text}"
            ),
            exit_type="HOLD",
        )

    except Exception:
        logger.exception("[AI_EXIT_DECISION] exception → HOLD")
        return _normalize_decision(
            allow_exit=False,
            confidence=0.0,
            reason="AI_EXIT_EXCEPTION",
            exit_type="HOLD",
            model_used="ERROR",
        )


def ai_exit_check(
    symbol: str,
    side: str,
    pnl: float,
    features: Dict[str, Any] | None = None,
) -> bool:
    """
    旧互換。
    True  → EXIT許可
    False → EXIT抑制
    """
    d = ai_exit_decision(
        symbol=symbol,
        side=side,
        pnl=pnl,
        features=features,
    )
    return bool(d.get("allow_exit"))


__all__ = [
    "set_exit_model",
    "ai_exit_decision",
    "ai_exit_check",
]