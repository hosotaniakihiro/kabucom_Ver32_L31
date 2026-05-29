# ============================================================
# File   : core/startup/summary_push_bg_due_interval_guard_patch.py
# Version: V2-ONE-MINUTE-STALE-LOCK-SHORTER
# ------------------------------------------------------------
# 目的:
#   main.py(entry_only) で PUSH 1m/3m/5m をBG実行する際、
#   3分足・5分足を毎分投入しないようにしつつ、1分足は詰まらせない。
#
# V2 修正:
#   ✔ 旧版は 1m/3m/5m すべて SUMMARY_PUSH_BG_INTERVAL_STALE_SEC=240秒を使用
#   ✔ 1分足が 115秒以上 stale running でも skipped され、表示されない原因になった
#   ✔ 1分足専用 stale を SUMMARY_PUSH_BG_INTERVAL_STALE_SEC_1M=60秒 に分離
#   ✔ 3分/5分は従来通り 240秒既定
#   ✔ stale clear 後は次の1分足BGを投入する
#
# ログ上の問題例:
#   [SUMMARY PUSH BG DUE GUARD] skipped reason=interval_still_running interval=1 elapsed=115.7s stale_sec=240.0s
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time

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


def _stale_sec_for_interval(interval: int) -> float:
    iv = int(interval)
    if iv == 1:
        # 1分足は毎分表示対象。前回が60秒以上残っていたらstaleとして解除する。
        return _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC_1M", 60.0)
    if iv == 3:
        return _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC_3M", _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC", 240.0))
    if iv == 5:
        return _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC_5M", _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC", 240.0))
    return _env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC", 240.0)


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

    stale_sec = _stale_sec_for_interval(iv)
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
                "[SUMMARY PUSH BG DUE GUARD] bg push start key=%s interval=%s now=%s display=%s run_entry=%s stale_sec=%.1f",
                bg_key,
                iv,
                now,
                display,
                run_entry,
                stale_sec,
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
        logger.warning("[SUMMARY PUSH BG DUE GUARD] bg push submitted key=%s interval=%s now=%s stale_sec=%.1f", bg_key, iv, now, stale_sec)
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
            "[SUMMARY PUSH BG DUE GUARD] installed v2 due_only=%s stale_1m=%.1f stale_3m=%.1f stale_5m=%.1f original=%s",
            _env_bool("SUMMARY_PUSH_BG_LONG_INTERVAL_DUE_ONLY", True),
            _stale_sec_for_interval(1),
            _stale_sec_for_interval(3),
            _stale_sec_for_interval(5),
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
