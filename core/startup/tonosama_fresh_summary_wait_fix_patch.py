# ============================================================
# File   : core/startup/tonosama_fresh_summary_wait_fix_patch.py
# Version: v6-NO-STALE-FAILOPEN-DURING-SESSION
# ------------------------------------------------------------
# Purpose:
#   Tonosama 起動前の fresh summary wait が、場中でも古いPUSH summaryを
#   stale fail-open して TONOSAMA 本体へ進めてしまう問題を防ぐ。
#
# Fix:
#   - 09:00-11:30 / 12:30-15:30 以外は即 False。
#   - 12:30:00〜12:33:00 は、latest が12:30未満なら即 False。
#   - 場中でも latest が max_age を超えたら fail-open しない。
#   - latest None の空状態だけは従来どおり環境変数で fail-open 可能。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_INSTALLING = False


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


def _is_market_session_now(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    t = now.time()
    return (dt.time(9, 0) <= t <= dt.time(11, 30)) or (dt.time(12, 30) <= t <= dt.time(15, 30))


def _is_lunch_reopen_grace(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    grace_min = max(0.0, _env_float("TONOSAMA_LUNCH_REOPEN_STALE_SKIP_MIN", 3.0))
    if grace_min <= 0:
        return False
    start = now.replace(hour=12, minute=30, second=0, microsecond=0)
    end = start + dt.timedelta(minutes=grace_min)
    return start <= now < end


def _candidate_dfs() -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    try:
        import core.global_context.context as ctx
        for name, fn in (
            ("module.get_push_merged_summary", lambda: ctx.get_push_merged_summary(1)),
            ("module.get_merged_summary_push", lambda: ctx.get_merged_summary(1, source="push")),
            ("module.get_summary_history_push", lambda: ctx.get_summary_history(1, source="push")),
        ):
            try:
                out.append((name, fn()))
            except Exception:
                logger.debug("[TONOSAMA FRESH SUMMARY WAIT FIX] module provider failed %s", name, exc_info=True)

        gc = getattr(ctx, "global_data", None) or getattr(ctx, "global_context", None) or getattr(ctx, "GC", None)
        if gc is not None:
            for name, fn in (
                ("gc.get_push_merged_summary", lambda: gc.get_push_merged_summary(1)),
                ("gc.get_merged_summary_push", lambda: gc.get_merged_summary(1, source="push")),
                ("gc.get_summary_history_push", lambda: gc.get_summary_history(1, source="push")),
                ("gc.get_push_summary", lambda: gc.get_push_summary(1)),
            ):
                try:
                    out.append((name, fn()))
                except TypeError:
                    try:
                        if "get_merged_summary" in name:
                            out.append((name, gc.get_merged_summary(tf=1, source="push")))
                    except Exception:
                        pass
                except Exception:
                    logger.debug("[TONOSAMA FRESH SUMMARY WAIT FIX] gc provider failed %s", name, exc_info=True)

            try:
                for attr in ("merged_summary_1", "push_summary_1", "push_summary_1m", "push_summary_1min", "summary_1m", "merged_summary_1m"):
                    if hasattr(gc, attr):
                        out.append((f"gc.attr.{attr}", getattr(gc, attr)))
            except Exception:
                pass
    except Exception:
        logger.debug("[TONOSAMA FRESH SUMMARY WAIT FIX] context lookup failed", exc_info=True)

    try:
        from trading.entry.tonosama.summary_loader import load_merged_summary
        out.append(("tonosama.summary_loader.load_merged_summary", load_merged_summary(1)))
    except Exception:
        logger.debug("[TONOSAMA FRESH SUMMARY WAIT FIX] summary_loader fallback failed", exc_info=True)

    return out


def _latest_from_df(df: Any, *, source_name: str) -> tuple[float | None, dt.datetime | None, int, str]:
    try:
        import pandas as pd
        if df is None or not isinstance(df, pd.DataFrame):
            return None, None, 0, source_name
        rows = int(len(df))
        if df.empty:
            return None, None, rows, source_name
        col = None
        for c in ("datetime", "dt", "end_time", "start_time", "time", "snapshot_time"):
            if c in df.columns:
                col = c
                break
        if not col:
            return None, None, rows, source_name
        s = pd.to_datetime(df[col], errors="coerce").dropna()
        if s.empty:
            return None, None, rows, source_name
        latest = s.max().to_pydatetime().replace(tzinfo=None)
        age = (dt.datetime.now() - latest).total_seconds()
        return float(age), latest, rows, f"{source_name}:{col}"
    except Exception:
        logger.debug("[TONOSAMA FRESH SUMMARY WAIT FIX] latest_from_df failed source=%s", source_name, exc_info=True)
        return None, None, 0, source_name


def _patched_latest_push_summary_age_sec():
    best_age = None
    best_dt = None
    best_rows = 0
    best_src = "none"
    for name, df in _candidate_dfs():
        age, latest, rows, src = _latest_from_df(df, source_name=name)
        if latest is None:
            if rows > best_rows:
                best_age, best_dt, best_rows, best_src = age, latest, rows, src
            continue
        if best_dt is None or latest > best_dt:
            best_age, best_dt, best_rows, best_src = age, latest, rows, src
    logger.info(
        "[TONOSAMA FRESH SUMMARY WAIT FIX] latest lookup latest=%s age=%s rows=%s source=%s",
        best_dt,
        None if best_age is None else round(float(best_age), 1),
        best_rows,
        best_src,
    )
    return best_age, best_dt, best_rows


def _latest_is_before_lunch_reopen(latest: dt.datetime | None, now: dt.datetime | None = None) -> bool:
    if latest is None:
        return True
    now = now or dt.datetime.now()
    lunch_open = now.replace(hour=12, minute=30, second=0, microsecond=0)
    return latest < lunch_open


def _patched_wait_fresh_push_summary_before_tonosama() -> bool:
    if not _env_bool("TONOSAMA_WAIT_FRESH_PUSH_SUMMARY", True):
        return True

    now = dt.datetime.now()
    if _env_bool("TONOSAMA_SKIP_WAIT_OUTSIDE_MARKET_SESSION", True) and not _is_market_session_now(now):
        logger.info(
            "[TONOSAMA ENTRY SCHEDULE] fresh push summary wait skipped outside market session now=%s patched=1",
            now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        return False

    max_age = max(30.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_MAX_AGE_SEC", 180.0))
    wait_sec = max(0.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_WAIT_SEC", 15.0))
    poll = max(0.25, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_POLL_SEC", 1.0))
    fail_open_empty = _env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", True)
    deadline = time.perf_counter() + wait_sec
    last_age = None
    last_dt = None
    last_rows = 0

    while True:
        age, latest, rows = _patched_latest_push_summary_age_sec()
        last_age, last_dt, last_rows = age, latest, rows

        if _env_bool("TONOSAMA_SKIP_STALE_DURING_LUNCH_REOPEN", True) and _is_lunch_reopen_grace(now) and _latest_is_before_lunch_reopen(latest, now):
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary skip lunch reopen stale latest=%s age=%s rows=%s grace_min=%.1f patched=1",
                latest,
                None if age is None else round(float(age), 1),
                rows,
                _env_float("TONOSAMA_LUNCH_REOPEN_STALE_SKIP_MIN", 3.0),
            )
            return False

        if age is not None and age <= max_age:
            logger.info(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary ok latest=%s age=%.1fs rows=%s max_age=%.1fs patched=1",
                latest,
                age,
                rows,
                max_age,
            )
            return True

        if latest is None and fail_open_empty:
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary unavailable latest=None rows=%s -> fail-open to Tonosama body patched=1",
                rows,
            )
            return True

        if latest is not None and age is not None and age > max_age:
            if time.perf_counter() >= deadline:
                logger.warning(
                    "[TONOSAMA ENTRY SCHEDULE] fresh push summary stale skip latest=%s age=%.1fs rows=%s max_age=%.1fs wait_sec=%.1fs patched=1 fail_open_stale=0",
                    latest,
                    age,
                    rows,
                    max_age,
                    wait_sec,
                )
                return False
        elif time.perf_counter() >= deadline:
            if last_dt is None and fail_open_empty:
                logger.warning(
                    "[TONOSAMA ENTRY SCHEDULE] fresh push summary wait expired latest=None rows=%s -> fail-open to Tonosama body patched=1",
                    last_rows,
                )
                return True
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary wait expired latest=%s age=%s rows=%s max_age=%.1fs wait_sec=%.1fs -> skip this cycle patched=1",
                last_dt,
                None if last_age is None else round(last_age, 1),
                last_rows,
                max_age,
                wait_sec,
            )
            return False
        time.sleep(poll)


def _apply() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.entry_exit.tasks as tasks
    except Exception:
        logger.debug("[TONOSAMA FRESH SUMMARY WAIT FIX] tasks not ready", exc_info=True)
        return False

    try:
        cur = getattr(tasks, "_wait_fresh_push_summary_before_tonosama", None)
        if getattr(cur, "_tonosama_fresh_summary_wait_fix_v6", False):
            _INSTALLED = True
            return True
        _patched_latest_push_summary_age_sec._tonosama_fresh_summary_wait_fix_v6 = True  # type: ignore[attr-defined]
        _patched_latest_push_summary_age_sec._tonosama_fresh_summary_wait_fix_v5 = True  # type: ignore[attr-defined]
        _patched_latest_push_summary_age_sec._tonosama_fresh_summary_wait_fix_v4 = True  # type: ignore[attr-defined]
        _patched_latest_push_summary_age_sec._tonosama_fresh_summary_wait_fix_v3 = True  # type: ignore[attr-defined]
        _patched_wait_fresh_push_summary_before_tonosama._tonosama_fresh_summary_wait_fix_v6 = True  # type: ignore[attr-defined]
        _patched_wait_fresh_push_summary_before_tonosama._tonosama_fresh_summary_wait_fix_v5 = True  # type: ignore[attr-defined]
        _patched_wait_fresh_push_summary_before_tonosama._tonosama_fresh_summary_wait_fix_v4 = True  # type: ignore[attr-defined]
        _patched_wait_fresh_push_summary_before_tonosama._tonosama_fresh_summary_wait_fix_v3 = True  # type: ignore[attr-defined]
        setattr(tasks, "_latest_push_summary_age_sec", _patched_latest_push_summary_age_sec)
        setattr(tasks, "_wait_fresh_push_summary_before_tonosama", _patched_wait_fresh_push_summary_before_tonosama)
        os.environ.setdefault("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", "1")
        os.environ["TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE"] = "0"
        os.environ.setdefault("TONOSAMA_SKIP_WAIT_OUTSIDE_MARKET_SESSION", "1")
        os.environ.setdefault("TONOSAMA_SKIP_STALE_DURING_LUNCH_REOPEN", "1")
        os.environ.setdefault("TONOSAMA_LUNCH_REOPEN_STALE_SKIP_MIN", "3")
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA FRESH SUMMARY WAIT FIX] installed v6 patched latest+wait fail_open_empty=%s fail_open_stale=%s skip_wait_outside_session=%s skip_lunch_reopen_stale=%s grace_min=%s",
            os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY"),
            os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE"),
            os.environ.get("TONOSAMA_SKIP_WAIT_OUTSIDE_MARKET_SESSION"),
            os.environ.get("TONOSAMA_SKIP_STALE_DURING_LUNCH_REOPEN"),
            os.environ.get("TONOSAMA_LUNCH_REOPEN_STALE_SKIP_MIN"),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA FRESH SUMMARY WAIT FIX] apply failed")
        return False


def install(retry: bool = True) -> bool:
    global _INSTALLING
    if _apply():
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _retry_loop() -> None:
            global _INSTALLING
            try:
                for _ in range(80):
                    if _apply():
                        return
                    time.sleep(0.2)
                logger.warning("[TONOSAMA FRESH SUMMARY WAIT FIX] retry exhausted")
            finally:
                _INSTALLING = False

        import threading
        threading.Thread(target=_retry_loop, name="tonosama-fresh-summary-wait-fix", daemon=True).start()
    return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA FRESH SUMMARY WAIT FIX] auto install failed")


__all__ = ["install"]
