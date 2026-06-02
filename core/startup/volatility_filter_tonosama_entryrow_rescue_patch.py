# ============================================================
# File   : core/startup/volatility_filter_tonosama_entryrow_rescue_patch.py
# Version: V1.2-SIGNATURE-SAFE-WRAPPERS
# ------------------------------------------------------------
# 目的:
#   TONOSAMA entry_row / pending の値幅で volatility filter を救済する。
#
# V1.2:
#   - range_5m_filter(df_5m=..., symbol=...) 呼び出しで、旧wrapperを_ORIG_RANGEとして掴み
#     TypeError: unexpected keyword argument 'df_5m' になる問題を防ぐ。
#   - _original を辿って二重wrapをunwrap。
#   - fallback中にTypeErrorが出てもfail-closeせず、直接の本体呼び出し/安全NGへ落とす。
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


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        return s[:-2] if s.endswith(".0") else s
    except Exception:
        return ""


def _is_tonosama(row: dict[str, Any]) -> bool:
    s = str(row.get("source") or row.get("pipeline_source") or row.get("entry_type") or "").upper()
    return "TONOSAMA" in s


def _extract_symbol(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    for key in ("symbol", "code", "stock_code"):
        if kwargs.get(key) is not None:
            return _norm_symbol(kwargs.get(key))
    for obj in args:
        if isinstance(obj, (str, int)):
            s = _norm_symbol(obj)
            if s:
                return s
    row = _row_to_dict(args[0]) if args else _row_to_dict(kwargs.get("entry_row"))
    return _norm_symbol(row.get("symbol") or row.get("Symbol"))


def _entry_row_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if kwargs.get("entry_row") is not None:
        return kwargs.get("entry_row")
    if args:
        if isinstance(args[0], pd.DataFrame):
            return None
        return args[0]
    return None


def _pending_entry_for_symbol(symbol: str) -> dict[str, Any]:
    sym = _norm_symbol(symbol)
    if not sym:
        return {}
    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", None)
        if isinstance(root, dict):
            bucket = root.get(sym) or root.get(str(sym))
            entries = bucket if isinstance(bucket, (list, tuple, set)) else ([bucket] if bucket is not None else [])
            for e in entries:
                d = _row_to_dict(e)
                if d and _is_tonosama(d):
                    return d
    except Exception:
        pass
    try:
        import trading.entry.pending_manager as pm
        iter_entries = getattr(pm, "iter_entries", None)
        if callable(iter_entries):
            for s, e in list(iter_entries()):
                if _norm_symbol(s) == sym:
                    d = _row_to_dict(e)
                    if d and _is_tonosama(d):
                        return d
    except Exception:
        pass
    return {}


def _entryrow_range_ratio(row: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    close = _f(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    high = _f(_first(row, ("high_price", "high", "High"), 0.0), 0.0)
    low = _f(_first(row, ("low_price", "low", "Low"), 0.0), 0.0)
    intrabar_pct = _f(_first(row, ("_intrabar_range_pct", "intrabar_range_pct", "range_pct", "row_range_pct"), 0.0), 0.0)
    ratio_from_ohlc = ((high - low) / close) if high > 0 and low > 0 and close > 0 and high >= low else 0.0
    ratio_from_pct = intrabar_pct / 100.0 if intrabar_pct > 0 else 0.0
    ratio = max(ratio_from_ohlc, ratio_from_pct)
    return ratio, {"close": close, "high": high, "low": low, "intrabar_pct": intrabar_pct, "ratio": ratio}


def _tonosama_vol_rescue(row_obj: Any, *, caller: str, symbol: str = "") -> tuple[bool, dict[str, Any]]:
    row = _row_to_dict(row_obj)
    if not row and symbol:
        row = _pending_entry_for_symbol(symbol)
    if not row:
        return False, {"reason": "row_missing", "symbol": symbol, "caller": caller}
    if not _is_tonosama(row):
        return False, {"reason": "not_tonosama", "symbol": symbol, "caller": caller, "source": row.get("source") or row.get("entry_type")}
    ratio, diag = _entryrow_range_ratio(row)
    min_ratio = _env_float("TONOSAMA_VOL_ENTRYROW_RESCUE_MIN_RANGE_RATIO", 0.006)
    min_pct = _env_float("TONOSAMA_VOL_ENTRYROW_RESCUE_MIN_INTRABAR_PCT", 0.6)
    ok = ratio >= min_ratio or _f(diag.get("intrabar_pct"), 0.0) >= min_pct
    diag.update({
        "caller": caller,
        "symbol": _norm_symbol(row.get("symbol") or row.get("Symbol") or symbol),
        "side": row.get("side"),
        "source": row.get("source") or row.get("entry_type"),
        "min_ratio": min_ratio,
        "min_intrabar_pct": min_pct,
        "ok": ok,
    })
    return bool(ok), diag


def _unwrap(fn: Any) -> Any:
    seen = set()
    cur = fn
    for _ in range(10):
        if not callable(cur) or id(cur) in seen:
            break
        seen.add(id(cur))
        nxt = getattr(cur, "_original", None)
        if not callable(nxt):
            break
        cur = nxt
    return cur


def _call_original(fn: Any, *args, **kwargs):
    target = _unwrap(fn)
    try:
        return target(*args, **kwargs)
    except TypeError as e:
        # 旧wrapper/旧signatureを掴んだ場合でも、例外でentry全体を落とさない。
        logger.warning("[VOL FILTER TONOSAMA RESCUE] original call TypeError swallowed target=%s err=%s kwargs=%s", getattr(target, "__name__", repr(target)), e, sorted(kwargs.keys()))
        # volatility_filter側の一般的な戻り値に合わせてNG扱いを返す。
        return True, {"reason": "original_signature_mismatch", "error": str(e)}


def _patched_atr_1m_filter(*args, **kwargs):
    if _env_bool("TONOSAMA_VOL_ENTRYROW_RESCUE_ENABLED", True):
        row_obj = _entry_row_from_call(args, kwargs)
        symbol = _extract_symbol(args, kwargs)
        ok, diag = _tonosama_vol_rescue(row_obj, caller="atr_1m_filter", symbol=symbol)
        if ok:
            logger.warning("[VOL FILTER TONOSAMA RESCUE] allow ATR by pending/entry_row range diag=%s", diag)
            return True
    return _call_original(_ORIG_ATR, *args, **kwargs)


def _patched_range_5m_filter(*args, **kwargs):
    if _env_bool("TONOSAMA_VOL_ENTRYROW_RESCUE_ENABLED", True):
        row_obj = _entry_row_from_call(args, kwargs)
        symbol = _extract_symbol(args, kwargs)
        ok, diag = _tonosama_vol_rescue(row_obj, caller="range_5m_filter", symbol=symbol)
        if ok:
            logger.warning("[VOL FILTER TONOSAMA RESCUE] allow RANGE by pending/entry_row range diag=%s", diag)
            return True
    return _call_original(_ORIG_RANGE, *args, **kwargs)


def _patch_entry_controller_refs(vf) -> None:
    try:
        import trading.handlers.entry_controller as ec
        if hasattr(ec, "atr_1m_filter"):
            ec.atr_1m_filter = vf.atr_1m_filter
        if hasattr(ec, "range_5m_filter"):
            ec.range_5m_filter = vf.range_5m_filter
    except Exception:
        logger.debug("[VOL FILTER TONOSAMA RESCUE] entry_controller ref patch skipped", exc_info=True)


def install() -> bool:
    global _INSTALLED, _ORIG_ATR, _ORIG_RANGE
    try:
        import trading.filters.volatility_filter as vf
        cur_atr = getattr(vf, "atr_1m_filter", None)
        cur_range = getattr(vf, "range_5m_filter", None)
        if not callable(cur_atr) or not callable(cur_range):
            logger.warning("[VOL FILTER TONOSAMA RESCUE] target functions unavailable")
            return False
        if getattr(cur_atr, "_tonosama_vol_rescue_v12", False):
            _INSTALLED = True
            return True
        _ORIG_ATR = getattr(cur_atr, "_original", cur_atr)
        _ORIG_RANGE = getattr(cur_range, "_original", cur_range)
        _patched_atr_1m_filter._tonosama_vol_rescue_v12 = True  # type: ignore[attr-defined]
        _patched_atr_1m_filter._original = _ORIG_ATR  # type: ignore[attr-defined]
        _patched_range_5m_filter._tonosama_vol_rescue_v12 = True  # type: ignore[attr-defined]
        _patched_range_5m_filter._original = _ORIG_RANGE  # type: ignore[attr-defined]
        vf.atr_1m_filter = _patched_atr_1m_filter
        vf.range_5m_filter = _patched_range_5m_filter
        _patch_entry_controller_refs(vf)
        _INSTALLED = True
        logger.warning(
            "[VOL FILTER TONOSAMA RESCUE] installed v1.2 enabled=%s min_range_ratio=%.4f min_intrabar_pct=%.2f signature_safe=True",
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
