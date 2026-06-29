# ============================================================
# File   : core/startup/entry_execute_timeout_guard_patch.py
# Version: V2-ENTRY-EXECUTE-TIMEOUT-AND-SUMMARY-AI-AGE-GUARD
# ------------------------------------------------------------
# SUMMARY_AI は足 datetime ではなく created_at / updated_at を優先して stale 判定する。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import queue
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)
_PATCHED = False
_WATCHER_STARTED = False
_ORIGINALS: dict[int, Callable[..., Any]] = {}
_INFLIGHT: dict[tuple[str, str], dict[str, Any]] = {}
_INFLIGHT_LOCK = threading.RLock()
_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_SET = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE_SET:
            return True
        if s in _FALSE_SET:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
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


def _row_to_dict(row: Any) -> dict[str, Any]:
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


def _parse_dt(v: Any) -> dt.datetime | None:
    try:
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)
        if v is None or str(v).strip() == "":
            return None
        if isinstance(v, (int, float)):
            fv = float(v)
            if fv > 1_000_000_000:
                return dt.datetime.fromtimestamp(fv)
            return None
        s = str(v).strip()
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return dt.datetime.strptime(s, fmt)
            except Exception:
                continue
    except Exception:
        return None
    return None


def _first_dt(*values: Any) -> dt.datetime | None:
    for v in values:
        ts = _parse_dt(v)
        if ts is not None:
            return ts
    return None


def _is_summary_ai_candidate(item: dict[str, Any], row: dict[str, Any]) -> bool:
    try:
        ai = item.get("ai") if isinstance(item.get("ai"), dict) else {}
        source = str(item.get("source") or row.get("source") or ai.get("source") or "").upper()
        entry_type = str(item.get("entry_type") or row.get("entry_type") or ai.get("entry_type") or "").upper()
        side = _norm_side(item.get("side") or row.get("side") or row.get("entry_decision") or row.get("ai_side") or ai.get("side") or ai.get("entry_decision") or ai.get("ai_side"))
        return source == "SUMMARY" or entry_type == "SUMMARY_AI" or side in {"BUY", "SELL"} or bool(item.get("ai_gate_allow") or row.get("ai_gate_allow") or ai.get("ai_gate_allow") or item.get("preapproved") or row.get("preapproved") or ai.get("preapproved"))
    except Exception:
        return False


def _item_age_sec(item: dict[str, Any], row: dict[str, Any]) -> tuple[float | None, str]:
    ai = item.get("ai") if isinstance(item.get("ai"), dict) else {}
    now = dt.datetime.now()
    if _is_summary_ai_candidate(item, row):
        ts = _first_dt(item.get("updated_at"), row.get("updated_at"), ai.get("updated_at"), item.get("created_at"), row.get("created_at"), item.get("pending_created_at"), ai.get("created_at"), item.get("queued_at"), row.get("queued_at"), ai.get("queued_at"))
        if ts is not None:
            return max(0.0, (now - ts).total_seconds()), "created_at"
        return 0.0, "summary_ai_now_fallback"
    ts = _first_dt(item.get("created_at"), item.get("pending_created_at"), ai.get("created_at"))
    if ts is not None:
        return max(0.0, (now - ts).total_seconds()), "created_at"
    ts = _first_dt(item.get("datetime"), row.get("datetime"), item.get("entry_time"), row.get("entry_time"))
    if ts is not None:
        return max(0.0, (now - ts).total_seconds()), "datetime"
    return None, "missing"


def _extract_symbol_side(item: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    row = _row_to_dict(item.get("entry_row") or item.get("entry"))
    ai = item.get("ai") if isinstance(item.get("ai"), dict) else {}
    symbol = _norm_symbol(item.get("symbol") or row.get("symbol") or ai.get("symbol"))
    side = _norm_side(item.get("side") or row.get("side") or row.get("entry_decision") or row.get("ai_side") or ai.get("side") or ai.get("entry_decision") or ai.get("ai_side"))
    return symbol, side, row


def _prune_pending_for_symbol(symbol: str, side: str, reason: str) -> int:
    try:
        from trading.entry import pending_manager
        side_u = _norm_side(side)
        def pred(sym: str, entry: dict[str, Any]) -> bool:
            if _norm_symbol(sym) != symbol:
                return False
            e_side = _norm_side(entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"))
            return not e_side or e_side == side_u
        return int(pending_manager.prune_entries(pred, reason=reason))
    except Exception:
        logger.exception("[ENTRY EXEC TIMEOUT GUARD] pending prune failed symbol=%s side=%s reason=%s", symbol, side, reason)
        return 0


def _candidate_stale_guard(item: dict[str, Any], row: dict[str, Any], symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_EXECUTE_STALE_CANDIDATE_GUARD_ENABLED", True):
        return True
    age, source = _item_age_sec(item, row)
    if age is None:
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] AGE_UNKNOWN_ALLOW symbol=%s side=%s", symbol, side)
        return True
    max_age = _env_float("ENTRY_EXECUTE_MAX_CANDIDATE_AGE_SEC", 90.0)
    if source == "datetime":
        max_age = _env_float("ENTRY_EXECUTE_MAX_BAR_AGE_SEC", 180.0)
    if source == "summary_ai_now_fallback":
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] SUMMARY_AI_AGE_FALLBACK_ALLOW symbol=%s side=%s source=%s", symbol, side, source)
        return True
    if age <= max_age:
        return True
    removed = _prune_pending_for_symbol(symbol, side, "execute_candidate_stale")
    logger.warning("[ENTRY EXEC TIMEOUT GUARD] STALE_CANDIDATE_SKIP symbol=%s side=%s age=%.1fs max_age=%.1fs source=%s pruned=%s", symbol, side, age, max_age, source, removed)
    return False


def _cleanup_inflight() -> None:
    now = time.time()
    ttl = _env_float("ENTRY_EXECUTE_INFLIGHT_TTL_SEC", 120.0)
    with _INFLIGHT_LOCK:
        for key, info in list(_INFLIGHT.items()):
            started = float(info.get("started", now))
            if bool(info.get("done")) or now - started > ttl:
                _INFLIGHT.pop(key, None)


def _call_with_timeout(orig: Callable[..., Any], item: dict[str, Any], boost_active: bool, symbol: str, side: str) -> bool:
    timeout = max(0.5, _env_float("ENTRY_EXECUTE_ORIG_TIMEOUT_SEC", 8.0))
    q: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    key = (symbol, side)
    def runner() -> None:
        try:
            q.put_nowait(("ok", bool(orig(item, boost_active))))
        except Exception as exc:
            try:
                q.put_nowait(("err", exc))
            except Exception:
                pass
        finally:
            with _INFLIGHT_LOCK:
                info = _INFLIGHT.get(key)
                if isinstance(info, dict):
                    info["done"] = True
                    info["finished"] = time.time()
    with _INFLIGHT_LOCK:
        _cleanup_inflight()
        existing = _INFLIGHT.get(key)
        if isinstance(existing, dict) and not bool(existing.get("done")):
            age = time.time() - float(existing.get("started", time.time()))
            logger.warning("[ENTRY EXEC TIMEOUT GUARD] INFLIGHT_DUPLICATE_SKIP symbol=%s side=%s age=%.1fs timeout=%.1fs", symbol, side, age, timeout)
            return False
        _INFLIGHT[key] = {"started": time.time(), "done": False, "thread": None}
    th = threading.Thread(target=runner, name=f"entry-execute-timeout-{symbol}-{side}", daemon=True)
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT:
            _INFLIGHT[key]["thread"] = th.name
    th.start()
    th.join(timeout)
    if th.is_alive():
        removed = _prune_pending_for_symbol(symbol, side, "execute_orig_timeout")
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] ORIG_TIMEOUT_RETURN_FALSE symbol=%s side=%s timeout=%.1fs pruned=%s thread=%s", symbol, side, timeout, removed, th.name)
        return False
    try:
        status, value = q.get_nowait()
    except Exception:
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] ORIG_EMPTY_RESULT symbol=%s side=%s", symbol, side)
        return False
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(key, None)
    if status == "err":
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] ORIG_ERROR_RETURN_FALSE symbol=%s side=%s error=%r", symbol, side, value)
        return False
    return bool(value)


def _wrap_current() -> bool:
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_execute_best_candidate", None)
        if not callable(cur):
            return False
        if getattr(cur, "_entry_execute_timeout_guard_v2", False):
            return True
        orig = getattr(cur, "_entry_execute_timeout_guard_original", None) or cur
        _ORIGINALS[id(orig)] = orig
        def wrapped(item: dict, boost_active: bool) -> bool:
            try:
                if not isinstance(item, dict):
                    logger.warning("[ENTRY EXEC TIMEOUT GUARD] INVALID_ITEM type=%s", type(item))
                    return False
                symbol, side, row = _extract_symbol_side(item)
                if not symbol or side not in {"BUY", "SELL"}:
                    logger.warning("[ENTRY EXEC TIMEOUT GUARD] INVALID_SYMBOL_SIDE symbol=%s side=%s keys=%s", symbol, side, list(item.keys()))
                    return False
                if not _candidate_stale_guard(item, row, symbol, side):
                    return False
                started = time.time()
                ok = _call_with_timeout(orig, item, boost_active, symbol, side)
                logger.warning("[ENTRY EXEC TIMEOUT GUARD] DONE symbol=%s side=%s ok=%s elapsed=%.3fs", symbol, side, ok, time.time() - started)
                return bool(ok)
            except Exception:
                logger.exception("[ENTRY EXEC TIMEOUT GUARD] wrapper failed")
                return False
        wrapped._entry_execute_timeout_guard_v2 = True  # type: ignore[attr-defined]
        wrapped._entry_execute_timeout_guard_v1 = True  # type: ignore[attr-defined]
        wrapped._entry_execute_timeout_guard_original = orig  # type: ignore[attr-defined]
        ec._execute_best_candidate = wrapped
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] wrapped target=%s timeout=%.1fs stale_created=%.1fs stale_bar=%.1fs summary_ai_age_fix=True", getattr(orig, "__name__", repr(orig)), _env_float("ENTRY_EXECUTE_ORIG_TIMEOUT_SEC", 8.0), _env_float("ENTRY_EXECUTE_MAX_CANDIDATE_AGE_SEC", 90.0), _env_float("ENTRY_EXECUTE_MAX_BAR_AGE_SEC", 180.0))
        return True
    except Exception:
        logger.exception("[ENTRY EXEC TIMEOUT GUARD] wrap failed")
        return False


def _watcher_loop() -> None:
    interval = max(1.0, _env_float("ENTRY_EXECUTE_TIMEOUT_WATCHER_INTERVAL_SEC", 2.0))
    while True:
        try:
            if not _env_bool("ENTRY_EXECUTE_TIMEOUT_GUARD_ENABLED", True):
                return
            _wrap_current()
            _cleanup_inflight()
        except Exception:
            logger.exception("[ENTRY EXEC TIMEOUT GUARD] watcher error")
        time.sleep(interval)


def install() -> bool:
    global _PATCHED, _WATCHER_STARTED
    if not _env_bool("ENTRY_EXECUTE_TIMEOUT_GUARD_ENABLED", True):
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] disabled by env")
        return False
    ok = _wrap_current()
    _PATCHED = bool(ok)
    if not _WATCHER_STARTED and _env_bool("ENTRY_EXECUTE_TIMEOUT_WATCHER_ENABLED", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher_loop, name="entry-execute-timeout-guard-watcher", daemon=True).start()
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] watcher started")
    logger.warning("[ENTRY EXEC TIMEOUT GUARD] installed V2 ok=%s", ok)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[ENTRY EXEC TIMEOUT GUARD] auto install failed")

__all__ = ["install"]
