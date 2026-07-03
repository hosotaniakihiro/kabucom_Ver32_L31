# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

VERSION = "V1-SUMMARY-AI-URGENT-PUSH-REGISTRATION"
_INSTALLED = False
_WATCHER_STARTED = False
_ORIGINAL_BUILD_ENTRY_ORDER = None
_ORIGINAL_RESOLVE_MONITOR = None
_ORIGINAL_RESOLVE_TARGETS = None

_LOCK = threading.RLock()
_URGENT: dict[str, float] = {}

_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_SUMMARY_SOURCE_SET = {"SUMMARY", "SUMMARY_AI", "PUSH", "PUSH_SUMMARY", "STOCK_SUMMARY"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in _TRUE_SET
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(str(raw).replace(",", "")))
    except Exception:
        return int(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".T"):
            s = s[:-2]
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        if not s or s in {"NONE", "NULL", "NAN", "NA", "-", "0"}:
            return ""
        if not s.isalnum() or not (3 <= len(s) <= 5):
            return ""
        return s
    except Exception:
        return ""


def _dedupe(items: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        sym = _norm_symbol(item)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def _ttl_sec() -> float:
    # A/B rotation is about 10s. Keep a little longer so the next rotation pass can pick it up.
    return max(3.0, _env_float("SUMMARY_AI_URGENT_PUSH_TTL_SEC", 18.0))


def _max_urgent() -> int:
    return max(1, _env_int("SUMMARY_AI_URGENT_PUSH_MAX_SYMBOLS", 15))


def _now() -> float:
    return time.monotonic()


def _prune_locked(now: float | None = None) -> None:
    t = _now() if now is None else float(now)
    expired = [sym for sym, until in _URGENT.items() if until <= t]
    for sym in expired:
        _URGENT.pop(sym, None)


def mark_urgent_symbol(symbol: Any, *, reason: str = "summary_ai", ttl_sec: float | None = None) -> bool:
    sym = _norm_symbol(symbol)
    if not sym:
        return False
    if not _env_bool("SUMMARY_AI_URGENT_PUSH_ENABLED", True):
        return False
    ttl = _ttl_sec() if ttl_sec is None else max(1.0, float(ttl_sec))
    with _LOCK:
        t = _now()
        _prune_locked(t)
        _URGENT[sym] = t + ttl
        # Keep the newest urgent symbols if a burst exceeds the reserved fixed frame.
        max_items = _max_urgent()
        if len(_URGENT) > max_items:
            oldest = sorted(_URGENT.items(), key=lambda kv: kv[1])[: max(0, len(_URGENT) - max_items)]
            for old_sym, _ in oldest:
                _URGENT.pop(old_sym, None)
        logger.warning(
            "[SUMMARY AI URGENT PUSH] marked symbol=%s ttl=%.1fs reason=%s active=%s version=%s",
            sym, ttl, reason, list(_URGENT.keys()), VERSION,
        )
    return True


def get_urgent_symbols() -> list[str]:
    if not _env_bool("SUMMARY_AI_URGENT_PUSH_ENABLED", True):
        return []
    with _LOCK:
        _prune_locked()
        # Later expiry means more recent / still hot; keep newest first.
        return [sym for sym, _until in sorted(_URGENT.items(), key=lambda kv: kv[1], reverse=True)[:_max_urgent()]]


def clear_urgent_symbol(symbol: Any) -> bool:
    sym = _norm_symbol(symbol)
    if not sym:
        return False
    with _LOCK:
        existed = sym in _URGENT
        _URGENT.pop(sym, None)
    return existed


def _is_summary_ai_order(kwargs: dict[str, Any]) -> bool:
    try:
        source = str(kwargs.get("source") or "").strip().upper()
        row = kwargs.get("entry_row") if isinstance(kwargs.get("entry_row"), dict) else {}
        row_source = str(row.get("source") or "").strip().upper()
        entry_type = str(row.get("entry_type") or kwargs.get("entry_type") or "").strip().upper()
        pipeline_source = str(row.get("pipeline_source") or "").strip().upper()
        joined = "|".join(
            str(x or "").upper()
            for x in (source, row_source, entry_type, pipeline_source, row.get("reason"), row.get("ai_reason"), row.get("skip_reason"))
        )
        if source in _SUMMARY_SOURCE_SET or row_source in _SUMMARY_SOURCE_SET or pipeline_source == "SUMMARY":
            return True
        if "SUMMARY_AI" in joined or "SRC=SUMMARY" in joined:
            return True
        if bool(row.get("ai_gate_allow") or row.get("preapproved") or row.get("summary_ai_ok")):
            return True
    except Exception:
        pass
    return False


def _pick_symbol_from_order(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    for key in ("symbol", "Symbol", "銘柄コード", "code", "stock_code"):
        sym = _norm_symbol(kwargs.get(key))
        if sym:
            return sym
    row = kwargs.get("entry_row") if isinstance(kwargs.get("entry_row"), dict) else None
    if isinstance(row, dict):
        for key in ("symbol", "Symbol", "銘柄コード", "code", "stock_code"):
            sym = _norm_symbol(row.get(key))
            if sym:
                return sym
    for arg in args:
        if isinstance(arg, dict):
            for key in ("symbol", "Symbol", "銘柄コード", "code", "stock_code"):
                sym = _norm_symbol(arg.get(key))
                if sym:
                    return sym
    return ""


def merge_urgent_first(symbols: Sequence[Any] | None, *, max_symbols: int | None = None, reason: str = "resolve") -> list[str]:
    base = _dedupe(symbols or [])
    urgent = get_urgent_symbols()
    if not urgent:
        return base[:max_symbols] if max_symbols else base
    limit = max_symbols or len(base) or _env_int("PUSH_REGISTER_MAX_KEEP", 100)
    limit = max(1, int(limit))
    merged = _dedupe([*urgent, *base])[:limit]
    logger.warning(
        "[SUMMARY AI URGENT PUSH] merge reason=%s urgent=%s before=%s after=%s head=%s version=%s",
        reason, urgent, len(base), len(merged), merged[:20], VERSION,
    )
    return merged


def _patch_entry_order_builder() -> bool:
    global _ORIGINAL_BUILD_ENTRY_ORDER
    try:
        from trading.handlers import entry_order_builder as eob
    except Exception:
        logger.debug("[SUMMARY AI URGENT PUSH] entry_order_builder import failed", exc_info=True)
        return False

    cur = getattr(eob, "build_entry_order", None)
    if not callable(cur):
        return False
    if getattr(cur, "_summary_ai_urgent_push_v1", False):
        return True

    original = cur
    _ORIGINAL_BUILD_ENTRY_ORDER = original

    def _patched_build_entry_order(*args, **kwargs):
        try:
            if _is_summary_ai_order(kwargs):
                sym = _pick_symbol_from_order(args, kwargs)
                if sym:
                    mark_urgent_symbol(sym, reason="before_build_entry_order")
        except Exception:
            logger.debug("[SUMMARY AI URGENT PUSH] mark before build failed", exc_info=True)
        return original(*args, **kwargs)

    _patched_build_entry_order._summary_ai_urgent_push_v1 = True  # type: ignore[attr-defined]
    _patched_build_entry_order._original = getattr(original, "_original", original)  # type: ignore[attr-defined]
    eob.build_entry_order = _patched_build_entry_order
    try:
        import trading.handlers.entry_controller as ec
        ec.build_entry_order = _patched_build_entry_order
    except Exception:
        logger.debug("[SUMMARY AI URGENT PUSH] entry_controller alias patch skipped", exc_info=True)
    logger.warning("[SUMMARY AI URGENT PUSH] entry_order_builder wrapped version=%s", VERSION)
    return True


def _patch_rotation_symbols() -> bool:
    global _ORIGINAL_RESOLVE_MONITOR, _ORIGINAL_RESOLVE_TARGETS
    try:
        from trading.push.push_stream import rotation_symbols as rs
    except Exception:
        logger.debug("[SUMMARY AI URGENT PUSH] rotation_symbols import failed", exc_info=True)
        return False

    ok = False
    max_keep = _env_int("PUSH_REGISTER_MAX_KEEP", getattr(rs, "DEFAULT_REGISTER_MAX_SYMBOLS", 100))

    cur_monitor = getattr(rs, "resolve_monitor_symbols", None)
    if callable(cur_monitor) and not getattr(cur_monitor, "_summary_ai_urgent_push_v1", False):
        _ORIGINAL_RESOLVE_MONITOR = cur_monitor

        def _patched_resolve_monitor_symbols():
            base = _ORIGINAL_RESOLVE_MONITOR()
            return merge_urgent_first(base, max_symbols=max_keep, reason="resolve_monitor_symbols")

        _patched_resolve_monitor_symbols._summary_ai_urgent_push_v1 = True  # type: ignore[attr-defined]
        rs.resolve_monitor_symbols = _patched_resolve_monitor_symbols
        ok = True

    cur_targets = getattr(rs, "resolve_register_targets", None)
    if callable(cur_targets) and not getattr(cur_targets, "_summary_ai_urgent_push_v1", False):
        _ORIGINAL_RESOLVE_TARGETS = cur_targets

        def _patched_resolve_register_targets():
            base = _ORIGINAL_RESOLVE_TARGETS()
            return merge_urgent_first(base, max_symbols=max_keep, reason="resolve_register_targets")

        _patched_resolve_register_targets._summary_ai_urgent_push_v1 = True  # type: ignore[attr-defined]
        rs.resolve_register_targets = _patched_resolve_register_targets
        ok = True

    if ok:
        logger.warning("[SUMMARY AI URGENT PUSH] rotation_symbols wrapped max_keep=%s version=%s", max_keep, VERSION)
    return ok or bool(getattr(getattr(rs, "resolve_register_targets", None), "_summary_ai_urgent_push_v1", False))


def _watcher_loop() -> None:
    loops = max(1, _env_int("SUMMARY_AI_URGENT_PUSH_WATCHER_LOOPS", 120))
    sleep_sec = max(0.2, _env_float("SUMMARY_AI_URGENT_PUSH_WATCHER_SLEEP_SEC", 0.5))
    for i in range(loops):
        try:
            _patch_entry_order_builder()
            _patch_rotation_symbols()
            if i % 20 == 0:
                logger.warning("[SUMMARY AI URGENT PUSH] watcher enforce i=%s urgent=%s version=%s", i, get_urgent_symbols(), VERSION)
        except Exception:
            logger.debug("[SUMMARY AI URGENT PUSH] watcher enforce failed", exc_info=True)
        time.sleep(sleep_sec)


def _start_watcher() -> bool:
    global _WATCHER_STARTED
    if _WATCHER_STARTED:
        return True
    if not _env_bool("SUMMARY_AI_URGENT_PUSH_WATCHER", True):
        return False
    _WATCHER_STARTED = True
    try:
        threading.Thread(target=_watcher_loop, name="summary-ai-urgent-push-watch", daemon=True).start()
        return True
    except Exception:
        logger.debug("[SUMMARY AI URGENT PUSH] watcher start failed", exc_info=True)
        return False


def install() -> bool:
    global _INSTALLED
    if not _env_bool("SUMMARY_AI_URGENT_PUSH_ENABLED", True):
        logger.warning("[SUMMARY AI URGENT PUSH] disabled by env version=%s", VERSION)
        return False
    # Re-run is allowed because other startup patches can replace wrappers after us.
    try:
        eob_ok = _patch_entry_order_builder()
        rot_ok = _patch_rotation_symbols()
        watcher_ok = _start_watcher()
        _INSTALLED = bool(eob_ok or rot_ok or watcher_ok)
        logger.warning(
            "[SUMMARY AI URGENT PUSH] installed eob=%s rotation=%s watcher=%s ttl=%.1fs max=%s version=%s",
            eob_ok, rot_ok, watcher_ok, _ttl_sec(), _max_urgent(), VERSION,
        )
        return _INSTALLED
    except Exception:
        logger.exception("[SUMMARY AI URGENT PUSH] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI URGENT PUSH] auto install failed")


__all__ = [
    "VERSION",
    "install",
    "mark_urgent_symbol",
    "get_urgent_symbols",
    "clear_urgent_symbol",
    "merge_urgent_first",
]
