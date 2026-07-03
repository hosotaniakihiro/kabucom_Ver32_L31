# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_runtime_consistency_patch.py
# Version: V6-BREAK-ASYNC-DIRECT-RECURSION
# ------------------------------------------------------------
# Purpose:
#   1) Summary-AI safety guard must not block a fresh direct 1m df just because
#      an older global_context summary_history remains cached.
#   2) Runtime final-board compatibility patches must not replace executor
#      selection with a pool that collapses AI_OK rows before rolling retry.
#   3) Ensure blowoff protection is installed without repeatedly re-wrapping or
#      logging every watcher tick.
#   4) Ensure SUMMARY_AI fast order builder is loaded from sitecustomize path,
#      even when usercustomize.py is not imported by this Python environment.
#   5) Break async_entry <-> direct_dispatch _original cycles.
#
# This does not relax low-move / blowoff / liquidity / board guards. It only
# keeps the runtime hook chain consistent and idempotent.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from functools import wraps
from typing import Any, Sequence

logger = logging.getLogger(__name__)
VERSION = "V6-BREAK-ASYNC-DIRECT-RECURSION"
_INSTALLED = False
_WATCHER_STARTED = False
_TLS = threading.local()
_LAST_DIRECT_DF: Any = None
_LAST_DIRECT_AT: float = 0.0
_BLOWOFF_MARKERS = tuple(f"_summary_ai_blowoff_prefilter_v{i}" for i in range(1, 10))
_ASYNC_MARKERS = tuple(f"_summary_ai_async_entry_patch_v{i}" for i in range(1, 13))
_DIRECT_MARKERS = tuple(f"_summary_ai_direct_dispatch_v{i}" for i in range(1, 12))


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
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _is_df(v: Any) -> bool:
    try:
        import pandas as pd
        return isinstance(v, pd.DataFrame) and not v.empty
    except Exception:
        return False


def _extract_df(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    try:
        for k in ("summary_df", "df", "source_df", "base_df"):
            if _is_df(kwargs.get(k)):
                return kwargs.get(k)
        for x in args:
            if _is_df(x):
                return x
    except Exception:
        pass
    return None


def _latest_dt(df: Any) -> dt.datetime | None:
    try:
        import pandas as pd
        if not _is_df(df):
            return None
        for c in ("datetime", "end_time", "last_update", "updated_at", "inserted_at", "time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce")
                try:
                    s = s.dt.tz_localize(None)
                except Exception:
                    pass
                mx = s.max()
                if pd.notna(mx):
                    ts = pd.Timestamp(mx)
                    try:
                        if ts.tzinfo is not None:
                            ts = ts.tz_localize(None)
                    except Exception:
                        pass
                    return ts.to_pydatetime().replace(tzinfo=None)
    except Exception:
        pass
    return None


def _df_age(df: Any) -> tuple[bool, float | None, dt.datetime | None, int]:
    try:
        if not _is_df(df):
            return False, None, None, 0
        latest = _latest_dt(df)
        if latest is None:
            return False, None, None, len(df)
        age = (dt.datetime.now().replace(tzinfo=None) - latest).total_seconds()
        return True, age, latest, len(df)
    except Exception:
        return False, None, None, 0


def _fresh_direct_df(df: Any) -> bool:
    ok, age, _latest, _rows = _df_age(df)
    if not ok or age is None:
        return False
    max_age = _env_float("SUMMARY_AI_DIRECT_INPUT_MAX_AGE_SEC", _env_float("SUMMARY_AI_MAX_PUSH_1M_AGE_SEC", 120.0))
    return age <= max_age


def _set_direct_context(df: Any) -> tuple[Any, bool]:
    global _LAST_DIRECT_DF, _LAST_DIRECT_AT
    prev = getattr(_TLS, "direct_df", None)
    if _is_df(df):
        _TLS.direct_df = df
        _LAST_DIRECT_DF = df
        _LAST_DIRECT_AT = time.time()
        return prev, True
    return prev, False


def _restore_direct_context(prev: Any) -> None:
    try:
        _TLS.direct_df = prev
    except Exception:
        pass


def _has_any_marker(fn: Any, markers: Sequence[str]) -> bool:
    return any(bool(getattr(fn, m, False)) for m in markers)


def _iter_wrapper_chain(fn: Any, *, limit: int = 32):
    seen: set[int] = set()
    cur = fn
    for _ in range(limit):
        if not callable(cur):
            return
        ident = id(cur)
        if ident in seen:
            return
        seen.add(ident)
        yield cur
        nxt = getattr(cur, "_original", None)
        if not callable(nxt):
            return
        cur = nxt


def _chain_has_marker(fn: Any, markers: Sequence[str]) -> bool:
    return any(_has_any_marker(x, markers) for x in _iter_wrapper_chain(fn))


def _is_async_or_direct(fn: Any) -> bool:
    return _has_any_marker(fn, _ASYNC_MARKERS) or _has_any_marker(fn, _DIRECT_MARKERS)


def _find_non_async_direct_base(*roots: Any) -> Any:
    """Find a callable base that is not async_entry/direct_dispatch.

    The previous runtime could build this cycle:
      async_entry._ORIG_EXECUTE -> direct_dispatch wrapper
      direct_dispatch._ORIG      -> async_entry wrapper
    Traversing with a seen set avoids hanging while looking for a safe base.
    """
    seen: set[int] = set()
    stack = [x for x in roots if callable(x)]
    while stack:
        cur = stack.pop(0)
        if not callable(cur):
            continue
        ident = id(cur)
        if ident in seen:
            continue
        seen.add(ident)
        if not _is_async_or_direct(cur):
            return cur
        nxt = getattr(cur, "_original", None)
        if callable(nxt):
            stack.append(nxt)
    return None


def _break_async_direct_cycle() -> bool:
    """Repair async_entry/direct_dispatch wrappers if they point to each other."""
    if not _env_bool("SUMMARY_AI_BREAK_ASYNC_DIRECT_RECURSION", True):
        return False
    try:
        from trading.entry.summary_ai import executor as exec_mod
        try:
            from trading.entry.summary_ai import runner as runner_mod
        except Exception:
            runner_mod = None
        try:
            from core.startup import summary_ai_async_entry_patch as async_patch
        except Exception:
            async_patch = None
        try:
            from core.startup import summary_ai_async_direct_dispatch_patch as direct_patch
        except Exception:
            direct_patch = None

        current = getattr(exec_mod, "execute_ai_ok_entries_bulk", None)
        async_orig = getattr(async_patch, "_ORIG_EXECUTE", None) if async_patch is not None else None
        direct_orig = getattr(direct_patch, "_ORIG", None) if direct_patch is not None else None
        base = _find_non_async_direct_base(current, async_orig, direct_orig)
        if not callable(base):
            return False

        changed = False
        if async_patch is not None:
            if callable(async_orig) and _is_async_or_direct(async_orig):
                async_patch._ORIG_EXECUTE = base
                changed = True
        if direct_patch is not None:
            if callable(direct_orig) and _is_async_or_direct(direct_orig):
                direct_patch._ORIG = base
                changed = True
            direct_wrapper = getattr(direct_patch, "_patched_execute_ai_ok_entries_bulk", None)
            if callable(direct_wrapper) and current is not direct_wrapper:
                # Prefer direct_dispatch as the top executor. Its _ORIG is now the safe base,
                # so it can run fallback dispatch without calling async_entry again.
                setattr(direct_wrapper, "_original", base)
                exec_mod.execute_ai_ok_entries_bulk = direct_wrapper
                if runner_mod is not None:
                    try:
                        runner_mod.execute_ai_ok_entries_bulk = direct_wrapper
                    except Exception:
                        pass
                changed = True
        if changed:
            logger.warning(
                "[SUMMARY AI RUNTIME CONSISTENCY] broke async/direct executor recursion base=%s current=%s async_orig=%s direct_orig=%s version=%s",
                getattr(base, "__name__", type(base).__name__),
                getattr(current, "__name__", type(current).__name__),
                getattr(async_orig, "__name__", type(async_orig).__name__),
                getattr(direct_orig, "__name__", type(direct_orig).__name__),
                VERSION,
            )
        return changed
    except Exception:
        logger.debug("[SUMMARY AI RUNTIME CONSISTENCY] async/direct recursion repair not ready", exc_info=True)
        return False


def _patch_hook_direct_context() -> bool:
    """Return True only when this call changed runtime state."""
    if not _env_bool("SUMMARY_AI_DIRECT_INPUT_FRESH_CHECK", True):
        return False
    try:
        import scheduler_jobs.summary.summary_ai_entry_hook_v20 as hook
        cur = getattr(hook, "run_summary_ai_entry_safe", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_hook_direct_input_v3", False):
            return False

        @wraps(cur)
        def _run_summary_ai_entry_safe_with_direct_df(*args: Any, **kwargs: Any):
            df = _extract_df(args, kwargs)
            prev, set_ok = _set_direct_context(df)
            if set_ok:
                ok, age, latest, rows = _df_age(df)
                logger.warning("[SUMMARY AI DIRECT INPUT GUARD] hook context set rows=%s latest=%s age=%.1f version=%s", rows, latest, float(age or 0.0), VERSION)
            try:
                return cur(*args, **kwargs)
            finally:
                _restore_direct_context(prev)

        _run_summary_ai_entry_safe_with_direct_df._summary_ai_hook_direct_input_v3 = True  # type: ignore[attr-defined]
        _run_summary_ai_entry_safe_with_direct_df._summary_ai_hook_direct_input_v2 = True  # type: ignore[attr-defined]
        _run_summary_ai_entry_safe_with_direct_df._summary_ai_hook_direct_input_v1 = True  # type: ignore[attr-defined]
        _run_summary_ai_entry_safe_with_direct_df._original = cur  # type: ignore[attr-defined]
        hook.run_summary_ai_entry_safe = _run_summary_ai_entry_safe_with_direct_df
        logger.warning("[SUMMARY AI DIRECT INPUT GUARD] hook wrapped version=%s", VERSION)
        return True
    except Exception:
        logger.debug("[SUMMARY AI DIRECT INPUT GUARD] hook wrap not ready", exc_info=True)
        return False


def _patch_safety_guard_context() -> bool:
    """Return True only when this call changed runtime state."""
    if not _env_bool("SUMMARY_AI_DIRECT_INPUT_FRESH_CHECK", True):
        return False
    changed = False
    try:
        import core.startup.summary_ai_candidate_refill_patch as crp
        import trading.entry.summary_ai.runner as runner
        old_get = getattr(crp, "_get_push_1m_context", None)
        if callable(old_get) and not getattr(old_get, "_summary_ai_direct_input_v3", False):
            @wraps(old_get)
            def _get_push_1m_context_direct_first(*args: Any, **kwargs: Any):
                direct = getattr(_TLS, "direct_df", None)
                if _fresh_direct_df(direct):
                    ok, age, latest, rows = _df_age(direct)
                    logger.warning("[SUMMARY AI DIRECT INPUT GUARD] fresh-check source=direct_runner_input rows=%s latest=%s age=%.1f version=%s", rows, latest, float(age or 0.0), VERSION)
                    return direct
                if _fresh_direct_df(_LAST_DIRECT_DF):
                    ok, age, latest, rows = _df_age(_LAST_DIRECT_DF)
                    logger.warning("[SUMMARY AI DIRECT INPUT GUARD] fresh-check source=last_direct_input rows=%s latest=%s age=%.1f version=%s", rows, latest, float(age or 0.0), VERSION)
                    return _LAST_DIRECT_DF
                return old_get(*args, **kwargs)
            _get_push_1m_context_direct_first._summary_ai_direct_input_v3 = True  # type: ignore[attr-defined]
            _get_push_1m_context_direct_first._summary_ai_direct_input_v2 = True  # type: ignore[attr-defined]
            _get_push_1m_context_direct_first._summary_ai_direct_input_v1 = True  # type: ignore[attr-defined]
            _get_push_1m_context_direct_first._original = old_get  # type: ignore[attr-defined]
            crp._get_push_1m_context = _get_push_1m_context_direct_first
            changed = True

        cur = getattr(runner, "run_summary_ai_entry_from_df", None)
        if callable(cur) and not getattr(cur, "_summary_ai_direct_input_v3", False):
            @wraps(cur)
            def _run_with_direct_df_context(*args: Any, **kwargs: Any):
                df = _extract_df(args, kwargs)
                prev, _ = _set_direct_context(df)
                try:
                    return cur(*args, **kwargs)
                finally:
                    _restore_direct_context(prev)
            _run_with_direct_df_context._summary_ai_direct_input_v3 = True  # type: ignore[attr-defined]
            _run_with_direct_df_context._summary_ai_direct_input_v2 = True  # type: ignore[attr-defined]
            _run_with_direct_df_context._summary_ai_direct_input_v1 = True  # type: ignore[attr-defined]
            _run_with_direct_df_context._original = cur  # type: ignore[attr-defined]
            runner.run_summary_ai_entry_from_df = _run_with_direct_df_context
            changed = True
        if changed:
            logger.warning("[SUMMARY AI DIRECT INPUT GUARD] installed version=%s", VERSION)
        return changed
    except Exception:
        logger.debug("[SUMMARY AI DIRECT INPUT GUARD] install not ready", exc_info=True)
        return False


def _patch_executor_selection() -> bool:
    """Return True only when this call changed runtime state."""
    if not _env_bool("SUMMARY_AI_PROTECT_EXECUTOR_SELECTION", True):
        return False
    try:
        import trading.entry.summary_ai.executor as ex
        cur = getattr(ex, "_select_ai_ok_items", None)
        if callable(cur) and getattr(cur, "_summary_ai_executor_selection_protect_v3", False):
            return False

        def _select_ai_ok_items_protected(ok_items, *, max_entries: int):
            try:
                pool = ex._selected_pool(ok_items, max_entries=max_entries)
                cap = ex._effective_max_entries(max_entries)
                selected = list(pool[:cap])
                logger.warning(
                    "[SUMMARY AI EXECUTOR] protected selection requested=%s cap=%s pool=%s ok_total=%s selected=%s version=%s",
                    max_entries,
                    cap,
                    len(pool),
                    len(ok_items or []),
                    [{"symbol": ex._pick_symbol(x), "side": ex._pick_side(x), "price": ex._pick_price(x), "score": round(ex._score_for_side(x), 3)} for x in selected],
                    VERSION,
                )
                return selected
            except Exception:
                logger.exception("[SUMMARY AI EXECUTOR] protected selection failed; fallback current")
                try:
                    return cur(ok_items, max_entries=max_entries) if callable(cur) else []
                except Exception:
                    return []

        _select_ai_ok_items_protected._summary_ai_executor_selection_protect_v3 = True  # type: ignore[attr-defined]
        _select_ai_ok_items_protected._summary_ai_executor_selection_protect_v2 = True  # type: ignore[attr-defined]
        _select_ai_ok_items_protected._summary_ai_executor_selection_protect_v1 = True  # type: ignore[attr-defined]
        _select_ai_ok_items_protected._original = cur  # type: ignore[attr-defined]
        ex._select_ai_ok_items = _select_ai_ok_items_protected
        logger.warning("[SUMMARY AI EXECUTOR] protected selection installed version=%s old=%s", VERSION, getattr(cur, "__name__", type(cur).__name__))
        return True
    except Exception:
        logger.debug("[SUMMARY AI EXECUTOR] protected selection install not ready", exc_info=True)
        return False


def _install_blowoff_prefilter() -> bool:
    """Install the reentrant-safe blowoff prefilter once."""
    if not _env_bool("SUMMARY_AI_EXECUTOR_BLOWOFF_PREFILTER", True):
        return False
    try:
        import trading.entry.summary_ai.executor as ex
        cur = getattr(ex, "execute_ai_ok_entries_bulk", None)
        if callable(cur) and _chain_has_marker(cur, _BLOWOFF_MARKERS):
            return False

        from core.startup import summary_ai_blowoff_prefilter_patch as bp
        fn = getattr(bp, "install", None)
        if not callable(fn):
            return False
        try:
            ok = bool(fn(reason="runtime_consistency"))
        except TypeError:
            ok = bool(fn())
        if ok:
            logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] blowoff prefilter installed via=%s version=%s", getattr(bp, "VERSION", "unknown"), VERSION)
        return bool(ok)
    except Exception:
        logger.debug("[SUMMARY AI RUNTIME CONSISTENCY] blowoff prefilter install not ready", exc_info=True)
        return False


def _install_fast_order_builder() -> bool:
    """Install Summary-AI order-builder fast path even when usercustomize is not loaded."""
    if not _env_bool("SUMMARY_AI_FAST_ORDER_BUILDER_ENABLED", True):
        return False
    try:
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_SEC", "0.8")
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", "0.2")
        from core.startup import summary_ai_fast_order_builder_patch as fp
        fn = getattr(fp, "install", None)
        ok = bool(fn()) if callable(fn) else False
        if ok:
            logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] fast order builder ensured via=%s version=%s", getattr(fp, "VERSION", "unknown"), VERSION)
        return bool(ok)
    except Exception:
        logger.debug("[SUMMARY AI RUNTIME CONSISTENCY] fast order builder install not ready", exc_info=True)
        return False


def _enforce(reason: str = "install") -> bool:
    changed_cycle = _break_async_direct_cycle()
    changed_hook = _patch_hook_direct_context()
    changed_direct = _patch_safety_guard_context()
    changed_selection = _patch_executor_selection()
    changed_blowoff = _install_blowoff_prefilter()
    changed_fast_order = _install_fast_order_builder()
    # Run once more after possible imports because direct_dispatch/async_entry can be imported by hooks above.
    changed_cycle_late = _break_async_direct_cycle()
    changed = bool(changed_cycle or changed_cycle_late or changed_hook or changed_direct or changed_selection or changed_blowoff or changed_fast_order)
    if changed:
        logger.warning(
            "[SUMMARY AI RUNTIME CONSISTENCY] enforce reason=%s cycle=%s/%s hook=%s direct_input=%s selection=%s blowoff_prefilter=%s fast_order=%s version=%s",
            reason,
            changed_cycle,
            changed_cycle_late,
            changed_hook,
            changed_direct,
            changed_selection,
            changed_blowoff,
            changed_fast_order,
            VERSION,
        )
    return changed


def _watcher() -> None:
    loops = max(1, _env_int("SUMMARY_AI_CONSISTENCY_WATCH_LOOPS", 240))
    sleep_sec = max(0.5, _env_float("SUMMARY_AI_CONSISTENCY_WATCH_INTERVAL", 1.0))
    for i in range(loops):
        try:
            _enforce(reason=f"watcher:{i}")
        except Exception:
            pass
        time.sleep(sleep_sec)
    logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] watcher done loops=%s version=%s", loops, VERSION)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("SUMMARY_AI_RUNTIME_CONSISTENCY_ENABLED", True):
        logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] disabled by env")
        return False
    changed = _enforce(reason="install")
    if not _WATCHER_STARTED and _env_bool("SUMMARY_AI_RUNTIME_CONSISTENCY_WATCHER", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-ai-runtime-consistency-watch", daemon=True).start()
        logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] watcher started version=%s", VERSION)
    _INSTALLED = bool(changed or _WATCHER_STARTED)
    logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] installed ok=%s changed=%s version=%s", _INSTALLED, changed, VERSION)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI RUNTIME CONSISTENCY] auto install failed")


__all__ = ["VERSION", "install"]
