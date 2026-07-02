# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_runtime_consistency_patch.py
# Version: V1-SUMMARY-AI-FRESH-INPUT-AND-SELECTION-PROTECT
# ------------------------------------------------------------
# Purpose:
#   1) Summary-AI safety guard must not block a fresh direct 1m df just because
#      an older global_context summary_history remains cached.
#   2) Runtime final-board compatibility patches must not replace executor
#      selection with a pool that collapses AI_OK rows before rolling retry.
#
# This does not relax low-move / blowoff / liquidity / board guards.  It only
# makes the safety guard and candidate selection use the current fresh input and
# executor-native filtering consistently.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-FRESH-INPUT-AND-SELECTION-PROTECT"
_INSTALLED = False
_WATCHER_STARTED = False
_TLS = threading.local()
_LAST_DIRECT_DF: Any = None
_LAST_DIRECT_AT: float = 0.0


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
    ok, age, latest, rows = _df_age(df)
    if not ok or age is None:
        return False
    max_age = _env_float("SUMMARY_AI_DIRECT_INPUT_MAX_AGE_SEC", _env_float("SUMMARY_AI_MAX_PUSH_1M_AGE_SEC", 120.0))
    return age <= max_age


def _patch_safety_guard_context() -> bool:
    """Patch candidate_refill fresh check to prefer the current direct runner df."""
    if not _env_bool("SUMMARY_AI_DIRECT_INPUT_FRESH_CHECK", True):
        return False
    try:
        import core.startup.summary_ai_candidate_refill_patch as crp
        import trading.entry.summary_ai.runner as runner

        old_get = getattr(crp, "_get_push_1m_context", None)
        if callable(old_get) and not getattr(old_get, "_summary_ai_direct_input_v1", False):
            @wraps(old_get)
            def _get_push_1m_context_direct_first(*args: Any, **kwargs: Any):
                direct = getattr(_TLS, "direct_df", None)
                if _fresh_direct_df(direct):
                    ok, age, latest, rows = _df_age(direct)
                    logger.warning(
                        "[SUMMARY AI DIRECT INPUT GUARD] fresh-check source=direct_runner_input rows=%s latest=%s age=%.1f version=%s",
                        rows, latest, float(age or 0.0), VERSION,
                    )
                    return direct
                if _fresh_direct_df(_LAST_DIRECT_DF):
                    ok, age, latest, rows = _df_age(_LAST_DIRECT_DF)
                    logger.warning(
                        "[SUMMARY AI DIRECT INPUT GUARD] fresh-check source=last_direct_input rows=%s latest=%s age=%.1f version=%s",
                        rows, latest, float(age or 0.0), VERSION,
                    )
                    return _LAST_DIRECT_DF
                return old_get(*args, **kwargs)

            _get_push_1m_context_direct_first._summary_ai_direct_input_v1 = True  # type: ignore[attr-defined]
            _get_push_1m_context_direct_first._original = old_get  # type: ignore[attr-defined]
            crp._get_push_1m_context = _get_push_1m_context_direct_first

        cur = getattr(runner, "run_summary_ai_entry_from_df", None)
        if callable(cur) and not getattr(cur, "_summary_ai_direct_input_v1", False):
            @wraps(cur)
            def _run_with_direct_df_context(*args: Any, **kwargs: Any):
                global _LAST_DIRECT_DF, _LAST_DIRECT_AT
                df = _extract_df(args, kwargs)
                prev = getattr(_TLS, "direct_df", None)
                if _is_df(df):
                    _TLS.direct_df = df
                    _LAST_DIRECT_DF = df
                    _LAST_DIRECT_AT = time.time()
                try:
                    return cur(*args, **kwargs)
                finally:
                    try:
                        _TLS.direct_df = prev
                    except Exception:
                        pass

            _run_with_direct_df_context._summary_ai_direct_input_v1 = True  # type: ignore[attr-defined]
            _run_with_direct_df_context._original = cur  # type: ignore[attr-defined]
            runner.run_summary_ai_entry_from_df = _run_with_direct_df_context

        logger.warning("[SUMMARY AI DIRECT INPUT GUARD] installed version=%s", VERSION)
        return True
    except Exception:
        logger.debug("[SUMMARY AI DIRECT INPUT GUARD] install not ready", exc_info=True)
        return False


def _patch_executor_selection() -> bool:
    """Restore executor-native AI_OK selection if outer patches replaced it."""
    if not _env_bool("SUMMARY_AI_PROTECT_EXECUTOR_SELECTION", True):
        return False
    try:
        import trading.entry.summary_ai.executor as ex

        cur = getattr(ex, "_select_ai_ok_items", None)
        if callable(cur) and getattr(cur, "_summary_ai_executor_selection_protect_v1", False):
            return True

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

        _select_ai_ok_items_protected._summary_ai_executor_selection_protect_v1 = True  # type: ignore[attr-defined]
        _select_ai_ok_items_protected._original = cur  # type: ignore[attr-defined]
        ex._select_ai_ok_items = _select_ai_ok_items_protected
        logger.warning("[SUMMARY AI EXECUTOR] protected selection installed version=%s old=%s", VERSION, getattr(cur, "__name__", type(cur).__name__))
        return True
    except Exception:
        logger.debug("[SUMMARY AI EXECUTOR] protected selection install not ready", exc_info=True)
        return False


def _enforce(reason: str = "install") -> bool:
    ok1 = _patch_safety_guard_context()
    ok2 = _patch_executor_selection()
    if ok1 or ok2:
        logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] enforce reason=%s direct_input=%s selection=%s version=%s", reason, ok1, ok2, VERSION)
    return bool(ok1 or ok2)


def _watcher() -> None:
    loops = max(1, _env_int("SUMMARY_AI_CONSISTENCY_WATCH_LOOPS", 180))
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
    ok = _enforce(reason="install")
    if not _WATCHER_STARTED and _env_bool("SUMMARY_AI_RUNTIME_CONSISTENCY_WATCHER", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-ai-runtime-consistency-watch", daemon=True).start()
        logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] watcher started version=%s", VERSION)
    _INSTALLED = bool(ok or _WATCHER_STARTED)
    logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] installed ok=%s version=%s", _INSTALLED, VERSION)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI RUNTIME CONSISTENCY] auto install failed")

__all__ = ["VERSION", "install"]
