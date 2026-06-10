# ============================================================
# File   : core/startup/ranking_entry_controller_timeout_patch.py
# Version: V1.9-RANKING-CONTROLLER-HARD-CAPS
# ------------------------------------------------------------
# RANKING ENTRY は pending 作成後の entry_controller が長時間ロックを握ると、
# TONOSAMA / SUMMARY の発注判定を待たせて timeout させる。
#
# V1.9:
#   - 外部 env / settings が 150 秒などを入れても、起動時と実行直前に短い上限へ強制補正。
#   - build timeout / controller timeout / runtime budget を hard cap 化。
#   - 1回の ranking entry で作る pending を3件に制限して、次回へ分割。
#   - timeout 時は stale pending を掃除して、次サイクルを詰まらせない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_RANKING_SAFE = None
_ORIG_ENTRY_CONTROLLER_PRECHECK = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _cap_env_float(name: str, *, default: float, lower: float, upper: float, force: bool = True) -> float:
    """外部設定が大きすぎても短い上限へ丸める。"""
    cur = _env_float(name, default)
    capped = max(float(lower), min(float(cur), float(upper)))
    if force or str(os.getenv(name, "")).strip() == "":
        os.environ[name] = str(capped)
    return capped


def _cap_env_int(name: str, *, default: int, lower: int, upper: int, force: bool = True) -> int:
    try:
        cur = int(float(os.getenv(name, str(default))))
    except Exception:
        cur = int(default)
    capped = max(int(lower), min(int(cur), int(upper)))
    if force or str(os.getenv(name, "")).strip() == "":
        os.environ[name] = str(capped)
    return capped


def _parse_dt(v: Any) -> dt.datetime | None:
    try:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in {"none", "nan", "nat"}:
            return None
        return dt.datetime.fromisoformat(s.replace("T", " ")).replace(tzinfo=None)
    except Exception:
        try:
            import pandas as pd
            x = pd.to_datetime(v, errors="coerce")
            if pd.isna(x):
                return None
            return x.to_pydatetime().replace(tzinfo=None)
        except Exception:
            return None


def _latest_snapshot_time(db_path: str) -> tuple[dt.datetime | None, str, int]:
    if not db_path:
        return None, "no_db_path", 0
    tables = ["ranking_snapshot_1min", "ranking_snapshot", "ranking_raw", "ranking_raw_1min"]
    cols = ["updated_at", "datetime", "snapshot_time", "created_at", "time"]
    try:
        with sqlite3.connect(str(db_path), timeout=2.0) as conn:
            cur = conn.cursor()
            existing = {r[0] for r in cur.execute("select name from sqlite_master where type='table'").fetchall()}
            best_dt = None
            best_src = "no_time"
            best_rows = 0
            for table in tables:
                if table not in existing:
                    continue
                try:
                    cnt = int(cur.execute(f"select count(*) from {table}").fetchone()[0] or 0)
                except Exception:
                    cnt = 0
                best_rows = max(best_rows, cnt)
                if cnt <= 0:
                    continue
                try:
                    table_cols = {r[1] for r in cur.execute(f"pragma table_info({table})").fetchall()}
                except Exception:
                    table_cols = set()
                for col in cols:
                    if col not in table_cols:
                        continue
                    try:
                        raw = cur.execute(f"select max({col}) from {table}").fetchone()[0]
                        parsed = _parse_dt(raw)
                        if parsed is not None and (best_dt is None or parsed > best_dt):
                            best_dt = parsed
                            best_src = f"{table}.{col}"
                    except Exception:
                        continue
            return best_dt, best_src, best_rows
    except Exception:
        logger.exception("[RANKING ENTRY SCHEDULE] ranking snapshot inspect failed db=%s", db_path)
        return None, "inspect_error", 0


def _ranking_snapshot_precheck() -> tuple[bool, dict[str, Any]]:
    if not _env_bool("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", False):
        return True, {"reason": "disabled"}
    try:
        from ats.ats_ranking.db_path import get_usable_ranking_db_path
        db_path = get_usable_ranking_db_path(force_refresh=True, allow_fallback=False, prefer_today_even_if_empty=True)
    except Exception:
        logger.exception("[RANKING ENTRY SCHEDULE] ranking db resolve failed")
        db_path = None
    latest, src, rows = _latest_snapshot_time(str(db_path or ""))
    max_age = _env_float("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", 300.0)
    now = dt.datetime.now()
    age = None if latest is None else (now - latest).total_seconds()
    ok = latest is not None and age is not None and age <= max_age
    return bool(ok), {
        "db": str(db_path or ""),
        "latest": latest.isoformat(sep=" ") if latest else None,
        "source": src,
        "rows": rows,
        "age_sec": None if age is None else round(float(age), 3),
        "max_age_sec": max_age,
    }


def _entry_source(entry: Any) -> str:
    try:
        if isinstance(entry, dict):
            return str(entry.get("source") or entry.get("pipeline_source") or entry.get("entry_type") or "").upper()
        return str(getattr(entry, "source", None) or getattr(entry, "pipeline_source", None) or getattr(entry, "entry_type", None) or "").upper()
    except Exception:
        return ""


def _entry_first_seen_ts(entry: Any) -> float:
    try:
        if isinstance(entry, dict):
            for key in ("_ranking_pending_first_seen_ts", "created_ts", "created_at_ts", "pending_created_ts", "first_seen_ts", "ts"):
                v = entry.get(key)
                if v:
                    return float(v)
            now = time.time()
            entry["_ranking_pending_first_seen_ts"] = now
            return now
    except Exception:
        pass
    return time.time()


def _pending_count_for_source(source: str) -> int:
    source_u = str(source or "").upper()
    total = 0
    try:
        import trading.entry.pending_manager as pm
        iter_entries = getattr(pm, "iter_entries", None)
        if callable(iter_entries):
            for _sym, entry in list(iter_entries()):
                if source_u in _entry_source(entry):
                    total += 1
            if total > 0:
                return int(total)
    except Exception:
        pass
    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", None)
        if isinstance(root, dict):
            for bucket in list(root.values()):
                entries = bucket if isinstance(bucket, (list, tuple, set)) else [bucket]
                for entry in entries:
                    if source_u in _entry_source(entry):
                        total += 1
    except Exception:
        pass
    return int(total)


def _pending_symbols_for_source(source: str) -> list[str]:
    source_u = str(source or "").upper()
    symbols: list[str] = []
    try:
        import trading.entry.pending_manager as pm
        iter_entries = getattr(pm, "iter_entries", None)
        if callable(iter_entries):
            for sym, entry in list(iter_entries()):
                if source_u in _entry_source(entry):
                    symbols.append(str(sym))
    except Exception:
        pass
    return sorted(set(symbols))


def _prune_pending_for_source(source: str, reason: str) -> int:
    if not _env_bool("RANKING_ENTRY_PRUNE_STALE_PENDING_AFTER_DISPATCH", True):
        return 0
    min_age_sec = max(3.0, _env_float("RANKING_ENTRY_STALE_PENDING_MIN_AGE_SEC", 10.0))
    source_u = str(source or "").upper()
    now = time.time()
    try:
        import trading.entry.pending_manager as pm
        prune_entries = getattr(pm, "prune_entries", None)
        if callable(prune_entries):
            def pred(_sym, entry):
                if source_u not in _entry_source(entry):
                    return False
                age = now - _entry_first_seen_ts(entry)
                if age < min_age_sec:
                    logger.warning("[RANKING ENTRY SCHEDULE] stale prune skipped young pending symbol=%s age=%.1fs min_age=%.1fs reason=%s", _sym, age, min_age_sec, reason)
                    return False
                return True
            return int(prune_entries(pred, reason=reason) or 0)
    except Exception:
        logger.warning("[RANKING ENTRY SCHEDULE] pending prune failed source=%s reason=%s", source_u, reason, exc_info=True)
    return 0


def _force_runtime_timeouts(tasks) -> None:
    """毎回実行直前に短い上限へ強制補正する。"""
    try:
        build_cap = _cap_env_float("RANKING_ENTRY_BUILD_TIMEOUT_SEC", default=18.0, lower=6.0, upper=18.0, force=True)
        controller_cap = _cap_env_float("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", default=12.0, lower=5.0, upper=12.0, force=True)
        runtime_cap = _cap_env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", default=15.0, lower=5.0, upper=15.0, force=True)
        max_pending = _cap_env_int("RANKING_ENTRY_MAX_PENDING_PER_RUN", default=3, lower=1, upper=3, force=True)
        os.environ["RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS"] = str(_cap_env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", default=24, lower=8, upper=24, force=True))
        os.environ["RANKING_ENTRY_FAST_MAX_SYMBOLS"] = str(_cap_env_int("RANKING_ENTRY_FAST_MAX_SYMBOLS", default=24, lower=8, upper=24, force=True))
        os.environ["RANKING_ENTRY_FAST_MAX_PER_SIDE"] = str(_cap_env_int("RANKING_ENTRY_FAST_MAX_PER_SIDE", default=12, lower=3, upper=12, force=True))
        os.environ["RANKING_ENTRY_FAST_MAX_PER_TYPE"] = str(_cap_env_int("RANKING_ENTRY_FAST_MAX_PER_TYPE", default=6, lower=2, upper=6, force=True))
        os.environ["RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS"] = str(_cap_env_int("RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS", default=300, lower=80, upper=300, force=True))
        tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = build_cap
        tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = controller_cap
        try:
            tasks.RANKING_ENTRY_MAX_PENDING_PER_RUN = max_pending
        except Exception:
            pass
        logger.warning(
            "[RANKING ENTRY TIMEOUT PATCH] hard caps enforced build=%.1fs controller=%.1fs runtime=%.1fs max_pending=%s",
            build_cap,
            controller_cap,
            runtime_cap,
            max_pending,
        )
    except Exception:
        logger.debug("[RANKING ENTRY TIMEOUT PATCH] force runtime timeouts failed", exc_info=True)


def _patch_entry_controller_precheck() -> bool:
    global _ORIG_ENTRY_CONTROLLER_PRECHECK
    if not _env_bool("RANKING_ENTRY_BYPASS_PRECHECK_WHEN_PENDING", True):
        return False
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "precheck_ranking_entry", None)
        if not callable(cur):
            return False
        if getattr(cur, "_ranking_pending_precheck_bypass_v19", False):
            return True
        orig = getattr(cur, "_original", cur)
        _ORIG_ENTRY_CONTROLLER_PRECHECK = orig

        def patched_precheck(*args, **kwargs):
            pre = orig(*args, **kwargs)
            try:
                if isinstance(pre, dict) and not pre.get("is_ready", False):
                    cnt = _pending_count_for_source("RANKING")
                    if cnt > 0:
                        out = dict(pre)
                        out["is_ready"] = True
                        out["explicit_ready"] = True
                        out["derived_ready"] = True
                        out["bypass_reason"] = "RANKING_PENDING_EXISTS"
                        out["pending_count"] = cnt
                        out["original_detail_reason"] = pre.get("detail_reason") or pre.get("reason")
                        logger.warning("[RANKING PRECHECK BYPASS] pending exists -> allow entry_controller pending_count=%s original=%s", cnt, pre)
                        return out
            except Exception:
                logger.debug("[RANKING PRECHECK BYPASS] check failed", exc_info=True)
            return pre

        patched_precheck._ranking_pending_precheck_bypass_v17 = True  # type: ignore[attr-defined]
        patched_precheck._ranking_pending_precheck_bypass_v18 = True  # type: ignore[attr-defined]
        patched_precheck._ranking_pending_precheck_bypass_v19 = True  # type: ignore[attr-defined]
        patched_precheck._original = orig  # type: ignore[attr-defined]
        ec.precheck_ranking_entry = patched_precheck
        logger.warning("[RANKING PRECHECK BYPASS] patched entry_controller.precheck_ranking_entry v1.9")
        return True
    except Exception:
        logger.exception("[RANKING PRECHECK BYPASS] patch failed")
        return False


def _dispatch_ranking_controller(tasks, timeout_sec: float) -> bool:
    _patch_entry_controller_precheck()
    timeout_sec = max(5.0, min(float(timeout_sec or 12.0), 12.0))
    return bool(tasks._dispatch_entry_controller(pipeline_source="RANKING", interval=1, timeout_sec=timeout_sec, reason="RANKING ENTRY SCHEDULE"))


def _dispatch_and_cleanup_ranking(tasks, *, timeout_sec: float, cleanup_reason: str) -> bool:
    before_symbols = _pending_symbols_for_source("RANKING")
    ok = _dispatch_ranking_controller(tasks, timeout_sec)
    time.sleep(max(0.0, _env_float("RANKING_ENTRY_POST_DISPATCH_GRACE_SEC", 0.1)))
    after_count = _pending_count_for_source("RANKING")
    if after_count > 0:
        removed = _prune_pending_for_source("RANKING", cleanup_reason)
        logger.warning("[RANKING ENTRY SCHEDULE] ranking pending after controller controller_ok=%s before_symbols=%s after_count=%s removed=%s reason=%s", ok, before_symbols, after_count, removed, cleanup_reason)
    return ok


def _patched_run_ranking_entry_safe() -> int:
    import trading.entry_exit.tasks as tasks
    _force_runtime_timeouts(tasks)
    _patch_entry_controller_precheck()
    started_dt = dt.datetime.now()
    started = time.perf_counter()
    with tasks._RANKING_ENTRY_LOCK:
        if tasks._RANKING_ENTRY_COOLDOWN_UNTIL is not None and started_dt < tasks._RANKING_ENTRY_COOLDOWN_UNTIL:
            remain = (tasks._RANKING_ENTRY_COOLDOWN_UNTIL - started_dt).total_seconds()
            logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=timeout_cooldown remain=%.1fs until=%s timeout_streak=%s", remain, tasks._RANKING_ENTRY_COOLDOWN_UNTIL, tasks._RANKING_ENTRY_TIMEOUT_STREAK)
            return 0
        if tasks._RANKING_ENTRY_RUNNING:
            elapsed = (dt.datetime.now() - tasks._RANKING_ENTRY_STARTED_AT).total_seconds() if tasks._RANKING_ENTRY_STARTED_AT else None
            stale_sec = _env_float("RANKING_ENTRY_RUNNING_STALE_RESET_SEC", 45.0)
            if elapsed is not None and elapsed > stale_sec:
                logger.warning("[RANKING ENTRY SCHEDULE] stale running flag reset elapsed=%.1fs stale_sec=%.1fs started_at=%s", elapsed, stale_sec, tasks._RANKING_ENTRY_STARTED_AT)
                tasks._RANKING_ENTRY_RUNNING = False
                tasks._RANKING_ENTRY_STARTED_AT = None
            else:
                logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=previous_still_running started_at=%s elapsed=%s", tasks._RANKING_ENTRY_STARTED_AT, elapsed)
                return 0
        tasks._RANKING_ENTRY_RUNNING = True
        tasks._RANKING_ENTRY_STARTED_AT = started_dt
    try:
        logger.info("[RANKING ENTRY SCHEDULE] fire at=%s patched=v1.9", started_dt.strftime("%Y-%m-%d %H:%M:%S"))
        fresh, diag = _ranking_snapshot_precheck()
        if not fresh:
            logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=ranking_snapshot_stale_before_build diag=%s", diag)
            return 0
        before_pending = _pending_count_for_source("RANKING")
        if before_pending > 0:
            logger.warning("[RANKING ENTRY SCHEDULE] existing ranking pending detected before build count=%s symbols=%s", before_pending, _pending_symbols_for_source("RANKING"))
            # 先に既存pendingを捌く。ビルドを重ねて詰まらせない。
            _dispatch_and_cleanup_ranking(tasks, timeout_sec=tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC, cleanup_reason="RANKING_EXISTING_PENDING_FIRST")
            if _pending_count_for_source("RANKING") >= _cap_env_int("RANKING_ENTRY_MAX_PENDING_PER_RUN", default=3, lower=1, upper=3, force=True):
                logger.warning("[RANKING ENTRY SCHEDULE] build skipped because pending remains count=%s", _pending_count_for_source("RANKING"))
                return 0
        build_fn = tasks._resolve_callable("trading.ranking.entry_from_ranking", "run_ranking_entry_pipeline")
        if not callable(build_fn):
            logger.warning("[RANKING ENTRY SCHEDULE] skipped reason=ranking_entry_pipeline_unavailable")
            return 0
        completed, created_ret = tasks._run_callable_with_timeout(build_fn, timeout_sec=tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC, name="RANKING ENTRY BUILD")
        after_pending = _pending_count_for_source("RANKING")
        if not completed:
            created_by_pending = max(0, after_pending - before_pending)
            logger.warning("[RANKING ENTRY SCHEDULE] build timeout but pending check before=%s after=%s created_by_pending=%s timeout_sec=%.3f elapsed=%.3fs", before_pending, after_pending, created_by_pending, tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC, time.perf_counter() - started)
            if created_by_pending > 0 or after_pending > 0:
                logger.warning("[RANKING ENTRY SCHEDULE] dispatch controller despite build timeout because pending exists count=%s", after_pending)
                _dispatch_and_cleanup_ranking(tasks, timeout_sec=tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC, cleanup_reason="RANKING_BUILD_TIMEOUT_OR_FILTER_NG_STALE")
                with tasks._RANKING_ENTRY_LOCK:
                    tasks._RANKING_ENTRY_TIMEOUT_STREAK = 0
                    tasks._RANKING_ENTRY_COOLDOWN_UNTIL = None
                return int(after_pending)
            with tasks._RANKING_ENTRY_LOCK:
                tasks._RANKING_ENTRY_TIMEOUT_STREAK += 1
                cool_sec = min(30.0, max(10.0, float(tasks._ranking_entry_cooldown_seconds())))
                tasks._RANKING_ENTRY_COOLDOWN_UNTIL = dt.datetime.now() + dt.timedelta(seconds=cool_sec)
            logger.warning("[RANKING ENTRY SCHEDULE] build timeout -> cooldown timeout_sec=%.3f elapsed=%.3fs timeout_streak=%s cooldown_sec=%.1f until=%s", tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC, time.perf_counter() - started, tasks._RANKING_ENTRY_TIMEOUT_STREAK, cool_sec, tasks._RANKING_ENTRY_COOLDOWN_UNTIL)
            return 0
        with tasks._RANKING_ENTRY_LOCK:
            tasks._RANKING_ENTRY_TIMEOUT_STREAK = 0
            tasks._RANKING_ENTRY_COOLDOWN_UNTIL = None
        created = int(created_ret or 0)
        logger.info("[RANKING ENTRY SCHEDULE] pending build done created=%s before_pending=%s after_pending=%s", created, before_pending, after_pending)
        if created > 0 or after_pending > before_pending or after_pending > 0:
            if created <= 0 and after_pending > 0:
                logger.warning("[RANKING ENTRY SCHEDULE] dispatch existing ranking pending created=0 count=%s symbols=%s", after_pending, _pending_symbols_for_source("RANKING"))
            _dispatch_and_cleanup_ranking(tasks, timeout_sec=tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC, cleanup_reason="RANKING_CONTROLLER_RETURNED_STALE_PENDING")
        else:
            logger.info("[RANKING ENTRY SCHEDULE] no pending created and no ranking pending remains -> controller dispatch skipped")
        final_pending = _pending_count_for_source("RANKING")
        logger.info("[RANKING ENTRY SCHEDULE] done created=%s pending_count=%s final_pending=%s elapsed=%.3fs", created, after_pending, final_pending, time.perf_counter() - started)
        return created
    except Exception:
        logger.exception("[RANKING ENTRY SCHEDULE] failed")
        return 0
    finally:
        with tasks._RANKING_ENTRY_LOCK:
            tasks._RANKING_ENTRY_RUNNING = False
            tasks._RANKING_ENTRY_STARTED_AT = None


def install() -> bool:
    global _INSTALLED, _ORIG_RANKING_SAFE
    try:
        os.environ["RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE"] = "0"
        os.environ["RANKING_ENTRY_BYPASS_PRECHECK_WHEN_PENDING"] = "1"
        os.environ["RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC"] = str(_cap_env_float("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", default=300.0, lower=120.0, upper=300.0, force=True))
        old_runtime_budget = os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC")
        old_max_pending = os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN")
        old_prune = os.environ.get("RANKING_ENTRY_PRUNE_STALE_PENDING_AFTER_DISPATCH")
        os.environ["RANKING_ENTRY_RUNTIME_BUDGET_SEC"] = str(_cap_env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", default=15.0, lower=5.0, upper=15.0, force=True))
        os.environ["RANKING_ENTRY_MAX_PENDING_PER_RUN"] = str(_cap_env_int("RANKING_ENTRY_MAX_PENDING_PER_RUN", default=3, lower=1, upper=3, force=True))
        os.environ["RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC"] = str(_cap_env_float("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", default=12.0, lower=5.0, upper=12.0, force=True))
        os.environ["RANKING_ENTRY_BUILD_TIMEOUT_SEC"] = str(_cap_env_float("RANKING_ENTRY_BUILD_TIMEOUT_SEC", default=18.0, lower=6.0, upper=18.0, force=True))
        os.environ["RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC"] = str(_cap_env_float("RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC", default=15.0, lower=5.0, upper=15.0, force=True))
        os.environ["RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC"] = str(_cap_env_float("RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC", default=30.0, lower=10.0, upper=30.0, force=True))
        os.environ["RANKING_ENTRY_PRUNE_STALE_PENDING_AFTER_DISPATCH"] = "1"
        os.environ.setdefault("RANKING_ENTRY_PRUNE_AFTER_SUCCESSFUL_CONTROLLER", "0")
        os.environ["RANKING_ENTRY_STALE_PENDING_MIN_AGE_SEC"] = str(_cap_env_float("RANKING_ENTRY_STALE_PENDING_MIN_AGE_SEC", default=10.0, lower=3.0, upper=10.0, force=True))
        os.environ["RANKING_ENTRY_POST_DISPATCH_GRACE_SEC"] = str(_cap_env_float("RANKING_ENTRY_POST_DISPATCH_GRACE_SEC", default=0.1, lower=0.0, upper=0.2, force=True))
        import trading.entry_exit.tasks as tasks
        old_controller = float(getattr(tasks, "RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 20.0) or 20.0)
        old_build = float(getattr(tasks, "RANKING_ENTRY_BUILD_TIMEOUT_SEC", 90.0) or 90.0)
        _force_runtime_timeouts(tasks)
        new_controller = float(getattr(tasks, "RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", 12.0) or 12.0)
        new_build = float(getattr(tasks, "RANKING_ENTRY_BUILD_TIMEOUT_SEC", 18.0) or 18.0)
        _patch_entry_controller_precheck()
        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if callable(cur) and not getattr(cur, "_ranking_timeout_dispatch_patch_v19", False):
            _ORIG_RANKING_SAFE = cur
            _patched_run_ranking_entry_safe._ranking_timeout_dispatch_patch_v14 = True  # type: ignore[attr-defined]
            _patched_run_ranking_entry_safe._ranking_timeout_dispatch_patch_v15 = True  # type: ignore[attr-defined]
            _patched_run_ranking_entry_safe._ranking_timeout_dispatch_patch_v16 = True  # type: ignore[attr-defined]
            _patched_run_ranking_entry_safe._ranking_timeout_dispatch_patch_v17 = True  # type: ignore[attr-defined]
            _patched_run_ranking_entry_safe._ranking_timeout_dispatch_patch_v18 = True  # type: ignore[attr-defined]
            _patched_run_ranking_entry_safe._ranking_timeout_dispatch_patch_v19 = True  # type: ignore[attr-defined]
            _patched_run_ranking_entry_safe._original = cur  # type: ignore[attr-defined]
            tasks._run_ranking_entry_safe = _patched_run_ranking_entry_safe
        _INSTALLED = True
        logger.warning(
            "[RANKING ENTRY TIMEOUT PATCH] installed v1.9 controller_timeout %.1f->%.1f build_timeout %.1f->%.1f runtime_budget old=%s new=%s max_pending old=%s new=%s stale_prune old=%s new=%s stale_snapshot_skip=%s precheck_bypass=%s",
            old_controller,
            new_controller,
            old_build,
            new_build,
            old_runtime_budget,
            os.environ.get("RANKING_ENTRY_RUNTIME_BUDGET_SEC"),
            old_max_pending,
            os.environ.get("RANKING_ENTRY_MAX_PENDING_PER_RUN"),
            old_prune,
            os.environ.get("RANKING_ENTRY_PRUNE_STALE_PENDING_AFTER_DISPATCH"),
            os.environ.get("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE"),
            os.environ.get("RANKING_ENTRY_BYPASS_PRECHECK_WHEN_PENDING"),
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY TIMEOUT PATCH] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY TIMEOUT PATCH] auto install failed")

__all__ = ["install"]
