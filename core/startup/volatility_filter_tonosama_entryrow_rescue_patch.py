# ============================================================
# File   : core/startup/volatility_filter_tonosama_entryrow_rescue_patch.py
# Version: V1.0-TONOSAMA-ENTRYROW-VOL-RESCUE
# ------------------------------------------------------------
# 目的:
#   寄り直後は1m履歴が15本未満で volatility_filter.atr_1m_filter(entry_row)
#   が fail-close し、TONOSAMA が board/credit/AI まで通った後に
#   [VOL FILTER] ATR df fallback fail-close reason=1m本数不足 bars=0
#   で止まる。
#
# 方針:
#   - TONOSAMA entry_row 呼び出しだけ救済
#   - entry_row 自体の high-low / close または _intrabar_range_pct が十分なら許可
#   - SUMMARY/RANKING の通常フィルタは維持
#   - 既存の position/credit/board/final safety guard は維持
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_ATR = None
_ORIG_RANGE = None


def _env_bool(name: str, default: bool = True) -> bool:
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


def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return dict(row)
        if isinstance(row, pd.Series):
            return row.to_dict()
        if hasattr(row, "to_dict"):
            v = row.to_dict()
            if isinstance(v, dict):
                return dict(v)
    except Exception:
        pass
    return {}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return float(default)


def _first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _is_entry_row_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    if kwargs.get("df_1m") is not None or kwargs.get("df_5m") is not None or kwargs.get("symbol") is not None:
        return False
    if args and args[0] is not None:
        return True
    if kwargs.get("entry_row") is not None:
        return True
    return False


def _entry_row_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if args:
        return args[0]
    return kwargs.get("entry_row")


def _is_tonosama(row: dict[str, Any]) -> bool:
    s = str(row.get("source") or row.get("pipeline_source") or row.get("entry_type") or "").upper()
    return "TONOSAMA" in s


def _entryrow_range_ratio(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    close = _f(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    high = _f(_first(row, ("high_price", "high", "High"), 0.0), 0.0)
    low = _f(_first(row, ("low_price", "low", "Low"), 0.0), 0.0)
    intrabar_pct = _f(_first(row, ("_intrabar_range_pct", "intrabar_range_pct", "range_pct"), 0.0), 0.0)
    ratio_from_ohlc = ((high - low) / close) if high > 0 and low > 0 and close > 0 and high >= low else 0.0
    ratio_from_pct = intrabar_pct / 100.0 if intrabar_pct > 0 else 0.0
    ratio = max(ratio_from_ohlc, ratio_from_pct)
    return ratio, {"close": close, "high": high, "low": low, "intrabar_pct": intrabar_pct, "ratio": ratio}


def _tonosama_vol_rescue(row_obj: Any, *, caller: str) -> tuple[bool, dict[str, Any]]:
    row = _row_to_dict(row_obj)
    if not row or not _is_tonosama(row):
        return False, {"reason": "not_tonosama"}
    ratio, diag = _entryrow_range_ratio(row)
    min_ratio = _env_float("TONOSAMA_VOL_ENTRYROW_RESCUE_MIN_RANGE_RATIO", 0.006)
    min_pct = _env_float("TONOSAMA_VOL_ENTRYROW_RESCUE_MIN_INTRABAR_PCT", 0.6)
    # ratioはhigh-low/close、intrabar_pctは%表記。どちらかを満たせば救済。
    ok = ratio >= min_ratio or _f(diag.get("intrabar_pct"), 0.0) >= min_pct
    diag.update({
        "caller": caller,
        "symbol": row.get("symbol") or row.get("Symbol"),
        "side": row.get("side"),
        "source": row.get("source") or row.get("entry_type"),
        "min_ratio": min_ratio,
        "min_intrabar_pct": min_pct,
        "ok": ok,
    })
    return bool(ok), diag


def _patched_atr_1m_filter(*args, **kwargs):
    if _is_entry_row_call(args, kwargs) and _env_bool("TONOSAMA_VOL_ENTRYROW_RESCUE_ENABLED", True):
        row_obj = _entry_row_from_call(args, kwargs)
        ok, diag = _tonosama_vol_rescue(row_obj, caller="atr_1m_filter")
        if ok:
            logger.warning("[VOL FILTER TONOSAMA RESCUE] allow ATR by entry_row range diag=%s", diag)
            return True
    return _ORIG_ATR(*args, **kwargs)  # type: ignore[misc]


def _patched_range_5m_filter(*args, **kwargs):
    if _is_entry_row_call(args, kwargs) and _env_bool("TONOSAMA_VOL_ENTRYROW_RESCUE_ENABLED", True):
        row_obj = _entry_row_from_call(args, kwargs)
        ok, diag = _tonosama_vol_rescue(row_obj, caller="range_5m_filter")
        if ok:
            logger.warning("[VOL FILTER TONOSAMA RESCUE] allow RANGE by entry_row range diag=%s", diag)
            return True
    return _ORIG_RANGE(*args, **kwargs)  # type: ignore[misc]


def _patch_entry_controller_refs(vf) -> None:
    try:
        import trading.handlers.entry_controller as ec
        # entry_controller が from ... import atr_1m_filter している場合に備える。
        if hasattr(ec, "atr_1m_filter"):
            ec.atr_1m_filter = vf.atr_1m_filter
        if hasattr(ec, "range_5m_filter"):
            ec.range_5m_filter = vf.range_5m_filter
    except Exception:
        logger.debug("[VOL FILTER TONOSAMA RESCUE] entry_controller ref patch skipped", exc_info=True)


def install() -> bool:
    global _INSTALLED, _ORIG_ATR, _ORIG_RANGE
    if _INSTALLED:
        return True
    try:
        import trading.filters.volatility_filter as vf
        cur_atr = getattr(vf, "atr_1m_filter", None)
        cur_range = getattr(vf, "range_5m_filter", None)
        if not callable(cur_atr) or not callable(cur_range):
            logger.warning("[VOL FILTER TONOSAMA RESCUE] target functions unavailable")
            return False
        if getattr(cur_atr, "_tonosama_vol_rescue_v1", False):
            _INSTALLED = True
            return True
        _ORIG_ATR = cur_atr
        _ORIG_RANGE = cur_range
        _patched_atr_1m_filter._tonosama_vol_rescue_v1 = True  # type: ignore[attr-defined]
        _patched_atr_1m_filter._original = cur_atr  # type: ignore[attr-defined]
        _patched_range_5m_filter._tonosama_vol_rescue_v1 = True  # type: ignore[attr-defined]
        _patched_range_5m_filter._original = cur_range  # type: ignore[attr-defined]
        vf.atr_1m_filter = _patched_atr_1m_filter
        vf.range_5m_filter = _patched_range_5m_filter
        _patch_entry_controller_refs(vf)
        _INSTALLED = True
        logger.warning(
            "[VOL FILTER TONOSAMA RESCUE] installed enabled=%s min_range_ratio=%.4f min_intrabar_pct=%.2f",
            _env_bool("TONOSAMA_VOL_ENTRYROW_RESCUE_ENABLED", True),
            _env_float("TONOSAMA_VOL_ENTRYROW_RESCUE_MIN_RANGE_RATIO", 0.006),
            _env_float("TONOSAMA_VOL_ENTRYROW_RESCUE_MIN_INTRABAR_PCT", 0.6),
        )
        return True
    except Exception:
        logger.exception("[VOL FILTER TONOSAMA RESCUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[VOL FILTER TONOSAMA RESCUE] auto install failed")

__all__ = ["install"]
