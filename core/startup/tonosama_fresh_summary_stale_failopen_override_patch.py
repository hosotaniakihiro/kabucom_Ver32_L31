# ============================================================
# File   : core/startup/tonosama_fresh_summary_stale_failopen_override_patch.py
# Version: V1-TONOSAMA-STALE-SUMMARY-SESSION-FAILOPEN
# ------------------------------------------------------------
# Purpose:
#   core.startup.tonosama_fresh_summary_wait_fix_patch v6 intentionally
#   fail-closes stale PUSH summary during session:
#
#     fresh push summary stale skip ... fail_open_stale=0
#
#   In live operation, PUSH can still be receiving raw ticks while merged
#   summary cache lags several minutes. Tonosama already has body-side guards
#   for liquidity/range/ATR/final safety, so do not stop before candidate
#   creation only because summary cache is stale.
#
# Behavior:
#   - outside market session: return False
#   - lunch reopen first N minutes: keep stale-before-12:30 blocked
#   - fresh summary: return True
#   - empty summary: fail-open if enabled
#   - stale summary during session: fail-open by default within grace seconds
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
            logger.debug("[TONOSAMA STALE SUMMARY FAILOPEN] tasks latest lookup failed", exc_info=True)
    return None, None, 0


def _raw_push_alive(max_age_sec: float) -> tuple[bool, str]:
    try:
        from trading.push.push_stream import state as push_state
        v = getattr(push_state, "_last_message_at", None)
        if isinstance(v, dt.datetime):
            age = max(0.0, (dt.datetime.now() - v.replace(tzinfo=None)).total_seconds())
            return age <= max_age_sec, f"raw_push_age={age:.1f}s max={max_age_sec:.1f}s"
    except Exception:
        pass
    return False, "raw_push_age=unknown"


def _patched_wait_fresh_push_summary_before_tonosama() -> bool:
    if not _env_bool("TONOSAMA_WAIT_FRESH_PUSH_SUMMARY", True):
        return True

    now = dt.datetime.now()
    if _env_bool("TONOSAMA_SKIP_WAIT_OUTSIDE_MARKET_SESSION", True) and not _is_market_session(now):
        logger.info(
            "[TONOSAMA ENTRY SCHEDULE] fresh push summary wait skipped outside market session now=%s stale_failopen_override=1",
            now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        return False

    try:
        import trading.entry_exit.tasks as tasks
    except Exception:
        logger.debug("[TONOSAMA STALE SUMMARY FAILOPEN] tasks import failed", exc_info=True)
        return True

    max_age = max(30.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_MAX_AGE_SEC", 180.0))
    wait_sec = max(0.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_WAIT_SEC", 15.0))
    poll = max(0.25, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_POLL_SEC", 1.0))
    fail_open_empty = _env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", True)
    fail_open_stale = _env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE", True)
    stale_grace = max(max_age, _env_float("TONOSAMA_WAIT_STALE_SUMMARY_FAIL_OPEN_GRACE_SEC", 900.0))
    raw_max_age = max(5.0, _env_float("TONOSAMA_WAIT_RAW_PUSH_MAX_AGE_SEC", 90.0))
    deadline = time.perf_counter() + wait_sec

    last_age = None
    last_dt = None
    last_rows = 0

    while True:
        age, latest, rows = _latest_push_summary_age_sec_from_tasks(tasks)
        last_age, last_dt, last_rows = age, latest, rows

        if _env_bool("TONOSAMA_SKIP_STALE_DURING_LUNCH_REOPEN", True) and _is_lunch_reopen_grace(now) and _latest_before_lunch_reopen(latest, now):
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary skip lunch reopen stale latest=%s age=%s rows=%s stale_failopen_override=1",
                latest,
                None if age is None else round(float(age), 1),
                rows,
            )
            return False

        if age is not None and age <= max_age:
            logger.info(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary ok latest=%s age=%.1fs rows=%s max_age=%.1fs stale_failopen_override=1",
                latest,
                age,
                rows,
                max_age,
            )
            return True

        if latest is None and fail_open_empty:
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary unavailable latest=None rows=%s -> fail-open to Tonosama body stale_failopen_override=1",
                rows,
            )
            return True

        if time.perf_counter() >= deadline:
            if latest is not None and age is not None and fail_open_stale:
                raw_alive, raw_detail = _raw_push_alive(raw_max_age)
                if age <= stale_grace or raw_alive:
                    logger.warning(
                        "[TONOSAMA ENTRY SCHEDULE] fresh push summary stale latest=%s age=%.1fs rows=%s max_age=%.1fs grace=%.1fs %s -> fail-open to Tonosama body stale_failopen_override=1",
                        latest,
                        age,
                        rows,
                        max_age,
                        stale_grace,
                        raw_detail,
                    )
                    return True
                logger.warning(
                    "[TONOSAMA ENTRY SCHEDULE] fresh push summary stale too old latest=%s age=%.1fs rows=%s max_age=%.1fs grace=%.1fs %s -> skip stale_failopen_override=1",
                    latest,
                    age,
                    rows,
                    max_age,
                    stale_grace,
                    raw_detail,
                )
                return False

            if last_dt is None and fail_open_empty:
                logger.warning(
                    "[TONOSAMA ENTRY SCHEDULE] fresh push summary wait expired latest=None rows=%s -> fail-open stale_failopen_override=1",
                    last_rows,
                )
                return True

            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] fresh push summary wait expired latest=%s age=%s rows=%s max_age=%.1fs wait_sec=%.1fs -> skip stale_failopen_override=1 fail_open_stale=%s",
                last_dt,
                None if last_age is None else round(float(last_age), 1),
                last_rows,
                max_age,
                wait_sec,
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
        if getattr(cur, "_tonosama_stale_summary_failopen_override_v1", False):
            _INSTALLED = True
            return True
        _patched_wait_fresh_push_summary_before_tonosama._tonosama_stale_summary_failopen_override_v1 = True  # type: ignore[attr-defined]
        _patched_wait_fresh_push_summary_before_tonosama._original = cur  # type: ignore[attr-defined]
        tasks._wait_fresh_push_summary_before_tonosama = _patched_wait_fresh_push_summary_before_tonosama
        os.environ.setdefault("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", "1")
        os.environ["TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE"] = "1"
        os.environ.setdefault("TONOSAMA_WAIT_STALE_SUMMARY_FAIL_OPEN_GRACE_SEC", "900")
        os.environ.setdefault("TONOSAMA_WAIT_RAW_PUSH_MAX_AGE_SEC", "90")
        _INSTALLED = True
        logger.warning(
            "[TONOSAMA STALE SUMMARY FAILOPEN] installed v1 fail_open_stale=%s grace=%s raw_max_age=%s",
            os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE"),
            os.environ.get("TONOSAMA_WAIT_STALE_SUMMARY_FAIL_OPEN_GRACE_SEC"),
            os.environ.get("TONOSAMA_WAIT_RAW_PUSH_MAX_AGE_SEC"),
        )
        return True
    except Exception:
        logger.debug("[TONOSAMA STALE SUMMARY FAILOPEN] apply failed", exc_info=True)
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
                logger.warning("[TONOSAMA STALE SUMMARY FAILOPEN] retry exhausted")
            finally:
                _INSTALLING = False

        threading.Thread(target=_retry_loop, name="tonosama-stale-summary-failopen", daemon=True).start()
    return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA STALE SUMMARY FAILOPEN] auto install failed")


__all__ = ["install"]
