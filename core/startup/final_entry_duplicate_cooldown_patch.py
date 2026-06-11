from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_EXECUTE = None
_LAST_ATTEMPT_TS: dict[tuple[str, str], float] = {}
_INFLIGHT: set[tuple[str, str]] = set()
_VERSION = "v1"


def _env_on(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        v = os.getenv(name)
        x = float(default) if v is None or str(v).strip() == "" else float(v)
        if min_value is not None:
            x = max(float(min_value), x)
        if max_value is not None:
            x = min(float(max_value), x)
        return float(x)
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _extract_symbol_side(item: Any) -> tuple[str, str]:
    try:
        if not isinstance(item, dict):
            return "", ""
        row = _row_to_dict(item.get("entry_row"))
        symbol = _norm_symbol(item.get("symbol") or row.get("symbol") or row.get("Symbol") or row.get("code"))
        side = _norm_side(item.get("side") or row.get("side") or row.get("entry_decision") or row.get("ai_side"))
        return symbol, side
    except Exception:
        return "", ""


def _patched_execute_best_candidate(item: dict, boost_active: bool) -> bool:
    if not callable(_ORIG_EXECUTE):
        logger.error("[FINAL ENTRY DUP COOLDOWN] original execute unavailable")
        return False
    if not _env_on("ENTRY_FINAL_DUPLICATE_COOLDOWN_ENABLED", True):
        return bool(_ORIG_EXECUTE(item, boost_active))

    symbol, side = _extract_symbol_side(item)
    if not symbol or side not in {"BUY", "SELL"}:
        return bool(_ORIG_EXECUTE(item, boost_active))

    key = (symbol, side)
    now = time.time()
    cooldown = _env_float("ENTRY_FINAL_DUPLICATE_COOLDOWN_SEC", 45.0, min_value=0.0, max_value=300.0)

    if key in _INFLIGHT:
        logger.warning("[FINAL ENTRY DUP COOLDOWN] skip inflight symbol=%s side=%s", symbol, side)
        return False

    last = _LAST_ATTEMPT_TS.get(key, 0.0)
    if cooldown > 0 and last > 0 and (now - last) < cooldown:
        logger.warning(
            "[FINAL ENTRY DUP COOLDOWN] skip cooldown symbol=%s side=%s elapsed=%.1fs cooldown=%.1fs",
            symbol,
            side,
            now - last,
            cooldown,
        )
        return False

    _LAST_ATTEMPT_TS[key] = now
    _INFLIGHT.add(key)
    try:
        ok = bool(_ORIG_EXECUTE(item, boost_active))
        logger.warning("[FINAL ENTRY DUP COOLDOWN] orig done symbol=%s side=%s ok=%s", symbol, side, ok)
        return ok
    finally:
        _INFLIGHT.discard(key)


def _is_wrapped() -> bool:
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_execute_best_candidate", None)
        return bool(getattr(cur, "_final_entry_duplicate_cooldown_v1", False))
    except Exception:
        return False


def _wrap_once() -> bool:
    global _INSTALLED, _ORIG_EXECUTE
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_execute_best_candidate", None)
        if not callable(cur):
            return False
        if getattr(cur, "_final_entry_duplicate_cooldown_v1", False):
            _INSTALLED = True
            return True
        _ORIG_EXECUTE = cur
        _patched_execute_best_candidate._final_entry_duplicate_cooldown_v1 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._original_execute_best_candidate = cur  # type: ignore[attr-defined]
        ec._execute_best_candidate = _patched_execute_best_candidate
        _INSTALLED = True
        logger.warning(
            "[FINAL ENTRY DUP COOLDOWN] installed %s cooldown=%.1fs orig=%s",
            _VERSION,
            _env_float("ENTRY_FINAL_DUPLICATE_COOLDOWN_SEC", 45.0),
            getattr(cur, "__name__", repr(cur)),
        )
        return True
    except Exception:
        logger.exception("[FINAL ENTRY DUP COOLDOWN] wrap failed")
        return False


def _watcher() -> None:
    # Other startup patches can replace _execute_best_candidate after this patch.
    # Re-enforce for a short window during startup.
    deadline = time.time() + _env_float("ENTRY_FINAL_DUPLICATE_COOLDOWN_WATCH_SEC", 90.0, min_value=5.0, max_value=300.0)
    while time.time() < deadline:
        try:
            if not _is_wrapped():
                _wrap_once()
        except Exception:
            logger.exception("[FINAL ENTRY DUP COOLDOWN] watcher enforce failed")
        time.sleep(2.0)


def install() -> bool:
    if not _env_on("ENTRY_FINAL_DUPLICATE_COOLDOWN_ENABLED", True):
        logger.warning("[FINAL ENTRY DUP COOLDOWN] disabled by env")
        return False
    try:
        ok = _wrap_once()
        threading.Thread(target=_watcher, name="final-entry-dup-cooldown-watch", daemon=True).start()
        return bool(ok)
    except Exception:
        logger.exception("[FINAL ENTRY DUP COOLDOWN] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[FINAL ENTRY DUP COOLDOWN] auto install failed")


__all__ = ["install"]
