# ============================================================
# File   : core/startup/tonosama_alert_liquidity_guard_patch.py
# Version: v1-TONOSAMA-ALERT-FINAL-LIQUIDITY-GUARD
# ------------------------------------------------------------
# Purpose:
#   Prevent low-volume / low-turnover TONOSAMA alerts from being
#   registered or sent to Discord.  This is intentionally placed
#   at the final pending-registration layer, because earlier
#   TONOSAMA filters may fail-open when short history is missing.
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False


def _env_bool(name: str, default: bool) -> bool:
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


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        s = str(v).strip()
        if s == "" or s.lower() in {"none", "nan", "nat"}:
            return float(default)
        x = float(s.replace(",", ""))
        if x != x:  # NaN
            return float(default)
        return x
    except Exception:
        return float(default)


def _norm_source(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_tonosama(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    return _norm_source(entry.get("source") or entry.get("entry_type")) == "TONOSAMA" or _norm_source(entry.get("entry_type")) == "TONOSAMA"


def _max_num(*values: Any) -> float:
    m = 0.0
    for v in values:
        try:
            m = max(m, _sf(v, 0.0))
        except Exception:
            pass
    return float(m)


def _should_reject(entry: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    """Return (reject, reason, detail)."""
    if not _env_bool("TONOSAMA_ALERT_LIQ_GUARD", True):
        return False, "disabled", {}
    if not _is_tonosama(entry):
        return False, "not_tonosama", {}

    cond = entry.get("entry_conditions") or {}
    if not isinstance(cond, dict):
        cond = {}

    min_latest_volume = _env_float("TONOSAMA_ALERT_MIN_LATEST_VOLUME", _env_float("ENTRY_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("TONOSAMA_ALERT_MIN_TURNOVER", _env_float("ENTRY_MIN_TURNOVER", 10000000.0))
    min_recent_bar_volume = _env_float("TONOSAMA_ALERT_MIN_RECENT_BAR_VOLUME", min_latest_volume)
    reject_missing = _env_bool("TONOSAMA_ALERT_REJECT_VOLUME_MISSING", True)

    price = _max_num(entry.get("price"), cond.get("close"), cond.get("close_price"), cond.get("price"))
    latest_volume = _max_num(
        entry.get("volume"),
        entry.get("latest_volume"),
        cond.get("latest_volume"),
        cond.get("volume"),
        cond.get("_latest_volume"),
    )
    volume_3m = _max_num(cond.get("volume_3m"), entry.get("volume_3m"))
    volume_5m = _max_num(cond.get("volume_5m"), entry.get("volume_5m"))
    recent_bar_volume = max(latest_volume, volume_3m, volume_5m)
    turnover = _max_num(
        entry.get("turnover"),
        entry.get("trading_value"),
        cond.get("turnover"),
        cond.get("turnover_raw"),
        cond.get("trading_value"),
        cond.get("売買代金"),
    )
    if turnover <= 0 and price > 0 and recent_bar_volume > 0:
        turnover = price * recent_bar_volume

    detail = {
        "symbol": entry.get("symbol"),
        "side": entry.get("side"),
        "price": price,
        "latest_volume": latest_volume,
        "volume_3m": volume_3m,
        "volume_5m": volume_5m,
        "recent_bar_volume": recent_bar_volume,
        "turnover": turnover,
        "min_latest_volume": min_latest_volume,
        "min_recent_bar_volume": min_recent_bar_volume,
        "min_turnover": min_turnover,
    }

    if reject_missing and latest_volume <= 0 and volume_3m <= 0 and volume_5m <= 0:
        return True, "alert_volume_missing", detail
    if latest_volume < min_latest_volume:
        return True, "alert_latest_volume_low", detail
    if recent_bar_volume < min_recent_bar_volume:
        return True, "alert_recent_bar_volume_low", detail
    if turnover < min_turnover:
        return True, "alert_turnover_low", detail
    return False, "ok", detail


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    if not _env_bool("TONOSAMA_ALERT_LIQ_GUARD", True):
        logger.warning("[TONOSAMA ALERT LIQ GUARD] disabled env=TONOSAMA_ALERT_LIQ_GUARD")
        _PATCHED = True
        return True

    try:
        import trading.entry.tonosama.pending_writer as pw
    except Exception:
        logger.exception("[TONOSAMA ALERT LIQ GUARD] pending_writer import failed")
        return False

    old_add = getattr(pw, "add_tonosama_pending", None)
    if not callable(old_add):
        logger.warning("[TONOSAMA ALERT LIQ GUARD] add_tonosama_pending not callable")
        return False
    if getattr(old_add, "_tonosama_alert_liq_guard", False):
        _PATCHED = True
        return True

    def _guarded_add(entry: dict[str, Any]) -> bool:
        reject, reason, detail = _should_reject(entry)
        if reject:
            logger.warning(
                "[TONOSAMA ALERT LIQ GUARD] reject symbol=%s side=%s reason=%s latest_volume=%.0f volume_3m=%.0f volume_5m=%.0f turnover=%.0f min_latest=%.0f min_recent=%.0f min_turnover=%.0f price=%.1f",
                detail.get("symbol"),
                detail.get("side"),
                reason,
                _sf(detail.get("latest_volume"), 0.0),
                _sf(detail.get("volume_3m"), 0.0),
                _sf(detail.get("volume_5m"), 0.0),
                _sf(detail.get("turnover"), 0.0),
                _sf(detail.get("min_latest_volume"), 0.0),
                _sf(detail.get("min_recent_bar_volume"), 0.0),
                _sf(detail.get("min_turnover"), 0.0),
                _sf(detail.get("price"), 0.0),
            )
            return False
        return bool(old_add(entry))

    _guarded_add._tonosama_alert_liq_guard = True  # type: ignore[attr-defined]
    _guarded_add._original_add_tonosama_pending = old_add  # type: ignore[attr-defined]
    pw.add_tonosama_pending = _guarded_add

    # runner.py imports add_tonosama_pending directly, so patch that binding too
    # when the runner module is already imported.
    try:
        import trading.entry.tonosama.runner as runner
        setattr(runner, "add_tonosama_pending", _guarded_add)
    except Exception:
        logger.debug("[TONOSAMA ALERT LIQ GUARD] runner binding patch skipped", exc_info=True)

    _PATCHED = True
    logger.warning(
        "[TONOSAMA ALERT LIQ GUARD] installed v1 min_latest_volume=%.0f min_recent_bar_volume=%.0f min_turnover=%.0f reject_missing=%s",
        _env_float("TONOSAMA_ALERT_MIN_LATEST_VOLUME", _env_float("ENTRY_MIN_VOLUME", 30000.0)),
        _env_float("TONOSAMA_ALERT_MIN_RECENT_BAR_VOLUME", _env_float("TONOSAMA_ALERT_MIN_LATEST_VOLUME", _env_float("ENTRY_MIN_VOLUME", 30000.0))),
        _env_float("TONOSAMA_ALERT_MIN_TURNOVER", _env_float("ENTRY_MIN_TURNOVER", 10000000.0)),
        _env_bool("TONOSAMA_ALERT_REJECT_VOLUME_MISSING", True),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[TONOSAMA ALERT LIQ GUARD] auto install failed")
