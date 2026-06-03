# ============================================================
# File   : core/startup/tonosama_fresh_summary_stale_failopen_override_patch.py
# Version: V2-TONOSAMA-STALE-SUMMARY-HISTORY-FIRST
# ------------------------------------------------------------
# Purpose:
#   古いPUSH merged summary のまま Tonosama body へ fail-open しない。
#
# Background:
#   2026-06-03 09:14ログで、raw PUSH は生きているが merged summary は
#   latest=09:00 / age=856s のままになり、このpatch V1 が stale fail-open して
#   Tonosama 本体が古いサマリーで動いていた。
#
# Behavior:
#   - outside market session: return False
#   - lunch reopen first N minutes: keep stale-before-12:30 blocked
#   - fresh summary: return True
#   - empty summary: fail-open only if explicitly enabled
#   - stale summary during session: do NOT fail-open by default
#   - stale summary is only allowed when summary_loader/history has refreshed enough
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
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


def _is_market_session(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    t = now.time()
    return (dt.time(9, 0) <= t < dt.time(11, 30)) or (dt.time(12, 30) <= t < dt.time(15, 30))


def _is_lunch_reopen_grace(now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now()
    grace_min = max(0.0, _env_float("TONOSAMA_LUNCH_REOPEN_STALE_SKIP_MIN", 3.0))
    if grace_min <= 0:
        return False
    start = now.replace(hour=12, minute=30, second=0, microsecond=0)
    return start <= now < start + dt.timedelta(minutes=grace_min)


def _latest_before_lunch_reopen(latest: dt.datetime | None, now: dt.datetime | None = None) -> bool:
    if latest is None:
        return True
    now = now or dt.datetime.now()
    return latest < now.replace(hour=12, minute=30, second=0, microsecond=0)


def _latest_push_summary_age_sec_from_tasks(tasks: Any) -> tuple[float | None, dt.datetime | None, int]:
    fn = getattr(tasks, "_latest_push_summary_age_sec", None)
    if callable(fn):
        try:
            age, latest, rows = fn()
            return age, latest, int(rows or 0)
        except Exception:
            logger.debug("[TONOSAMA STALE SUMMARY HISTORY FIRST] tasks latest lookup failed", exc_info=True)
    return None, None, 0


def _patched_wait_fresh_push_summary_before_tonosama() -> bool:
    if not _env_bool("TONOSAMA_WAIT_FRESH_PUSH_SUMMARY", True):
        return True

    now = dt.datetime.now()
    if _env_bool("TONOSAMA_SKIP_WAIT_OUTSIDE_MARKET_SESSION", True) and not _is_market_session(now):
        logger.info(
            "[TONOSAMA ENTRY SCHEDULE] fresh push summary wait skipped outside market session now=%s stale_history_first=1",
            now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        return False

    try:
        import trading.entry_exit.tasks as tasks
    except Exception:
        logger.debug("[TONOSAMA STALE SUMMARY HISTORY FIRST] tasks import failed", exc_info=True)
        return False

    max_age = max(30.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_MAX_AGE_SEC", 180.0))
    wait_sec = max(0.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_WAIT_SEC", 3.0))
    poll = max(0.25, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_POLL_SEC", 1.0))
    fail_open_empty = _env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", False)
    fail_open_stale = _env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE", False)
    deadline = time.perf_counter() + wait_sec

    last_age = None
    last_dt = None
    last_rows = 0

    while True:
        age, latest, rows = _latest_push_summary_age_sec_from_tasks(tasks)
        last_age, last_dt, last_rows = age, latest, rows

        if _env_bool("TONOSAMA_SKIP_STALE_DURING_LUNCH_REOPEN", True) and _is_lunch_reopen_grace(now) and _latest_before_lunch_reopen(latest, now):
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary skip lunch reopen stale latest=%s age=%s rows=%s stale_history_first=1",
                latest,
                None if age is None else round(float(age), 1),
                rows,
            )
            return False

        if age is not None and age <= max_age:
            logger.info(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary ok latest=%s age=%.1fs rows=%s max_age=%.1fs stale_history_first=1",
                latest,
                age,
                rows,
                max_age,
            )
            return True

        if latest is None and fail_open_empty:
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary unavailable latest=None rows=%s -> fail-open stale_history_first=1 fail_open_empty=1",
                rows,
            )
            return True

        if time.perf_counter() >= deadline:
            if latest is not None and age is not None and fail_open_stale:
                logger.warning(
                    "[TONOSAMA ENTRY SCHEDULE] fresh push summary stale latest=%s age=%.1fs rows=%s max_age=%.1fs -> fail-open stale_history_first=1 explicit_env=1",
                    latest,
                    age,
                    rows,
                    max_age,
                )
                return True

            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary stale/empty -> skip this cycle latest=%s age=%s rows=%s max_age=%.1fs wait_sec=%.1fs fail_open_empty=%s fail_open_stale=%s stale_history_first=1",
                last_dt,
                None if last_age is None else round(float(last_age), 1),
                last_rows,
                max_age,
                wait_sec,
                fail_open_empty,
                fail_open_stale,
            )
            return False

        time.sleep(poll)


def _apply() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, "_wait_fresh_push_summary_before_tonosama", None)
        if getattr(cur, "_tonosama_stale_summary_history_first_v2", False):
            _INSTALLED = True
            return True
        _patched_wait_fresh_push_summary_before_tonosama._tonosama_stale_summary_history_first_v2 = True  # type: ignore[attr-defined]
        _patched_wait_fresh_push_summary_before_tonosama._original = cur  # type: ignore[attr-defined]
        tasks._wait_fresh_push_summary_before_tonosama = _patched_wait_fresh_push_summary_before_tonosama

        # 重要: V1 の stale fail-open を無効化する。必要時のみ環境変数で明示許可。
        os.environ.setdefault("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", "0")
        os.environ["TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE"] = os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE", "0")
        os.environ.setdefault("TONOSAMA_REPLACE_STALE_MERGED_WITH_HISTORY", "1")
        os.environ.setdefault("TONOSAMA_HISTORY_FALLBACK_MAX_AGE_SEC", "240")
        os.environ.setdefault("TONOSAMA_WAIT_PUSH_SUMMARY_WAIT_SEC", "3")

        _INSTALLED = True
        logger.warning(
            "[TONOSAMA STALE SUMMARY HISTORY FIRST] installed v2 fail_open_empty=%s fail_open_stale=%s replace_stale_with_history=%s history_max_age=%s wait_sec=%s",
            os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY"),
            os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE"),
            os.environ.get("TONOSAMA_REPLACE_STALE_MERGED_WITH_HISTORY"),
            os.environ.get("TONOSAMA_HISTORY_FALLBACK_MAX_AGE_SEC"),
            os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_WAIT_SEC"),
        )
        return True
    except Exception:
        logger.debug("[TONOSAMA STALE SUMMARY HISTORY FIRST] apply failed", exc_info=True)
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
                for _ in range(100):
                    if _apply():
                        return
                    time.sleep(0.2)
                logger.warning("[TONOSAMA STALE SUMMARY HISTORY FIRST] retry exhausted")
            finally:
                _INSTALLING = False

        threading.Thread(target=_retry_loop, name="tonosama-stale-summary-history-first", daemon=True).start()
    return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA STALE SUMMARY HISTORY FIRST] auto install failed")


__all__ = ["install"]
