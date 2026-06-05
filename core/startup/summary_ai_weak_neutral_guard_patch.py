# ============================================================
# File   : core/startup/summary_ai_weak_neutral_guard_patch.py
# Version: V3-DISABLED-BY-DEFAULT
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI approved化直前の弱中立ガード。
#
# V3:
#   - エントリー数不足対策。
#   - 直近ログで AI_OK 5件が全て weak_neutral_no_technical_basis で
#     allow=False にされ、approved=0 / no_ai_ok になっていた。
#   - デフォルトではこのガードを無効化し、AI_OK 候補を approved 選抜へ進める。
#   - 必要な場合のみ SUMMARY_AI_WEAK_NEUTRAL_GUARD_ENABLED=1 で再有効化。
#
# 背景:
#   寄り直後やPUSH 1分足では rsi=50, macd=0, slope=0 になりやすい。
#   この状態で score=1.0 のSELL候補を全削除すると、エントリーが発火しない。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_BUILD = None


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
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


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        s = str(v).strip()
        if s == "" or s.lower() in {"none", "nan", "nat"}:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _norm_side(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        return s if s in {"BUY", "SELL"} else ""
    except Exception:
        return ""


def _dicts(item: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    yield item
    for k in ("ai_row", "source_row", "row", "src", "entry", "entry_row"):
        v = item.get(k)
        if isinstance(v, dict):
            yield v


def _first(item: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for d in _dicts(item):
        for k in keys:
            if k in d and d.get(k) is not None:
                return d.get(k)
    return default


def _side(item: Dict[str, Any]) -> str:
    return _norm_side(_first(item, "side", "ai_side", "entry_decision", default="")) or "BUY"


def _source(item: Dict[str, Any]) -> str:
    try:
        return str(_first(item, "source", default="SUMMARY") or "SUMMARY").strip().upper()
    except Exception:
        return "SUMMARY"


def _side_score(item: Dict[str, Any], side: str) -> float:
    if side == "SELL":
        return max(
            _safe_float(_first(item, "sell_score", "score_sell", default=0.0)),
            abs(_safe_float(_first(item, "score_total", "total_score", "score", default=0.0))),
            abs(_safe_float(_first(item, "final_score", "display_score", default=0.0))),
        )
    return max(
        _safe_float(_first(item, "buy_score", "score_buy", default=0.0)),
        abs(_safe_float(_first(item, "score_total", "total_score", "score", default=0.0))),
        abs(_safe_float(_first(item, "final_score", "display_score", default=0.0))),
    )


def _is_weak_neutral(item: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
    side = _side(item)
    source = _source(item)
    source_ok = source in {"SUMMARY", "PUSH", "PUSH_SUMMARY", "SUMMARY_AI", "YAHOO", "YAHOO_SUMMARY", ""}
    if not source_ok:
        return False, {"reason": "source_skip", "source": source, "side": side}
    slope = _safe_float(_first(item, "slope", "disp_slope", default=0.0))
    macd = _safe_float(_first(item, "macd", default=0.0))
    signal = _safe_float(_first(item, "signal", default=0.0))
    rsi = _safe_float(_first(item, "rsi", default=50.0), 50.0)
    score = _side_score(item, side)
    mtf = _safe_float(_first(item, "mtf", "score_mtf", "mtf_score", default=0.0))
    max_score = _env_float("SUMMARY_AI_WEAK_NEUTRAL_MAX_SIDE_SCORE", 1.25)
    slope_eps = abs(_env_float("SUMMARY_AI_WEAK_NEUTRAL_SLOPE_EPS", 0.0002))
    macd_eps = abs(_env_float("SUMMARY_AI_WEAK_NEUTRAL_MACD_EPS", 0.0001))
    rsi_low = _env_float("SUMMARY_AI_WEAK_NEUTRAL_RSI_LOW", 45.0)
    rsi_high = _env_float("SUMMARY_AI_WEAK_NEUTRAL_RSI_HIGH", 55.0)
    macd_neutral = abs(macd) <= macd_eps and abs(signal) <= macd_eps
    weak = bool(score <= max_score and abs(slope) <= slope_eps and macd_neutral and rsi_low <= rsi <= rsi_high)
    return weak, {"side": side, "source": source, "score": score, "slope": slope, "macd": macd, "signal": signal, "rsi": rsi, "mtf": mtf, "max_score": max_score, "slope_eps": slope_eps, "macd_eps": macd_eps, "rsi_low": rsi_low, "rsi_high": rsi_high}


def _patched_build_ai_ok_approved_rows(ai_results, *args, **kwargs):
    if not _env_bool("SUMMARY_AI_WEAK_NEUTRAL_GUARD_ENABLED", False):
        return _ORIG_BUILD(ai_results, *args, **kwargs)  # type: ignore[misc]
    kept = []
    skipped = []
    try:
        for item in list(ai_results or []):
            if not isinstance(item, dict):
                kept.append(item)
                continue
            if not bool(item.get("allow")):
                kept.append(item)
                continue
            weak, diag = _is_weak_neutral(item)
            if weak:
                x = dict(item)
                x["allow"] = False
                x["reason"] = (str(item.get("reason") or "") + "|weak_neutral_no_technical_basis").strip("|")
                skipped.append({"symbol": _first(item, "symbol", default=""), "side": diag.get("side"), "source": diag.get("source"), "score": round(float(diag.get("score") or 0.0), 4), "slope": round(float(diag.get("slope") or 0.0), 6), "macd": round(float(diag.get("macd") or 0.0), 6), "signal": round(float(diag.get("signal") or 0.0), 6), "rsi": round(float(diag.get("rsi") or 0.0), 2), "mtf": round(float(diag.get("mtf") or 0.0), 3), "reason": "weak_neutral_no_technical_basis"})
                kept.append(x)
            else:
                kept.append(item)
    except Exception:
        logger.exception("[SUMMARY AI WEAK NEUTRAL GUARD] prefilter failed; fail-open")
        return _ORIG_BUILD(ai_results, *args, **kwargs)  # type: ignore[misc]
    if skipped:
        logger.warning("[SUMMARY AI WEAK NEUTRAL GUARD] removed before approved before=%s after_allow=%s skipped=%s", len(list(ai_results or [])), sum(1 for x in kept if isinstance(x, dict) and bool(x.get("allow"))), skipped[:50])
    return _ORIG_BUILD(kept, *args, **kwargs)  # type: ignore[misc]


def install() -> bool:
    global _INSTALLED, _ORIG_BUILD
    try:
        os.environ.setdefault("SUMMARY_AI_WEAK_NEUTRAL_GUARD_ENABLED", "0")
        import trading.entry.summary_ai.executor as executor
        fn = getattr(executor, "build_ai_ok_approved_rows", None)
        if getattr(fn, "_summary_ai_weak_neutral_guard_v3", False):
            _INSTALLED = True
            return True
        original = getattr(fn, "_original", None) if callable(fn) else None
        if callable(original):
            _ORIG_BUILD = original
        elif callable(fn):
            _ORIG_BUILD = fn
        else:
            logger.warning("[SUMMARY AI WEAK NEUTRAL GUARD] target not found")
            return False
        _patched_build_ai_ok_approved_rows._summary_ai_weak_neutral_guard_v3 = True  # type: ignore[attr-defined]
        _patched_build_ai_ok_approved_rows._original = _ORIG_BUILD  # type: ignore[attr-defined]
        executor.build_ai_ok_approved_rows = _patched_build_ai_ok_approved_rows
        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI WEAK NEUTRAL GUARD] installed v3 enabled=%s default_off=1 max_score=%.3f slope_eps=%.6f rsi=[%.1f, %.1f]",
            _env_bool("SUMMARY_AI_WEAK_NEUTRAL_GUARD_ENABLED", False),
            _env_float("SUMMARY_AI_WEAK_NEUTRAL_MAX_SIDE_SCORE", 1.25),
            abs(_env_float("SUMMARY_AI_WEAK_NEUTRAL_SLOPE_EPS", 0.0002)),
            _env_float("SUMMARY_AI_WEAK_NEUTRAL_RSI_LOW", 45.0),
            _env_float("SUMMARY_AI_WEAK_NEUTRAL_RSI_HIGH", 55.0),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI WEAK NEUTRAL GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI WEAK NEUTRAL GUARD] auto install failed")

__all__ = ["install"]
