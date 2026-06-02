# ============================================================
# File   : core/startup/tonosama_range5m_filter_rescue_patch.py
# Version: V1-TONOSAMA-RANGE5M-FILTER-RESCUE
# ------------------------------------------------------------
# Purpose:
#   Tonosama candidates are created from volume-surge/intrabar features.
#   entry_controller runs trading.filters.volatility_filter.range_5m_filter
#   before AI gate. For Tonosama, this can reject a strong candidate when
#   the generic 5m df fallback ratio is small:
#
#     RANGE_5M_FILTER_NG detail={'side': 'BUY'}
#     [VOL FILTER] RANGE df fallback allow=False ratio=0.0042 min_pct=0.012
#
#   If Tonosama raw features show strong intrabar range and volume surge,
#   allow it to proceed to AI/final safety instead of stopping here.
#
# Scope:
#   - Only source/entry_type == TONOSAMA
#   - SUMMARY/RANKING are unchanged
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_EC_RANGE = None
_ORIG_VF_RANGE = None


def _env_on(name: str, default: bool = True) -> bool:
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
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        if isinstance(v, str):
            s = v.strip().replace(",", "").replace("%", "").replace("％", "")
            if not s or s.lower() in {"none", "nan", "null", "<na>"}:
                return float(default)
            return float(s)
        return float(v)
    except Exception:
        return float(default)


def _su(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_tonosama(row: Any) -> bool:
    try:
        if not isinstance(row, dict):
            return False
        return _su(row.get("source")) == "TONOSAMA" or _su(row.get("entry_type")) == "TONOSAMA"
    except Exception:
        return False


def _get_num(row: dict, keys: tuple[str, ...], default: float = 0.0) -> float:
    for k in keys:
        if k in row:
            v = _sf(row.get(k), None)  # type: ignore[arg-type]
            if v is not None and v != 0.0:
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
                v = _sf(raw.get(k), None)  # type: ignore[arg-type]
                if v is not None and v != 0.0:
                    return float(v)
    return float(default)


def _tonosama_should_rescue(row: dict) -> tuple[bool, str]:
    if not _env_on("TONOSAMA_RANGE5M_FILTER_RESCUE", True):
        return False, "disabled"
    if not _is_tonosama(row):
        return False, "not_tonosama"

    raw_range = _get_num(row, (
        "_intrabar_range_pct",
        "intrabar_range_pct",
        "range_pct",
        "_range_pct",
    ), 0.0)
    surge = _get_num(row, (
        "_max_volume_surge_ratio",
        "max_volume_surge_ratio",
        "volume_surge_ratio",
        "surge_ratio",
        "volume_speed",
    ), 0.0)
    vol = _get_num(row, (
        "_latest_volume",
        "latest_volume",
        "volume",
        "trading_volume",
    ), 0.0)
    score = max(abs(_get_num(row, ("_tonosama_score", "tonosama_score", "score", "final_score"), 0.0)), 0.0)
    close = _get_num(row, ("close", "price", "current_price", "close_price"), 0.0)

    min_range = _env_float("TONOSAMA_RANGE5M_RESCUE_MIN_INTRABAR_RANGE_PCT", 3.0)
    min_surge = _env_float("TONOSAMA_RANGE5M_RESCUE_MIN_SURGE", 3.0)
    min_vol = _env_float("TONOSAMA_RANGE5M_RESCUE_MIN_VOLUME", 50000.0)
    min_score = _env_float("TONOSAMA_RANGE5M_RESCUE_MIN_SCORE", 2.0)

    if raw_range >= min_range and surge >= min_surge and vol >= min_vol and score >= min_score:
        return True, (
            f"raw_range={raw_range:.3f}>={min_range:.3f} surge={surge:.2f}>={min_surge:.2f} "
            f"vol={vol:.0f}>={min_vol:.0f} score={score:.3f}>={min_score:.3f} close={close:.1f}"
        )
    return False, (
        f"weak raw_range={raw_range:.3f}/{min_range:.3f} surge={surge:.2f}/{min_surge:.2f} "
        f"vol={vol:.0f}/{min_vol:.0f} score={score:.3f}/{min_score:.3f}"
    )


def _patched_range_5m_filter(row: dict) -> bool:
    try:
        ok = bool(_ORIG_EC_RANGE(row))
        if ok:
            return True
    except Exception:
        logger.exception("[TONOSAMA RANGE5M RESCUE] original range_5m_filter failed")
        ok = False

    try:
        rescue, detail = _tonosama_should_rescue(row)
        symbol = row.get("symbol") if isinstance(row, dict) else None
        side = row.get("side") or row.get("entry_decision") if isinstance(row, dict) else None
        if rescue:
            logger.warning(
                "[TONOSAMA RANGE5M RESCUE] OK symbol=%s side=%s detail=%s",
                symbol,
                side,
                detail,
            )
            return True
        if _is_tonosama(row):
            logger.info(
                "[TONOSAMA RANGE5M RESCUE] keep NG symbol=%s side=%s detail=%s",
                symbol,
                side,
                detail,
            )
    except Exception:
        logger.debug("[TONOSAMA RANGE5M RESCUE] rescue check failed", exc_info=True)
    return bool(ok)


def install() -> bool:
    global _INSTALLED, _ORIG_EC_RANGE, _ORIG_VF_RANGE
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        import trading.filters.volatility_filter as vf

        cur_ec = getattr(ec, "range_5m_filter", None)
        cur_vf = getattr(vf, "range_5m_filter", None)
        if not callable(cur_ec):
            logger.warning("[TONOSAMA RANGE5M RESCUE] target missing ec.range_5m_filter")
            return False
        if getattr(cur_ec, "_tonosama_range5m_rescue_v1", False):
            _INSTALLED = True
            return True

        _ORIG_EC_RANGE = getattr(cur_ec, "_original", cur_ec)
        _ORIG_VF_RANGE = cur_vf if callable(cur_vf) else None
        _patched_range_5m_filter._tonosama_range5m_rescue_v1 = True  # type: ignore[attr-defined]
        _patched_range_5m_filter._original = _ORIG_EC_RANGE  # type: ignore[attr-defined]
        ec.range_5m_filter = _patched_range_5m_filter
        # Also patch module-level filter for later imports. Existing entry_controller binding is the important one.
        if callable(cur_vf):
            vf.range_5m_filter = _patched_range_5m_filter
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA RANGE5M RESCUE] installed v1 enabled=%s min_range=%s min_surge=%s min_volume=%s min_score=%s",
            _env_on("TONOSAMA_RANGE5M_FILTER_RESCUE", True),
            os.getenv("TONOSAMA_RANGE5M_RESCUE_MIN_INTRABAR_RANGE_PCT", "3.0"),
            os.getenv("TONOSAMA_RANGE5M_RESCUE_MIN_SURGE", "3.0"),
            os.getenv("TONOSAMA_RANGE5M_RESCUE_MIN_VOLUME", "50000"),
            os.getenv("TONOSAMA_RANGE5M_RESCUE_MIN_SCORE", "2.0"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA RANGE5M RESCUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA RANGE5M RESCUE] auto install failed")


__all__ = ["install"]
