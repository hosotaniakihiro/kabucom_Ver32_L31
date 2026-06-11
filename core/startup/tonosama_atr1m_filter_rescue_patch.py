# ============================================================
# File   : core/startup/tonosama_atr1m_filter_rescue_patch.py
# Version: V2-TONOSAMA-ATR1M-FILTER-RESCUE-OPT-IN
# ------------------------------------------------------------
# Legacy rescue shim.  The original patch allowed Tonosama candidates to pass
# the generic ATR 1m filter when the 1m history was short.  Keep it available as
# an explicit escape hatch, but do not wrap entry_controller by default.
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_EC_ATR = None


def _env_on(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _legacy_rescue_enabled() -> bool:
    return _env_on("USERCUSTOMIZE_ENABLE_LEGACY_TONOSAMA_FAILOPEN_PATCHES", False) or _env_on(
        "TONOSAMA_ATR1M_FILTER_RESCUE", False
    )


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _sf(v: Any, default: float | None = 0.0) -> float | None:
    try:
        if v is None:
            return default
        if isinstance(v, str):
            s = v.strip().replace(",", "").replace("%", "").replace("％", "")
            if not s or s.lower() in {"none", "nan", "null", "<na>"}:
                return default
            return float(s)
        return float(v)
    except Exception:
        return default


def _su(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_tonosama(row: Any) -> bool:
    try:
        return isinstance(row, dict) and (_su(row.get("source")) == "TONOSAMA" or _su(row.get("entry_type")) == "TONOSAMA")
    except Exception:
        return False


def _get_num(row: dict, keys: tuple[str, ...], default: float = 0.0) -> float:
    for k in keys:
        if k in row:
            v = _sf(row.get(k), None)
            if v is not None and float(v) != 0.0:
                return float(v)
    raw = row.get("_raw") or row.get("raw") or row.get("source_row")
    if hasattr(raw, "to_dict"):
        try:
            raw = raw.to_dict()
        except Exception:
            raw = None
    if isinstance(raw, dict):
        for k in keys:
            if k in raw:
                v = _sf(raw.get(k), None)
                if v is not None and float(v) != 0.0:
                    return float(v)
    return float(default)


def _should_rescue(row: dict) -> tuple[bool, str]:
    if not _legacy_rescue_enabled():
        return False, "legacy_rescue_disabled"
    if not _is_tonosama(row):
        return False, "not_tonosama"

    rng = _get_num(row, ("_intrabar_range_pct", "intrabar_range_pct", "range_pct", "_range_pct"), 0.0)
    surge = _get_num(row, ("_max_volume_surge_ratio", "max_volume_surge_ratio", "volume_surge_ratio", "surge_ratio", "volume_speed"), 0.0)
    vol = _get_num(row, ("_latest_volume", "latest_volume", "volume", "trading_volume"), 0.0)
    score = abs(_get_num(row, ("_tonosama_score", "tonosama_score", "score", "final_score"), 0.0))
    close = _get_num(row, ("close", "price", "current_price", "close_price"), 0.0)

    min_range = _env_float("TONOSAMA_ATR1M_RESCUE_MIN_INTRABAR_RANGE_PCT", 3.0)
    min_surge = _env_float("TONOSAMA_ATR1M_RESCUE_MIN_SURGE", 3.0)
    min_vol = _env_float("TONOSAMA_ATR1M_RESCUE_MIN_VOLUME", 50000.0)
    min_score = _env_float("TONOSAMA_ATR1M_RESCUE_MIN_SCORE", 2.0)

    if rng >= min_range and surge >= min_surge and vol >= min_vol and score >= min_score:
        return True, (
            f"raw_range={rng:.3f}>={min_range:.3f} surge={surge:.2f}>={min_surge:.2f} "
            f"vol={vol:.0f}>={min_vol:.0f} score={score:.3f}>={min_score:.3f} close={close:.1f}"
        )
    return False, (
        f"weak raw_range={rng:.3f}/{min_range:.3f} surge={surge:.2f}/{min_surge:.2f} "
        f"vol={vol:.0f}/{min_vol:.0f} score={score:.3f}/{min_score:.3f} close={close:.1f}"
    )


def _patched_atr_1m_filter(row: dict) -> bool:
    try:
        ok = bool(_ORIG_EC_ATR(row))
        if ok:
            return True
    except Exception:
        logger.exception("[TONOSAMA ATR1M RESCUE] original atr_1m_filter failed")
        ok = False

    try:
        rescue, detail = _should_rescue(row)
        symbol = row.get("symbol") if isinstance(row, dict) else None
        side = (row.get("side") or row.get("entry_decision")) if isinstance(row, dict) else None
        if rescue:
            logger.warning("[TONOSAMA ATR1M RESCUE] OK symbol=%s side=%s detail=%s", symbol, side, detail)
            return True
        if _is_tonosama(row):
            logger.info("[TONOSAMA ATR1M RESCUE] keep NG symbol=%s side=%s detail=%s", symbol, side, detail)
    except Exception:
        logger.debug("[TONOSAMA ATR1M RESCUE] rescue check failed", exc_info=True)
    return bool(ok)


def install() -> bool:
    global _INSTALLED, _ORIG_EC_ATR
    if _INSTALLED:
        return True
    if not _legacy_rescue_enabled():
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA ATR1M RESCUE] skipped; legacy rescue disabled. "
            "Set USERCUSTOMIZE_ENABLE_LEGACY_TONOSAMA_FAILOPEN_PATCHES=1 or TONOSAMA_ATR1M_FILTER_RESCUE=1 to restore."
        )
        return True
    try:
        import trading.handlers.entry_controller as ec
        import trading.filters.volatility_filter as vf

        cur = getattr(ec, "atr_1m_filter", None)
        if not callable(cur):
            logger.warning("[TONOSAMA ATR1M RESCUE] target missing ec.atr_1m_filter")
            return False
        if getattr(cur, "_tonosama_atr1m_rescue_v2", False) or getattr(cur, "_tonosama_atr1m_rescue_v1", False):
            _INSTALLED = True
            return True

        _ORIG_EC_ATR = getattr(cur, "_original", cur)
        _patched_atr_1m_filter._tonosama_atr1m_rescue_v2 = True  # type: ignore[attr-defined]
        _patched_atr_1m_filter._tonosama_atr1m_rescue_v1 = True  # type: ignore[attr-defined]
        _patched_atr_1m_filter._original = _ORIG_EC_ATR  # type: ignore[attr-defined]
        ec.atr_1m_filter = _patched_atr_1m_filter
        if callable(getattr(vf, "atr_1m_filter", None)):
            vf.atr_1m_filter = _patched_atr_1m_filter
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA ATR1M RESCUE] installed legacy v2 min_range=%s min_surge=%s min_volume=%s min_score=%s",
            os.getenv("TONOSAMA_ATR1M_RESCUE_MIN_INTRABAR_RANGE_PCT", "3.0"),
            os.getenv("TONOSAMA_ATR1M_RESCUE_MIN_SURGE", "3.0"),
            os.getenv("TONOSAMA_ATR1M_RESCUE_MIN_VOLUME", "50000"),
            os.getenv("TONOSAMA_ATR1M_RESCUE_MIN_SCORE", "2.0"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA ATR1M RESCUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA ATR1M RESCUE] auto install failed")


__all__ = ["install"]
