# ============================================================
# File   : core/startup/summary_no_overlap_runtime_patch.py
# Version: Ver02-SUMMARY-NO-OVERLAP-GUARD-STALE-INFLIGHT-CLEAR
# ------------------------------------------------------------
# Purpose:
#   summary parent tick / unified runner が前回処理を残したまま
#   次の1分tickで重複起動するのを防ぐ runtime patch。
#
# 背景:
#   ログ例:
#     [SUMMARY PARALLEL] tick timeout ... timeout=55.0s done=0 total=1
#     fallback_state={'running': True, 'reason': 'previous_unified_bg_still_running'}
#     [summary_controller] duplicate diff_update skipped ... holder_held=109s
#
#   この状態で次tickを開始すると、DB読み書き・summary計算・AI候補生成が
#   重なり、3分/5分サマリー空、tonosama timeout、entry遅延につながる。
#
# 方針:
#   - scheduler_jobs.summary.time_locked_runner / runners / scheduler の
#     run_time_locked_summary_jobs をまとめてラップする。
#   - 前回summaryがまだ実行中なら即returnし、次tickへ重ねない。
#   - 古い summary_controller._SUMMARY_INFLIGHT ロックは自動解除する。
#   - デフォルトで summary parallel の負荷も下げる。
#
# ENV:
#   SUMMARY_NO_OVERLAP_ENABLED=1
#   SUMMARY_NO_OVERLAP_MAX_SEC=70
#   SUMMARY_CONTROLLER_INFLIGHT_STALE_CLEAR_SEC=45
#   SUMMARY_PARALLEL_INTERVAL_WORKERS=1       # 未設定時のみ
#   SUMMARY_PARALLEL_RANKING_ENABLED=0        # 未設定時のみ
#   SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC=25  # 未設定時のみ
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from functools import wraps
from typing import Any, Callable

logger = logging.getLogger(__name__)

_INSTALLED = False
_LOCK = threading.RLock()
_RUNNING = False
_STARTED_AT = 0.0
_RUNNING_DETAIL: dict[str, Any] = {}
_ORIGINAL: Callable[..., Any] | None = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return max(1.0, float(v))
    except Exception:
        return float(default)


def _set_default_env() -> None:
    """未設定時だけ、安全側のデフォルト値を入れる。"""
    defaults = {
        "SUMMARY_PARALLEL_INTERVAL_WORKERS": "1",
        "SUMMARY_PARALLEL_RANKING_ENABLED": "0",
        "SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC": "25",
        "SUMMARY_PARALLEL_SKIP_IF_ANY_RUNNING": "1",
        "SUMMARY_CONTROLLER_INFLIGHT_STALE_CLEAR_SEC": "45",
    }
    for k, v in defaults.items():
        if os.getenv(k) is None or str(os.getenv(k)).strip() == "":
            os.environ[k] = v
            logger.warning("[SUMMARY NO OVERLAP] default env set %s=%s", k, v)


def _patch_summary_controller_inflight_stale_clear() -> bool:
    """Clear stale per-interval diff_update inflight locks left by timed-out worker threads."""
    try:
        import trading.summary.summary_controller as sc

        old_enter = getattr(sc, "_enter_interval", None)
        if not callable(old_enter):
            logger.warning("[SUMMARY NO OVERLAP] summary_controller._enter_interval missing")
            return False
        if getattr(old_enter, "_summary_inflight_stale_clear_v2", False):
            return True

        guard = getattr(sc, "_SUMMARY_INFLIGHT_GUARD", None)
        inflight = getattr(sc, "_SUMMARY_INFLIGHT", None)
        if guard is None or not isinstance(inflight, dict):
            logger.warning("[SUMMARY NO OVERLAP] summary_controller inflight state missing")
            return False

        def _patched_enter_interval(interval: int) -> bool:
            try:
                interval_i = int(interval)
            except Exception:
                interval_i = interval
            stale_sec = _env_float("SUMMARY_CONTROLLER_INFLIGHT_STALE_CLEAR_SEC", 45.0)
            try:
                now = dt.datetime.now()
                with guard:
                    meta = inflight.get(interval_i)
                    if meta and bool(meta.get("running", False)):
                        started_at = meta.get("started_at")
                        held_sec = 0.0
                        try:
                            if isinstance(started_at, dt.datetime):
                                held_sec = max(0.0, (now - started_at).total_seconds())
                        except Exception:
                            held_sec = 0.0
                        if held_sec >= stale_sec:
                            logger.error(
                                "[SUMMARY NO OVERLAP][INFLIGHT STALE CLEAR] interval=%s held=%.3fs stale_sec=%.1f holder_tid=%s holder_thread=%s -> clear stale running flag",
                                interval_i,
                                held_sec,
                                stale_sec,
                                meta.get("tid"),
                                meta.get("thread"),
                            )
                            inflight[interval_i] = {"running": False, "started_at": None, "tid": None, "thread": None}
            except Exception:
                logger.exception("[SUMMARY NO OVERLAP][INFLIGHT STALE CLEAR] pre-clear failed interval=%s", interval)
            return old_enter(interval)

        _patched_enter_interval._summary_inflight_stale_clear_v2 = True  # type: ignore[attr-defined]
        _patched_enter_interval._original = old_enter  # type: ignore[attr-defined]
        sc._enter_interval = _patched_enter_interval
        logger.warning(
            "[SUMMARY NO OVERLAP] summary_controller inflight stale clear installed stale_sec=%.1f",
            _env_float("SUMMARY_CONTROLLER_INFLIGHT_STALE_CLEAR_SEC", 45.0),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY NO OVERLAP] summary_controller inflight stale clear install failed")
        return False


def _make_wrapper(original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def wrapped_run_time_locked_summary_jobs(*args: Any, **kwargs: Any) -> Any:
        global _RUNNING, _STARTED_AT, _RUNNING_DETAIL

        if not _env_bool("SUMMARY_NO_OVERLAP_ENABLED", True):
            return original(*args, **kwargs)

        max_sec = _env_float("SUMMARY_NO_OVERLAP_MAX_SEC", 70.0)
        now_arg = kwargs.get("now", None)
        detail = {
            "now": str(now_arg),
            "run_push": kwargs.get("run_push", None),
            "run_ranking": kwargs.get("run_ranking", None),
            "display": kwargs.get("display", None),
            "run_entry": kwargs.get("run_entry", None),
        }

        with _LOCK:
            if _RUNNING:
                elapsed = time.perf_counter() - _STARTED_AT if _STARTED_AT else 0.0
                if elapsed < max_sec:
                    logger.warning(
                        "[SUMMARY NO OVERLAP] skip new summary tick because previous still running elapsed=%.3fs max_sec=%.1f previous=%s new=%s",
                        elapsed,
                        max_sec,
                        _RUNNING_DETAIL,
                        detail,
                    )
                    return {"push": {}, "ranking": {}, "skipped": True, "reason": "previous_summary_still_running"}

                logger.error(
                    "[SUMMARY NO OVERLAP] stale running flag forcibly cleared elapsed=%.3fs max_sec=%.1f previous=%s",
                    elapsed,
                    max_sec,
                    _RUNNING_DETAIL,
                )

            _RUNNING = True
            _STARTED_AT = time.perf_counter()
            _RUNNING_DETAIL = detail

        try:
            return original(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - _STARTED_AT if _STARTED_AT else 0.0
            with _LOCK:
                _RUNNING = False
                _STARTED_AT = 0.0
                _RUNNING_DETAIL = {}
            logger.info("[SUMMARY NO OVERLAP] summary tick released elapsed=%.3fs", elapsed)

    wrapped_run_time_locked_summary_jobs._summary_no_overlap_v1 = True  # type: ignore[attr-defined]
    wrapped_run_time_locked_summary_jobs._original = original  # type: ignore[attr-defined]
    return wrapped_run_time_locked_summary_jobs


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        _patch_summary_controller_inflight_stale_clear()
        logger.warning("[SUMMARY NO OVERLAP] already installed")
        return True

    try:
        _set_default_env()
        _patch_summary_controller_inflight_stale_clear()

        # 先に parallel patch があるなら読み込ませる。無ければ無視。
        try:
            import core.startup.summary_parallel_intervals_runtime_patch as sp
            if hasattr(sp, "install"):
                sp.install()
        except Exception:
            logger.debug("[SUMMARY NO OVERLAP] summary_parallel install skipped/failed", exc_info=True)

        import scheduler_jobs.summary.time_locked_runner as tlr
        import scheduler_jobs.summary.runners as runners
        import scheduler_jobs.summary.scheduler as scheduler

        cur = getattr(tlr, "run_time_locked_summary_jobs", None)
        if not callable(cur):
            logger.error("[SUMMARY NO OVERLAP] run_time_locked_summary_jobs not callable")
            return False
        if getattr(cur, "_summary_no_overlap_v1", False):
            _INSTALLED = True
            return True

        _ORIGINAL = cur
        wrapped = _make_wrapper(cur)

        tlr.run_time_locked_summary_jobs = wrapped
        runners.run_time_locked_summary_jobs = wrapped
        scheduler.run_time_locked_summary_jobs = wrapped

        _INSTALLED = True
        logger.warning(
            "[SUMMARY NO OVERLAP] installed enabled=%s max_sec=%.1f workers=%s ranking_parallel=%s timeout=%s inflight_stale_sec=%.1f",
            _env_bool("SUMMARY_NO_OVERLAP_ENABLED", True),
            _env_float("SUMMARY_NO_OVERLAP_MAX_SEC", 70.0),
            os.getenv("SUMMARY_PARALLEL_INTERVAL_WORKERS"),
            os.getenv("SUMMARY_PARALLEL_RANKING_ENABLED"),
            os.getenv("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"),
            _env_float("SUMMARY_CONTROLLER_INFLIGHT_STALE_CLEAR_SEC", 45.0),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY NO OVERLAP] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY NO OVERLAP] auto install failed")


__all__ = ["install"]
