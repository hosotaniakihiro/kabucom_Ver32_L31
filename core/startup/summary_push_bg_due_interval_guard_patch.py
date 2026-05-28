# ============================================================
# File   : core/startup/summary_push_bg_due_interval_guard_patch.py
# Version: V1-DUE-ONLY-PUSH-BG-INTERVAL-GUARD
# ------------------------------------------------------------
# 目的:
#   main.py(entry_only) で PUSH 1m/3m/5m をBG実行する際、
#   3分足・5分足を毎分投入しないようにする。
#
# 背景:
#   2026-05-28 09:48ログで、5分PUSH BGが elapsed=606s まで
#   長時間残っていた。親tickは軽いが、5分足を毎分BG投入すると
#   同じ長足ジョブが積み上がり、CPU/DB/GC/表示処理を圧迫する。
#
# 方針:
#   - 1分足は毎分BG可
#   - 3分足は minute % 3 == 0 の時だけBG投入
#   - 5分足は minute % 5 == 0 の時だけBG投入
#   - 同じ interval のBGがまだ実行中なら、次の同intervalは投入しない
#   - stale秒を超えた実行中フラグは解除して再投入を許可
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_SUBMIT = None
_RUNNING_BY_INTERVAL: dict[int, tuple[float, str]] = {}
_LOCK = threading.RLock()


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return max(1.0, float(raw))
    except Exception:
        return float(default)


def _is_due(interval: int, now: dt.datetime) -> bool:
    try:
        iv = int(interval)
        if iv <= 1:
            return True
        return int(now.minute) % iv == 0
    except Exception:
        return True


def _patched_submit_bg_push_interval(*, interval: int, now: dt.datetime, display: bool, run_entry: bool) -> None:
    """summary_parallel_intervals_runtime_patch._submit_bg_push_interval replacement."""
    try:
        import core.startup.summary_parallel_intervals_runtime_patch as sp
    except Exception:
        logger.exception("[SUMMARY PUSH BG DUE GUARD] import summary_parallel failed")
        if callable(_ORIGINAL_SUBMIT):
            return _ORIGINAL_SUBMIT(interval=interval, now=now, display=display, run_entry=run_entry)
        return None

    iv = int(interval)
    due_only = _env_bool("SUMMARY_PUSH_BG_LONG_INTERVAL_DUE_ONLY", True)
    if due_only and iv in (3, 5) and not _is_due(iv, now):
        logger.warning(
            "[SUMMARY PUSH BG DUE GUARD] skipped reason=not_due interval=%s now=%s minute=%s",
            iv,
            now,
            getattr(now, "minute", None),
        )
        return None

    stale_sec = _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC", 240.0)
    now_ts = time.time()
    with _LOCK:
        old = _RUNNING_BY_INTERVAL.get(iv)
        if old:
            started, key_old = old
            elapsed = now_ts - float(started)
            if elapsed < stale_sec:
                logger.warning(
                    "[SUMMARY PUSH BG DUE GUARD] skipped reason=interval_still_running interval=%s elapsed=%.1fs stale_sec=%.1fs key=%s",
                    iv,
                    elapsed,
                    stale_sec,
                    key_old,
                )
                return None
            logger.warning(
                "[SUMMARY PUSH BG DUE GUARD] stale interval running cleared interval=%s elapsed=%.1fs stale_sec=%.1fs key=%s",
                iv,
                elapsed,
                stale_sec,
                key_old,
            )
            _RUNNING_BY_INTERVAL.pop(iv, None)

        bg_key = f"{now.strftime('%Y%m%d%H%M')}:push:{iv}"
        _RUNNING_BY_INTERVAL[iv] = (now_ts, bg_key)

    def _task() -> None:
        try:
            logger.warning(
                "[SUMMARY PUSH BG DUE GUARD] bg push start key=%s interval=%s now=%s display=%s run_entry=%s",
                bg_key,
                iv,
                now,
                display,
                run_entry,
            )
            sp._job_one_source(source="push", interval=iv, now=now, display=display, run_entry=run_entry)
        except Exception:
            logger.exception("[SUMMARY PUSH BG DUE GUARD] bg push failed key=%s interval=%s", bg_key, iv)
        finally:
            with _LOCK:
                cur = _RUNNING_BY_INTERVAL.get(iv)
                if cur and cur[1] == bg_key:
                    _RUNNING_BY_INTERVAL.pop(iv, None)
            logger.warning("[SUMMARY PUSH BG DUE GUARD] bg push done key=%s interval=%s now=%s", bg_key, iv, now)

    try:
        sp._bg_executor().submit(_task)
        logger.warning("[SUMMARY PUSH BG DUE GUARD] bg push submitted key=%s interval=%s now=%s", bg_key, iv, now)
    except Exception:
        with _LOCK:
            cur = _RUNNING_BY_INTERVAL.get(iv)
            if cur and cur[1] == bg_key:
                _RUNNING_BY_INTERVAL.pop(iv, None)
        logger.exception("[SUMMARY PUSH BG DUE GUARD] submit failed key=%s interval=%s", bg_key, iv)


def install() -> bool:
    global _PATCHED, _ORIGINAL_SUBMIT
    if _PATCHED:
        return True
    try:
        import core.startup.summary_parallel_intervals_runtime_patch as sp
        cur = getattr(sp, "_submit_bg_push_interval", None)
        if getattr(cur, "_summary_push_bg_due_guard", False):
            _PATCHED = True
            return True
        _ORIGINAL_SUBMIT = cur
        _patched_submit_bg_push_interval._summary_push_bg_due_guard = True  # type: ignore[attr-defined]
        sp._submit_bg_push_interval = _patched_submit_bg_push_interval
        _PATCHED = True
        logger.warning(
            "[SUMMARY PUSH BG DUE GUARD] installed due_only=%s stale_sec=%.1f original=%s",
            _env_bool("SUMMARY_PUSH_BG_LONG_INTERVAL_DUE_ONLY", True),
            _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC", 240.0),
            getattr(cur, "__name__", str(cur)),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY PUSH BG DUE GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY PUSH BG DUE GUARD] auto install failed")


__all__ = ["install"]
