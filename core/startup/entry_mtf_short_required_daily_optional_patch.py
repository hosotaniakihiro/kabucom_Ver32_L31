# ============================================================
# File   : core/startup/entry_mtf_short_required_daily_optional_patch.py
# Version: V1.1-SHORT-MTF-BACKFILL-FROM-GLOBAL-SUMMARY
# ------------------------------------------------------------
# 【目的】
#   日足MA/日足MTF 1つの逆行だけでエントリー不可になる問題を防ぐ。
#
# 【方針】
#   - 発注直前の方向ガードでは、日足込みの mtf / score_mtf は必須判定に使わない
#   - 1分・3分・5分の slope_atr_scaled_* を必須にする
#   - 日足MTFはスコア加点・参考情報として残すが、単独では発注停止しない
#
# V1.1:
#   - entry_row に slope_1m / slope_3m / slope_5m が無い場合、
#     global_context の push merged summary / summary history から補完する
#   - 現在の row.interval と一致する足は slope_atr_scaled / slope からも補完する
#   - これにより SHORT_MTF_SLOPE_MISSING で不必要に全落ちする問題を緩和
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
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


def _safe_float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None or str(v).strip() == "":
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        if s.endswith(".T"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _get(row: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return None


def _ng(reason: str, **detail: Any) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "detail": detail}


def _latest_symbol_row(df: Any, symbol: str) -> dict:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return {}
        sym = _norm_symbol(symbol)
        work = df.copy()
        work["__sym__"] = work["symbol"].map(_norm_symbol)
        work = work[work["__sym__"] == sym].copy()
        if work.empty:
            return {}
        time_col = None
        for c in ("datetime", "end_time", "start_time", "time"):
            if c in work.columns:
                time_col = c
                break
        if time_col:
            work["__dt__"] = pd.to_datetime(work[time_col], errors="coerce")
            work = work.sort_values("__dt__", kind="stable")
        return dict(work.iloc[-1].to_dict())
    except Exception:
        logger.debug("[SHORT MTF GUARD] latest row lookup failed symbol=%s", symbol, exc_info=True)
        return {}


def _slope_from_summary_row(row: dict) -> Optional[float]:
    return _safe_float_or_none(_get(row, "slope_atr_scaled", "slope", "score_slope", "disp_slope"))


def _get_gc_summary_row(symbol: str, tf: int) -> dict:
    """global_context から該当銘柄の最新summary行を取得する。"""
    try:
        from core.global_context.context import global_context as GC
    except Exception:
        return {}

    # 表示用最新summaryを優先。無ければ履歴キャッシュ。
    for getter_name in ("get_push_merged_summary", "get_merged_summary", "get_summary_history"):
        try:
            getter = getattr(GC, getter_name, None)
            if not callable(getter):
                continue
            if getter_name == "get_merged_summary":
                df = getter(tf, source="push")
            else:
                df = getter(tf)
            got = _latest_symbol_row(df, symbol)
            if got:
                return got
        except Exception:
            logger.debug("[SHORT MTF GUARD] GC getter failed getter=%s tf=%s symbol=%s", getter_name, tf, symbol, exc_info=True)
    return {}


def _resolve_short_slope(row: Dict[str, Any], *, symbol: str, tf: int) -> Optional[float]:
    """entry_row → 現在足 → global_context の順で 1/3/5分 slope を補完する。"""
    tf_s = str(tf)

    # 1) 明示列を優先
    explicit = _safe_float_or_none(
        _get(
            row,
            f"slope_atr_scaled_{tf_s}m",
            f"slope_{tf_s}m",
            f"slope{tf_s}m",
            f"score_slope_{tf_s}m",
        )
    )
    if explicit is not None:
        return explicit

    # 2) row.interval が該当足なら、汎用 slope を使う
    row_interval = _safe_int(_get(row, "interval"), 0)
    if row_interval == tf:
        cur = _safe_float_or_none(_get(row, "slope_atr_scaled", "slope", "score_slope", "disp_slope"))
        if cur is not None:
            return cur

    # 3) global_context の該当足summaryから補完
    gc_row = _get_gc_summary_row(symbol, tf)
    val = _slope_from_summary_row(gc_row)
    if val is not None:
        return val

    return None


def _short_mtf_direction_guard(entry_row: Dict[str, Any], *, symbol: str, side: str, source: str) -> Optional[Dict[str, Any]]:
    if not _env_bool("ENTRY_SHORT_MTF_REQUIRED", True):
        return None

    if str(source or "").upper() != "SUMMARY_AI":
        return None

    side_u = str(side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return None

    row = entry_row or {}
    sym = _norm_symbol(symbol or row.get("symbol"))
    eps = abs(_env_float("ENTRY_SHORT_MTF_SLOPE_EPS", 0.0))
    require_all = _env_bool("ENTRY_SHORT_MTF_REQUIRE_ALL", True)

    slopes = {
        "slope_1m": _resolve_short_slope(row, symbol=sym, tf=1),
        "slope_3m": _resolve_short_slope(row, symbol=sym, tf=3),
        "slope_5m": _resolve_short_slope(row, symbol=sym, tf=5),
    }

    missing = [k for k, v in slopes.items() if v is None]
    if missing and require_all:
        return _ng(
            "SHORT_MTF_SLOPE_MISSING",
            symbol=sym,
            side=side_u,
            missing=missing,
            slopes=slopes,
            daily_mtf_optional=_env_bool("ENTRY_DAILY_MTF_OPTIONAL", True),
            note="short slopes were not found in entry_row or global_context summaries",
        )

    available = {k: v for k, v in slopes.items() if v is not None}
    if not available:
        return _ng(
            "SHORT_MTF_NO_DATA",
            symbol=sym,
            side=side_u,
            slopes=slopes,
            daily_mtf_optional=_env_bool("ENTRY_DAILY_MTF_OPTIONAL", True),
        )

    if side_u == "BUY":
        bad = {k: v for k, v in available.items() if v <= eps}
        if bad:
            return _ng(
                "SHORT_MTF_NOT_BUY_ALIGNED",
                symbol=sym,
                side=side_u,
                bad=bad,
                slopes=slopes,
                eps=eps,
                note="daily_mtf_is_optional_not_blocking",
            )
    else:
        bad = {k: v for k, v in available.items() if v >= -eps}
        if bad:
            return _ng(
                "SHORT_MTF_NOT_SELL_ALIGNED",
                symbol=sym,
                side=side_u,
                bad=bad,
                slopes=slopes,
                eps=eps,
                note="daily_mtf_is_optional_not_blocking",
            )

    logger.warning(
        "[SHORT MTF GUARD] OK symbol=%s side=%s slopes=%s daily_mtf_optional=%s",
        sym,
        side_u,
        slopes,
        _env_bool("ENTRY_DAILY_MTF_OPTIONAL", True),
    )
    return None


def _strict_guard_short_mtf_only(*, symbol: str, side: str, row: dict, detail: dict) -> Optional[Dict[str, Any]]:
    return _short_mtf_direction_guard(row, symbol=symbol, side=side, source="SUMMARY_AI")


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    os.environ.setdefault("ENTRY_SHORT_MTF_REQUIRED", "1")
    os.environ.setdefault("ENTRY_SHORT_MTF_REQUIRE_ALL", "1")
    os.environ.setdefault("ENTRY_SHORT_MTF_SLOPE_EPS", "0.0")
    os.environ.setdefault("ENTRY_DAILY_MTF_OPTIONAL", "1")

    ok_any = False

    try:
        import trading.handlers.entry_order_builder as eob
        old = getattr(eob, "_summary_mtf_direction_guard", None)
        if callable(old) and not getattr(old, "_short_required_daily_optional_v11", False):
            _short_mtf_direction_guard._short_required_daily_optional_v11 = True  # type: ignore[attr-defined]
            _short_mtf_direction_guard._original = old  # type: ignore[attr-defined]
            eob._summary_mtf_direction_guard = _short_mtf_direction_guard
            ok_any = True
            logger.warning("[SHORT MTF GUARD] patched entry_order_builder._summary_mtf_direction_guard v1.1")
    except Exception:
        logger.exception("[SHORT MTF GUARD] patch entry_order_builder failed")

    try:
        import core.startup.entry_limit_passive_runtime_patch as elp
        old2 = getattr(elp, "_summary_ai_strict_guard", None)
        if callable(old2) and not getattr(old2, "_short_required_daily_optional_v11", False):
            def _patched_summary_ai_strict_guard(*, symbol: str, side: str, row: dict, detail: dict):
                return _strict_guard_short_mtf_only(symbol=symbol, side=side, row=row, detail=detail)

            _patched_summary_ai_strict_guard._short_required_daily_optional_v11 = True  # type: ignore[attr-defined]
            _patched_summary_ai_strict_guard._original = old2  # type: ignore[attr-defined]
            elp._summary_ai_strict_guard = _patched_summary_ai_strict_guard
            ok_any = True
            logger.warning("[SHORT MTF GUARD] patched entry_limit_passive_runtime_patch._summary_ai_strict_guard v1.1")
    except Exception:
        logger.exception("[SHORT MTF GUARD] patch entry_limit_passive_runtime_patch failed")

    _PATCHED = bool(ok_any)
    logger.warning(
        "[SHORT MTF GUARD] installed=%s required=%s require_all=%s eps=%s daily_optional=%s backfill_from_gc=True",
        _PATCHED,
        os.getenv("ENTRY_SHORT_MTF_REQUIRED"),
        os.getenv("ENTRY_SHORT_MTF_REQUIRE_ALL"),
        os.getenv("ENTRY_SHORT_MTF_SLOPE_EPS"),
        os.getenv("ENTRY_DAILY_MTF_OPTIONAL"),
    )
    return _PATCHED


try:
    install()
except Exception:
    logger.exception("[SHORT MTF GUARD] auto install failed")

__all__ = ["install"]
