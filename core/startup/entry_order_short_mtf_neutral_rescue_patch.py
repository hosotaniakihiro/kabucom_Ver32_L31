# ============================================================
# File   : core/startup/entry_order_short_mtf_neutral_rescue_patch.py
# Version: V1-SUMMARY-AI-SHORT-MTF-NEUTRAL-RESCUE
# ------------------------------------------------------------
# 目的:
#   SUMMARY_AI が AI_OK / FINAL ENTRY SAFETY GUARD / ENTRY_QTY_FINAL まで通過後、
#   注文作成直前で SHORT_MTF_NOT_BUY_ALIGNED / SHORT_MTF_NOT_SELL_ALIGNED により
#   1mだけ方向一致・3m/5mが0または欠損のケースまで落ちる問題を救済する。
#
# 方針:
#   - 既存 build_entry_order を一度実行する。
#   - SHORT_MTF_NOT_*_ALIGNED でNGの場合のみ判定。
#   - SUMMARY_AI限定。
#   - 1m slope がエントリー方向へ一致。
#   - 3m/5m は逆方向でなく、0/欠損なら中立扱い。
#   - score_buy/score_sell が閾値以上なら、MTF guardだけ一時OFFにして再実行。
#   - 低ボラ、5秒足、板、流動性、数量など他ガードは再実行時も維持。
# ============================================================

from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _first(row: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return None


def _score_for_side(row: dict[str, Any], side: str) -> float:
    side_u = str(side or "").upper()
    if side_u == "BUY":
        return max(_sf(row.get("score_buy")), _sf(row.get("buy_score")), _sf(row.get("score")), _sf(row.get("final_score")), _sf(row.get("display_score")))
    if side_u == "SELL":
        return max(_sf(row.get("score_sell")), _sf(row.get("sell_score")), abs(_sf(row.get("score"))), abs(_sf(row.get("final_score"))), abs(_sf(row.get("display_score"))))
    return 0.0


def _slope_values(row: dict[str, Any]) -> dict[str, float]:
    return {
        "slope_1m": _sf(_first(row, "slope_atr_scaled_1m", "slope_1m", "slope1m", "slope_atr_scaled", "slope", "score_slope")),
        "slope_3m": _sf(_first(row, "slope_atr_scaled_3m", "slope_3m", "slope3m")),
        "slope_5m": _sf(_first(row, "slope_atr_scaled_5m", "slope_5m", "slope5m")),
    }


def _is_short_mtf_reason(reason: Any) -> bool:
    s = str(reason or "").upper()
    return s in {
        "SHORT_MTF_NOT_BUY_ALIGNED",
        "SHORT_MTF_NOT_SELL_ALIGNED",
        "MTF_NOT_BUY_ALIGNED",
        "MTF_NOT_SELL_ALIGNED",
    }


def _can_rescue(row: dict[str, Any], *, symbol: str, side: str, source: str, result: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    if not _env_bool("ENTRY_ORDER_SHORT_MTF_NEUTRAL_RESCUE", True):
        return False, {"reason": "disabled"}
    if str(source or "").upper() != "SUMMARY_AI":
        return False, {"reason": "not_summary_ai", "source": source}

    side_u = str(side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return False, {"reason": "bad_side", "side": side}

    score = _score_for_side(row, side_u)
    min_score = _env_float("ENTRY_ORDER_SHORT_MTF_NEUTRAL_MIN_SCORE", 1.0)
    if score < min_score:
        return False, {"reason": "score_low", "score": score, "min_score": min_score}

    eps = abs(_env_float("ENTRY_ORDER_SHORT_MTF_NEUTRAL_EPS", 0.0))
    slopes = _slope_values(row)
    s1 = slopes["slope_1m"]
    s3 = slopes["slope_3m"]
    s5 = slopes["slope_5m"]

    if side_u == "BUY":
        aligned_1m = s1 > eps
        hard_opposite = {k: v for k, v in slopes.items() if k != "slope_1m" and v < -eps}
    else:
        aligned_1m = s1 < -eps
        hard_opposite = {k: v for k, v in slopes.items() if k != "slope_1m" and v > eps}

    if not aligned_1m:
        return False, {"reason": "one_min_not_aligned", "slopes": slopes, "eps": eps, "score": score}
    if hard_opposite:
        return False, {"reason": "higher_tf_opposite", "hard_opposite": hard_opposite, "slopes": slopes, "eps": eps, "score": score}

    # 3m/5mが0近辺または欠損相当なら中立。ここでは _sf の既定で0になっている。
    return True, {
        "reason": "short_mtf_neutral_rescue",
        "symbol": symbol,
        "side": side_u,
        "score": score,
        "min_score": min_score,
        "slopes": slopes,
        "eps": eps,
        "original_reason": result.get("reason") if isinstance(result, dict) else None,
    }


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        logger.warning("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] already installed")
        return True

    try:
        import trading.handlers.entry_order_builder as eob
        import trading.handlers.entry_controller as ec

        original = getattr(eob, "build_entry_order", None)
        if not callable(original):
            logger.error("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] build_entry_order not callable")
            return False
        if getattr(original, "_short_mtf_neutral_rescue_v1", False):
            _INSTALLED = True
            return True

        _ORIGINAL = original

        @wraps(original)
        def wrapped_build_entry_order(*args: Any, **kwargs: Any):
            result = original(*args, **kwargs)
            try:
                if not isinstance(result, dict) or bool(result.get("ok")):
                    return result
                if not _is_short_mtf_reason(result.get("reason")):
                    return result

                row = kwargs.get("entry_row") or {}
                if not isinstance(row, dict):
                    return result
                symbol = str(kwargs.get("symbol") or row.get("symbol") or "")
                side = str(kwargs.get("side") or row.get("side") or row.get("entry_decision") or "")
                source = str(kwargs.get("source") or row.get("source") or row.get("entry_type") or "")

                ok, detail = _can_rescue(row, symbol=symbol, side=side, source=source, result=result)
                if not ok:
                    logger.info("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] no_rescue detail=%s result=%s", detail, result)
                    return result

                old_enabled = getattr(eob, "ENTRY_ORDER_MTF_GUARD_ENABLED", True)
                try:
                    eob.ENTRY_ORDER_MTF_GUARD_ENABLED = False
                    retry = original(*args, **kwargs)
                finally:
                    eob.ENTRY_ORDER_MTF_GUARD_ENABLED = old_enabled

                if isinstance(retry, dict) and bool(retry.get("ok")):
                    retry = dict(retry)
                    d = dict(retry.get("detail") or {})
                    d["short_mtf_neutral_rescue"] = True
                    d["short_mtf_neutral_rescue_detail"] = detail
                    retry["detail"] = d
                    logger.warning("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] rescued detail=%s retry=%s", detail, retry)
                    return retry

                logger.warning("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] retry_still_ng detail=%s retry=%s", detail, retry)
                return retry
            except Exception:
                logger.exception("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] wrapper failed; return original result")
                return result

        wrapped_build_entry_order._short_mtf_neutral_rescue_v1 = True  # type: ignore[attr-defined]
        wrapped_build_entry_order._original = original  # type: ignore[attr-defined]
        eob.build_entry_order = wrapped_build_entry_order
        try:
            ec.build_entry_order = wrapped_build_entry_order
        except Exception:
            pass

        _INSTALLED = True
        logger.warning(
            "[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] installed v1 enabled=%s min_score=%.3f eps=%.6f",
            _env_bool("ENTRY_ORDER_SHORT_MTF_NEUTRAL_RESCUE", True),
            _env_float("ENTRY_ORDER_SHORT_MTF_NEUTRAL_MIN_SCORE", 1.0),
            _env_float("ENTRY_ORDER_SHORT_MTF_NEUTRAL_EPS", 0.0),
        )
        return True
    except Exception:
        logger.exception("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] auto install failed")


__all__ = ["install"]
