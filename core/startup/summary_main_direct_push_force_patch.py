# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_direct_push_force_patch.py
# Version: V7-FORCE-DIRECT-1M-NO-HEAVY-CONTEXT-SETTERS
# ------------------------------------------------------------
# main.py の1分PUSH summary tickを軽量経路に固定する。
#
# V7:
#   - V6で async AI start は復旧したが、context store が48秒かかり、
#     次分の 1m build timeout を誘発していた。
#   - main.py ではDB保存・重いcontext setterを使わず、軽量attrsだけを同期更新する。
#   - Summary-AIは分ごとのdaemon threadで即起動する。
#   - ガード/発注条件は緩和しない。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
import time
from functools import wraps
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V7-FORCE-DIRECT-1M-NO-HEAVY-CONTEXT-SETTERS"
_PATCHED = False
_WATCHER_STARTED = False
_MONOTONIC_PATCHED = False
_AI_LOCK = threading.RLock()
_AI_RUNNING: dict[str, float] = {}
_ORIGINAL_JOB_SUMMARY = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == "" else float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
            return False
        return "main.py" in argv
    except Exception:
        return False


def _dt_key(now: Any) -> str:
    return now.strftime("%Y%m%d%H%M%S") if isinstance(now, dt.datetime) else str(now)


def _latest_dt(df: Any) -> pd.Timestamp | None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
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
                    return ts.tz_localize(None) if getattr(ts, "tzinfo", None) else ts
    except Exception:
        pass
    return None


def _extract_interval_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> int | None:
    try:
        for k in ("tf", "interval"):
            if k in kwargs and kwargs.get(k) is not None:
                return int(kwargs.get(k))
        for x in args:
            if isinstance(x, int):
                return int(x)
            if isinstance(x, str) and x.strip().isdigit():
                return int(x)
    except Exception:
        pass
    return None


def _extract_source_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    try:
        if "source" in kwargs and kwargs.get("source") is not None:
            return str(kwargs.get("source") or "").strip().lower()
        for x in reversed(args):
            if isinstance(x, str) and x.strip().lower() in {"push", "push-cache", "summary", "legacy", "ranking"}:
                return x.strip().lower()
    except Exception:
        pass
    return "push"


def _extract_df_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> pd.DataFrame | None:
    try:
        for k in ("df", "summary_df"):
            if isinstance(kwargs.get(k), pd.DataFrame):
                return kwargs.get(k)
        for x in args:
            if isinstance(x, pd.DataFrame):
                return x
    except Exception:
        pass
    return None


def _get_existing_for_setter(obj: Any, setter_name: str, interval: int, source: str) -> pd.DataFrame:
    getters = ["get_merged_summary", "get_push_merged_summary", "get_push_summary", "get_summary_history"]
    for name in getters:
        fn = getattr(obj, name, None)
        if not callable(fn):
            continue
        for call in (
            lambda fn=fn: fn(tf=interval, source=source),
            lambda fn=fn: fn(interval, source=source),
            lambda fn=fn: fn(interval),
        ):
            try:
                v = call()
                if isinstance(v, pd.DataFrame) and not v.empty:
                    return v
            except TypeError:
                continue
            except Exception:
                break
    return pd.DataFrame()


def _maybe_skip_stale_overwrite(obj: Any, setter_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> pd.DataFrame | None:
    if not _env_bool("SUMMARY_PUSH_MONOTONIC_CACHE_GUARD", True):
        return None
    try:
        interval = _extract_interval_from_call(args, kwargs)
        if interval != 1:
            return None
        source = _extract_source_from_call(args, kwargs)
        if source not in {"push", "push-cache", "summary"}:
            return None
        incoming = _extract_df_from_call(args, kwargs)
        if not isinstance(incoming, pd.DataFrame) or incoming.empty:
            return None
        incoming_latest = _latest_dt(incoming)
        if incoming_latest is None:
            return None
        existing = _get_existing_for_setter(obj, setter_name, 1, "push")
        existing_latest = _latest_dt(existing)
        if existing_latest is None:
            return None
        lag = (existing_latest - incoming_latest).total_seconds()
        if lag > _env_float("SUMMARY_PUSH_MONOTONIC_TOLERANCE_SEC", 5.0):
            logger.warning(
                "[SUMMARY PUSH MONOTONIC GUARD] skip stale overwrite setter=%s source=%s incoming_latest=%s existing_latest=%s lag=%.1fs incoming_rows=%s existing_rows=%s version=%s",
                setter_name, source, incoming_latest, existing_latest, lag, len(incoming), len(existing), VERSION,
            )
            return existing.copy(deep=False)
    except Exception:
        logger.debug("[SUMMARY PUSH MONOTONIC GUARD] check failed setter=%s", setter_name, exc_info=True)
    return None


def _install_monotonic_guard() -> bool:
    global _MONOTONIC_PATCHED
    if _MONOTONIC_PATCHED or not _env_bool("SUMMARY_PUSH_MONOTONIC_CACHE_GUARD", True):
        return bool(_MONOTONIC_PATCHED)
    patched = 0
    targets: list[Any] = []
    try:
        from global_state import global_data
        targets.append(global_data)
    except Exception:
        pass
    try:
        from core.global_context.context import global_context
        targets.append(global_context)
    except Exception:
        pass
    seen: set[int] = set()
    for obj in targets:
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        for name in ("set_summary_history", "set_merged_summary", "set_push_summary", "set_push_merged_summary", "set_latest_summary"):
            old = getattr(obj, name, None)
            if not callable(old) or getattr(old, "_summary_push_monotonic_guard_v7", False):
                continue

            @wraps(old)
            def _wrapped(*args: Any, __old=old, __obj=obj, __name=name, **kwargs: Any):
                existing = _maybe_skip_stale_overwrite(__obj, __name, args, kwargs)
                if isinstance(existing, pd.DataFrame) and not existing.empty:
                    return existing
                return __old(*args, **kwargs)

            _wrapped._summary_push_monotonic_guard_v7 = True  # type: ignore[attr-defined]
            _wrapped._summary_push_monotonic_guard_v6 = True  # type: ignore[attr-defined]
            _wrapped._summary_push_monotonic_guard_v5 = True  # type: ignore[attr-defined]
            _wrapped._original = old  # type: ignore[attr-defined]
            try:
                setattr(obj, name, _wrapped)
                patched += 1
            except Exception:
                pass
    _MONOTONIC_PATCHED = patched > 0
    logger.warning("[SUMMARY PUSH MONOTONIC GUARD] installed patched=%s version=%s", patched, VERSION)
    return patched > 0


def _raw_memory_rows() -> int:
    try:
        from core.startup.summary_main_memory_latest_1m_patch import _load_push_memory_df
        df = _load_push_memory_df()
        return len(df) if isinstance(df, pd.DataFrame) else 0
    except Exception:
        return 0


def _build_direct_inner(now: dt.datetime) -> pd.DataFrame:
    try:
        from core.startup.summary_main_memory_latest_1m_patch import _build_memory_1m_summary
        df = _build_memory_1m_summary(now=now)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] robust memory builder failed", exc_info=True)
    return pd.DataFrame()


def _build_direct(now: dt.datetime) -> pd.DataFrame:
    t0 = time.perf_counter()
    timeout = max(0.0, _env_float("SUMMARY_FORCE_DIRECT_BUILD_TIMEOUT_SEC", 6.0))
    if timeout <= 0:
        df = _build_direct_inner(now)
    else:
        box: dict[str, Any] = {"df": pd.DataFrame(), "error": None}

        def _target() -> None:
            try:
                box["df"] = _build_direct_inner(now)
            except Exception as e:
                box["error"] = e

        th = threading.Thread(target=_target, name="summary-force-direct-build", daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            logger.error(
                "[SUMMARY FORCE DIRECT 1M] build timeout timeout=%.3fs elapsed=%.3fs now=%s version=%s note=inner_thread_left_daemon",
                timeout, time.perf_counter() - t0, now, VERSION,
            )
            return pd.DataFrame()
        df = box.get("df")
    if isinstance(df, pd.DataFrame) and not df.empty:
        logger.warning(
            "[SUMMARY FORCE DIRECT 1M] built via robust memory rows=%s symbols=%s latest_dt=%s elapsed=%.3fs timeout=%.3fs version=%s",
            len(df), int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
            df["datetime"].max() if "datetime" in df.columns else None,
            time.perf_counter() - t0, timeout, VERSION,
        )
        return df.reset_index(drop=True)
    return pd.DataFrame()


def _normalize_1m_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        out = df.copy()
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass
            out = out.dropna(subset=["datetime"])
        if "close" not in out.columns:
            for c in ("close_price", "current_price", "price"):
                if c in out.columns:
                    out["close"] = out[c]
                    break
        if "close" not in out.columns:
            return pd.DataFrame()
        for c in ("open", "high", "low"):
            if c not in out.columns:
                out[c] = out["close"]
        if "volume" not in out.columns:
            for c in ("trading_volume", "vol", "Volume", "出来高"):
                if c in out.columns:
                    out["volume"] = out[c]
                    break
        if "volume" not in out.columns:
            out["volume"] = 0.0
        for c in ("open", "high", "low", "close", "volume"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.dropna(subset=["symbol", "datetime", "close"])
        out["interval"] = 1
        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY FORCE DIRECT 1M] normalize 1m df failed", exc_info=True)
        return pd.DataFrame()


def _assign_attrs(obj: Any, hist: pd.DataFrame, latest: pd.DataFrame) -> None:
    for name, value in (
        ("summary_1m_df", hist),
        ("latest_summary_1m_df", latest),
        ("summary_1m_latest_df", latest),
        ("push_summary_1", latest),
        ("push_summary_1min", latest),
        ("push_summary_1m_df", hist),
        ("push_merged_summary_1", latest),
        ("push_merged_summary_1min", latest),
        ("push_merged_summary_1m_df", latest),
        ("merged_summary_1", latest),
        ("merged_summary_1min", latest),
    ):
        try:
            setattr(obj, name, value.copy(deep=False))
        except Exception:
            pass


def _store(df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    hist = _normalize_1m_df(df)
    if hist.empty:
        return
    try:
        latest = hist.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1).reset_index(drop=True)
    except Exception:
        latest = hist.copy()
    t0 = time.perf_counter()
    try:
        from global_state import global_data
        _assign_attrs(global_data, hist, latest)
    except Exception:
        pass
    try:
        from core.global_context.context import global_context as GC
        _assign_attrs(GC, hist, latest)
    except Exception:
        pass

    # V7: main.pyでは重い setter は呼ばない。必要なら明示的に有効化する。
    if _env_bool("SUMMARY_FORCE_DIRECT_HEAVY_CONTEXT_SETTERS", False):
        logger.warning("[SUMMARY FORCE DIRECT 1M] heavy context setters requested but disabled in V7 safe path")

    logger.warning(
        "[SUMMARY FORCE DIRECT 1M] fast attrs only tf=1 hist_rows=%s latest_rows=%s latest_dt=%s elapsed=%.3fs version=%s",
        len(hist), len(latest), hist["datetime"].max() if "datetime" in hist.columns else None, time.perf_counter() - t0, VERSION,
    )


def _cleanup_ai_running(now_ts: float) -> None:
    max_age = max(30.0, _env_float("SUMMARY_FORCE_DIRECT_AI_RUNNING_MAX_AGE_SEC", 90.0))
    stale: list[tuple[str, float]] = []
    for k, ts in list(_AI_RUNNING.items()):
        age = now_ts - float(ts or 0.0)
        if age > max_age:
            stale.append((k, age))
    for k, age in stale:
        _AI_RUNNING.pop(k, None)
        logger.warning("[SUMMARY FORCE DIRECT 1M] async AI stale running key cleared key=%s age_sec=%.1f version=%s", k, age, VERSION)


def _submit_ai(df: pd.DataFrame, now: dt.datetime, run_entry: bool) -> None:
    if not run_entry or df is None or df.empty or not _env_bool("SUMMARY_FORCE_DIRECT_ASYNC_AI", True):
        return
    key = "force-summary-ai:1:" + _dt_key(now)
    now_ts = time.time()
    with _AI_LOCK:
        _cleanup_ai_running(now_ts)
        if key in _AI_RUNNING:
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI skipped already_running key=%s rows=%s", key, len(df))
            return
        active_cap = max(1, _env_int("SUMMARY_FORCE_DIRECT_AI_MAX_ACTIVE", 3))
        if len(_AI_RUNNING) >= active_cap:
            oldest = sorted(_AI_RUNNING.items(), key=lambda kv: kv[1])[:3]
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI skipped active_cap=%s active=%s oldest=%s key=%s", active_cap, len(_AI_RUNNING), oldest, key)
            return
        _AI_RUNNING[key] = now_ts
    df_copy = df.copy(deep=False)

    def _task() -> None:
        started = time.time()
        try:
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI start key=%s rows=%s version=%s", key, len(df_copy), VERSION)
            try:
                from scheduler_jobs.summary.summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
                run_summary_ai_entry_safe(interval=1, now=now, df=df_copy, source="SUMMARY")
            except Exception:
                logger.exception("[SUMMARY FORCE DIRECT 1M] async AI failed key=%s", key)
        finally:
            with _AI_LOCK:
                _AI_RUNNING.pop(key, None)
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI done key=%s elapsed=%.3fs version=%s", key, time.time() - started, VERSION)

    try:
        threading.Thread(target=_task, name=f"summary-force-direct-ai-{_dt_key(now)}", daemon=True).start()
        logger.warning("[SUMMARY FORCE DIRECT 1M] async AI thread started key=%s rows=%s active=%s version=%s", key, len(df_copy), len(_AI_RUNNING), VERSION)
    except Exception:
        with _AI_LOCK:
            _AI_RUNNING.pop(key, None)
        logger.exception("[SUMMARY FORCE DIRECT 1M] async AI thread start failed key=%s", key)


def _patch_once(reason: str = "install") -> bool:
    global _ORIGINAL_JOB_SUMMARY
    _install_monotonic_guard()
    if not _is_main_py() or not _env_bool("SUMMARY_FORCE_DIRECT_PATCH_ENABLED", True):
        return False
    try:
        import scheduler_jobs.summary.runner_core as rc
        current = getattr(rc, "job_summary", None)
        if getattr(current, "_summary_force_direct_v7", False):
            return True
        if _ORIGINAL_JOB_SUMMARY is None and callable(current):
            _ORIGINAL_JOB_SUMMARY = getattr(current, "_original", current)
        orig = _ORIGINAL_JOB_SUMMARY if callable(_ORIGINAL_JOB_SUMMARY) else current

        def job_summary_force(interval: int, display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True, **kwargs) -> pd.DataFrame:
            interval_i = int(interval)
            now_i = (now or rc.now_naive()).replace(microsecond=0)
            if interval_i != 1:
                return orig(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)
            t0 = time.perf_counter()
            df = _build_direct(now_i)
            if df is not None and not df.empty:
                _store(df)
                _submit_ai(df, now_i, run_entry)
                logger.warning("[SUMMARY FORCE DIRECT 1M] return interval=1 rows=%s elapsed=%.3fs mode=forced_direct_v7", len(df), time.perf_counter() - t0)
                return df
            raw_rows = _raw_memory_rows()
            if _env_bool("SUMMARY_FORCE_DIRECT_NO_ORIGINAL_FALLBACK_WHEN_RAW_EXISTS", True) and raw_rows > 0:
                logger.warning(
                    "[SUMMARY FORCE DIRECT 1M] direct empty/timeout but raw memory exists -> skip original heavy fallback interval=1 raw_rows=%s elapsed=%.3fs",
                    raw_rows, time.perf_counter() - t0,
                )
                return pd.DataFrame()
            logger.warning("[SUMMARY FORCE DIRECT 1M] direct empty -> original fallback interval=1 raw_rows=%s", raw_rows)
            return orig(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)

        for attr in ("_summary_force_direct_v1", "_summary_force_direct_v2", "_summary_force_direct_v3", "_summary_force_direct_v4", "_summary_force_direct_v5", "_summary_force_direct_v6", "_summary_force_direct_v7"):
            setattr(job_summary_force, attr, True)
        job_summary_force._original = orig  # type: ignore[attr-defined]
        rc.job_summary = job_summary_force
        rc.run_push_summary_job = lambda interval=1, display=True, now=None, run_entry=True, **kwargs: job_summary_force(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)
        rc.job_1m = lambda display=True, now=None, run_entry=True: job_summary_force(1, display=display, now=now, run_entry=run_entry)
        logger.warning("[SUMMARY FORCE DIRECT 1M] patched reason=%s target=runner_core.job_summary version=%s", reason, VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY FORCE DIRECT 1M] patch failed reason=%s", reason)
        return False


def _watcher() -> None:
    deadline = time.time() + max(30, _env_int("SUMMARY_FORCE_DIRECT_WATCH_SEC", 180))
    i = 0
    while time.time() < deadline:
        try:
            _patch_once(reason=f"watcher:{i}")
        except Exception:
            logger.debug("[SUMMARY FORCE DIRECT 1M] watcher reapply failed", exc_info=True)
        i += 1
        time.sleep(max(0.5, float(os.getenv("SUMMARY_FORCE_DIRECT_WATCH_INTERVAL", "2.0"))))
    logger.warning("[SUMMARY FORCE DIRECT 1M] watcher done reapplies=%s", i)


def install() -> bool:
    global _PATCHED, _WATCHER_STARTED
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_NO_ORIGINAL_FALLBACK_WHEN_RAW_EXISTS", "1")
    os.environ.setdefault("SUMMARY_PUSH_MONOTONIC_CACHE_GUARD", "1")
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_BUILD_TIMEOUT_SEC", "6")
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_AI_MAX_ACTIVE", "3")
    os.environ.setdefault("SUMMARY_FORCE_DIRECT_HEAVY_CONTEXT_SETTERS", "0")
    ok = _patch_once(reason="install")
    if ok and not _WATCHER_STARTED and _env_bool("SUMMARY_FORCE_DIRECT_WATCHER", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-force-direct-1m-watch", daemon=True).start()
        logger.warning("[SUMMARY FORCE DIRECT 1M] watcher started version=%s", VERSION)
    _PATCHED = bool(ok)
    logger.warning("[SUMMARY FORCE DIRECT 1M] installed version=%s ok=%s main=%s", VERSION, ok, _is_main_py())
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY FORCE DIRECT 1M] auto install failed")

__all__ = ["VERSION", "install"]
