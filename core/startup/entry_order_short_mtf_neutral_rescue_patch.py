# ============================================================
# File   : core/startup/entry_order_short_mtf_neutral_rescue_patch.py
# Version: V5-ALL-ZERO-NEUTRAL-PASS
# ------------------------------------------------------------
# 目的:
#   SUMMARY_AI が AI_OK / FINAL ENTRY SAFETY GUARD / ENTRY_QTY_FINAL まで通過後、
#   注文作成直前で SHORT_MTF_NOT_BUY_ALIGNED / SHORT_MTF_NOT_SELL_ALIGNED により
#   1m/3m/5m がすべて0.0の中立ケースまで落ちる問題を救済する。
#
# V5:
#   - slope_1m/slope_3m/slope_5m がすべて0.0近傍の場合は、
#     反対方向ではなく未確定・中立として通す。
#   - 従来の「1mだけ方向一致、3m/5m neutral」救済も維持。
#   - 板、信用、数量、低ボラ、API発注ガードは維持。
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


def _norm(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _score_for_side(row: dict[str, Any], side: str) -> float:
    side_u = _norm(side)
    if side_u == "BUY":
        return max(
            _sf(row.get("score_buy")),
            _sf(row.get("buy_score")),
            _sf(row.get("score")),
            _sf(row.get("final_score")),
            _sf(row.get("display_score")),
        )
    if side_u == "SELL":
        return max(
            _sf(row.get("score_sell")),
            _sf(row.get("sell_score")),
            abs(_sf(row.get("score"))),
            abs(_sf(row.get("final_score"))),
            abs(_sf(row.get("display_score"))),
        )
    return 0.0


def _slope_values(row: dict[str, Any]) -> dict[str, float]:
    return {
        "slope_1m": _sf(_first(row, "slope_atr_scaled_1m", "slope_1m", "slope1m", "slope_atr_scaled", "slope", "score_slope")),
        "slope_3m": _sf(_first(row, "slope_atr_scaled_3m", "slope_3m", "slope3m")),
        "slope_5m": _sf(_first(row, "slope_atr_scaled_5m", "slope_5m", "slope5m")),
    }


def _is_all_zero_slopes(slopes: dict[str, float]) -> bool:
    eps = abs(_env_float("ENTRY_ORDER_SHORT_MTF_ZERO_EPS", 0.0000001))
    return all(abs(float(v or 0.0)) <= eps for v in slopes.values())


def _is_short_mtf_reason(reason: Any) -> bool:
    s = _norm(reason)
    return s in {
        "SHORT_MTF_NOT_BUY_ALIGNED",
        "SHORT_MTF_NOT_SELL_ALIGNED",
        "MTF_NOT_BUY_ALIGNED",
        "MTF_NOT_SELL_ALIGNED",
    }


def _is_summary_ai_like(row: dict[str, Any], source: str) -> bool:
    src = _norm(source or row.get("source"))
    et = _norm(row.get("entry_type"))
    return src in {"SUMMARY_AI", "SUMMARY"} or et == "SUMMARY_AI"


def _can_rescue(row: dict[str, Any], *, symbol: str, side: str, source: str, result: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    if not _env_bool("ENTRY_ORDER_SHORT_MTF_NEUTRAL_RESCUE", True):
        return False, {"reason": "disabled"}
    if not isinstance(row, dict):
        return False, {"reason": "row_not_dict"}
    if not _is_summary_ai_like(row, source):
        return False, {"reason": "not_summary_ai_like", "source": source, "entry_type": row.get("entry_type")}

    side_u = _norm(side or row.get("side") or row.get("entry_decision"))
    if side_u not in {"BUY", "SELL"}:
        return False, {"reason": "bad_side", "side": side}

    score = _score_for_side(row, side_u)
    min_score = _env_float("ENTRY_ORDER_SHORT_MTF_NEUTRAL_MIN_SCORE", 1.0)
    if score < min_score:
        return False, {"reason": "score_low", "score": score, "min_score": min_score}

    eps = abs(_env_float("ENTRY_ORDER_SHORT_MTF_NEUTRAL_EPS", 0.0))
    slopes = _slope_values(row)

    # すべて0.0は反対方向ではなく、未確定・中立として通す。
    if _env_bool("ENTRY_ORDER_ALLOW_ALL_ZERO_SHORT_MTF", True) and _is_all_zero_slopes(slopes):
        return True, {
            "reason": "all_zero_short_mtf_neutral_rescue",
            "symbol": symbol or row.get("symbol"),
            "side": side_u,
            "score": score,
            "min_score": min_score,
            "slopes": slopes,
            "eps": eps,
            "source": source or row.get("source"),
            "entry_type": row.get("entry_type"),
            "original_reason": result.get("reason") if isinstance(result, dict) else None,
        }

    s1 = slopes["slope_1m"]
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

    return True, {
        "reason": "short_mtf_neutral_rescue",
        "symbol": symbol or row.get("symbol"),
        "side": side_u,
        "score": score,
        "min_score": min_score,
        "slopes": slopes,
        "eps": eps,
        "source": source or row.get("source"),
        "entry_type": row.get("entry_type"),
        "original_reason": result.get("reason") if isinstance(result, dict) else None,
    }


def _row_from_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    row = kwargs.get("entry_row") or kwargs.get("row")
    if isinstance(row, dict):
        return row
    for a in args:
        if isinstance(a, dict):
            return a
    return {}


def _context_from_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[dict[str, Any], str, str, str]:
    row = _row_from_args(args, kwargs)
    symbol = str(kwargs.get("symbol") or row.get("symbol") or "")
    side = str(kwargs.get("side") or row.get("side") or row.get("entry_decision") or "")
    source = str(kwargs.get("source") or row.get("source") or row.get("entry_type") or "")
    return row, symbol, side, source


class _TempShortMtfBypass:
    def __init__(self, eob: Any):
        self.eob = eob
        self.attrs: dict[tuple[Any, str], Any] = {}
        self.env: dict[str, str | None] = {}

    @staticmethod
    def _noop(*_args: Any, **_kwargs: Any):
        return None

    def __enter__(self):
        for obj, name, value in [
            (self.eob, "ENTRY_ORDER_MTF_GUARD_ENABLED", False),
            (self.eob, "ENTRY_ORDER_REQUIRE_MTF_DATA", False),
        ]:
            try:
                self.attrs[(obj, name)] = getattr(obj, name)
                setattr(obj, name, value)
            except Exception:
                pass
        for obj_name in ("core.startup.entry_limit_passive_runtime_patch",):
            try:
                mod = __import__(obj_name, fromlist=["dummy"])
                old_strict = getattr(mod, "_summary_ai_strict_guard", None)
                if callable(old_strict):
                    self.attrs[(mod, "_summary_ai_strict_guard")] = old_strict
                    setattr(mod, "_summary_ai_strict_guard", self._noop)
            except Exception:
                pass
        for name, value in {
            "ENTRY_ORDER_MTF_GUARD_ENABLED": "0",
            "ENTRY_ORDER_REQUIRE_MTF_DATA": "0",
            "ENTRY_SHORT_MTF_REQUIRED": "0",
            "ENTRY_SHORT_MTF_FORCE_2OF3": "0",
            "ENTRY_SHORT_MTF_REQUIRE_ALL": "0",
            "ENTRY_SHORT_MTF_MIN_ALIGNED": "1",
            "ENTRY_SHORT_MTF_MIN_AVAILABLE": "1",
        }.items():
            self.env[name] = os.environ.get(name)
            os.environ[name] = value
        return self

    def __exit__(self, exc_type, exc, tb):
        for (obj, name), value in self.attrs.items():
            try:
                setattr(obj, name, value)
            except Exception:
                pass
        for name, value in self.env.items():
            try:
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            except Exception:
                pass
        return False


def _patch_guard_function(obj: Any, name: str) -> bool:
    old = getattr(obj, name, None)
    if not callable(old):
        return False
    if getattr(old, "_short_mtf_neutral_guard_v5", False):
        return True
    original = getattr(old, "_original", old)

    @wraps(original)
    def guard_wrapper(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        try:
            if result is None:
                return None
            if isinstance(result, dict) and not _is_short_mtf_reason(result.get("reason")):
                return result
            row, symbol, side, source = _context_from_args(args, kwargs)
            ok, detail = _can_rescue(row, symbol=symbol, side=side, source=source, result=result if isinstance(result, dict) else None)
            if ok:
                logger.warning("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] guard bypass v5 target=%s.%s detail=%s original=%s", getattr(obj, "__name__", obj.__class__.__name__), name, detail, result)
                return None
        except Exception:
            logger.exception("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] guard wrapper failed target=%s.%s", obj, name)
        return result

    guard_wrapper._short_mtf_neutral_guard_v5 = True  # type: ignore[attr-defined]
    guard_wrapper._short_mtf_neutral_guard_v4 = True  # type: ignore[attr-defined]
    guard_wrapper._original = original  # type: ignore[attr-defined]
    setattr(obj, name, guard_wrapper)
    return True


def _patch_direct_guards(eob: Any) -> list[str]:
    patched: list[str] = []
    try:
        if _patch_guard_function(eob, "_summary_mtf_direction_guard"):
            patched.append("entry_order_builder._summary_mtf_direction_guard")
    except Exception:
        logger.exception("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] patch eob guard failed")
    try:
        import core.startup.entry_limit_passive_runtime_patch as elp
        if _patch_guard_function(elp, "_summary_ai_strict_guard"):
            patched.append("entry_limit_passive_runtime_patch._summary_ai_strict_guard")
    except Exception:
        logger.debug("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] entry_limit passive guard patch skipped", exc_info=True)
    return patched


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        try:
            import trading.handlers.entry_order_builder as eob
            _patch_direct_guards(eob)
        except Exception:
            pass
        logger.warning("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] already installed v5")
        return True

    try:
        import trading.handlers.entry_order_builder as eob
        import trading.handlers.entry_controller as ec

        original = getattr(eob, "build_entry_order", None)
        if not callable(original):
            logger.error("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] build_entry_order not callable")
            return False
        if getattr(original, "_short_mtf_neutral_rescue_v5", False):
            _INSTALLED = True
            return True

        _ORIGINAL = getattr(original, "_original", original)
        patched_guards = _patch_direct_guards(eob)

        @wraps(_ORIGINAL)
        def wrapped_build_entry_order(*args: Any, **kwargs: Any):
            result = _ORIGINAL(*args, **kwargs)
            try:
                if not isinstance(result, dict) or bool(result.get("ok")):
                    return result
                if not _is_short_mtf_reason(result.get("reason")):
                    return result

                row, symbol, side, source = _context_from_args(args, kwargs)
                if not row:
                    return result

                ok, detail = _can_rescue(row, symbol=symbol, side=side, source=source, result=result)
                if not ok:
                    logger.info("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] no_rescue detail=%s result=%s", detail, result)
                    return result

                with _TempShortMtfBypass(eob):
                    retry = _ORIGINAL(*args, **kwargs)

                if isinstance(retry, dict) and bool(retry.get("ok")):
                    retry = dict(retry)
                    d = dict(retry.get("detail") or {})
                    d["short_mtf_neutral_rescue"] = True
                    d["short_mtf_neutral_rescue_detail"] = detail
                    retry["detail"] = d
                    logger.warning("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] rescued v5 detail=%s retry=%s", detail, retry)
                    return retry

                logger.warning("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] retry_still_ng_v5 detail=%s retry=%s", detail, retry)
                return retry
            except Exception:
                logger.exception("[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] wrapper failed; return original result")
                return result

        wrapped_build_entry_order._short_mtf_neutral_rescue_v5 = True  # type: ignore[attr-defined]
        wrapped_build_entry_order._short_mtf_neutral_rescue_v4 = True  # type: ignore[attr-defined]
        wrapped_build_entry_order._short_mtf_neutral_rescue_v3 = True  # type: ignore[attr-defined]
        wrapped_build_entry_order._short_mtf_neutral_rescue_v2 = True  # type: ignore[attr-defined]
        wrapped_build_entry_order._short_mtf_neutral_rescue_v1 = True  # type: ignore[attr-defined]
        wrapped_build_entry_order._original = _ORIGINAL  # type: ignore[attr-defined]
        eob.build_entry_order = wrapped_build_entry_order
        try:
            ec.build_entry_order = wrapped_build_entry_order
        except Exception:
            pass

        _INSTALLED = True
        logger.warning(
            "[ENTRY ORDER SHORT MTF NEUTRAL RESCUE] installed v5 enabled=%s min_score=%.3f eps=%.6f allow_all_zero=%s patched_guards=%s",
            _env_bool("ENTRY_ORDER_SHORT_MTF_NEUTRAL_RESCUE", True),
            _env_float("ENTRY_ORDER_SHORT_MTF_NEUTRAL_MIN_SCORE", 1.0),
            _env_float("ENTRY_ORDER_SHORT_MTF_NEUTRAL_EPS", 0.0),
            _env_bool("ENTRY_ORDER_ALLOW_ALL_ZERO_SHORT_MTF", True),
            patched_guards,
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
