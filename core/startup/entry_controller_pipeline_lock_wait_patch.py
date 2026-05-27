# ============================================================
# File   : core/startup/entry_controller_pipeline_lock_wait_patch.py
# Version: V1-RANKING-ENTRY-CONTROLLER-LOCK-WAIT
# ------------------------------------------------------------
# 目的:
#   SUMMARY entry_controller 実行中に RANKING entry_controller dispatch が来ると、
#   entry_controller.run_entry_pipeline() が _pipeline_lock を取れず
#   "ENTRY PIPELINE already running → skip" で即終了する。
#
#   15:11ログでは、RANKING pending は作成済みだが、直前のSUMMARY pipelineが
#   RANKING pendingを PIPELINE_FILTER_MISMATCH で読み飛ばしており、
#   RANKING側dispatchは lock 空き待ちが必要。
#
# 方針:
#   - RANKING pipeline_source の時だけ、entry_controller._pipeline_lock が空くまで待つ
#   - 待てなければ従来通り original を呼ぶが、ログで分かるようにする
#   - SUMMARY / TONOSAMA は従来通り即時挙動
#
# ENV:
#   ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED=1
#   ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC=35
#   ENTRY_CONTROLLER_RANKING_LOCK_WAIT_POLL_SEC=0.25
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_RUN = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = True) -> bool:
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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _normalize_source(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _pending_count_for_source(source: str) -> int:
    source_u = _normalize_source(source)
    total = 0
    try:
        import trading.entry.pending_manager as pm
        iter_entries = getattr(pm, "iter_entries", None)
        if callable(iter_entries):
            for _sym, entry in list(iter_entries()):
                if isinstance(entry, dict) and source_u == _normalize_source(entry.get("source")):
                    total += 1
            return int(total)
    except Exception:
        pass
    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", None)
        if isinstance(root, dict):
            for bucket in root.values():
                entries = bucket if isinstance(bucket, (list, tuple, set)) else [bucket]
                for entry in entries:
                    if isinstance(entry, dict) and source_u == _normalize_source(entry.get("source")):
                        total += 1
    except Exception:
        pass
    return int(total)


def _wait_until_entry_lock_free(ec: Any, *, source: str) -> bool:
    if not _env_bool("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED", True):
        return False
    if _normalize_source(source) != "RANKING":
        return False

    lock = getattr(ec, "_pipeline_lock", None)
    if lock is None:
        return False

    timeout = max(0.0, _env_float("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC", 35.0))
    poll = max(0.05, _env_float("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_POLL_SEC", 0.25))
    started = time.perf_counter()
    waited = 0.0

    while True:
        try:
            acquired = bool(lock.acquire(blocking=False))
            if acquired:
                try:
                    lock.release()
                except Exception:
                    pass
                if waited > 0:
                    logger.warning(
                        "[ENTRY CONTROLLER LOCK WAIT] lock free source=%s waited=%.3fs pending_ranking=%s",
                        source,
                        waited,
                        _pending_count_for_source("RANKING"),
                    )
                return True
        except Exception:
            return False

        waited = time.perf_counter() - started
        if waited >= timeout:
            logger.warning(
                "[ENTRY CONTROLLER LOCK WAIT] timeout source=%s waited=%.3fs pending_ranking=%s -> call original anyway",
                source,
                waited,
                _pending_count_for_source("RANKING"),
            )
            return False
        time.sleep(poll)


def _patched_run_entry_pipeline(*args, **kwargs):
    try:
        pipeline_source = kwargs.get("pipeline_source")
        if pipeline_source is None and args:
            # original は keyword-only だが念のため
            pipeline_source = None
        if _normalize_source(pipeline_source) == "RANKING":
            try:
                import trading.handlers.entry_controller as ec
                before = _pending_count_for_source("RANKING")
                logger.warning(
                    "[ENTRY CONTROLLER LOCK WAIT] ranking dispatch start pending_ranking=%s",
                    before,
                )
                _wait_until_entry_lock_free(ec, source="RANKING")
            except Exception:
                logger.debug("[ENTRY CONTROLLER LOCK WAIT] prewait failed", exc_info=True)
        return _ORIGINAL_RUN(*args, **kwargs)
    except Exception:
        logger.exception("[ENTRY CONTROLLER LOCK WAIT] patched run_entry_pipeline failed")
        return _ORIGINAL_RUN(*args, **kwargs) if callable(_ORIGINAL_RUN) else None


def install() -> bool:
    global _INSTALLED, _ORIGINAL_RUN
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "run_entry_pipeline", None)
        if not callable(cur):
            logger.warning("[ENTRY CONTROLLER LOCK WAIT] target missing")
            return False
        if getattr(cur, "_entry_controller_lock_wait_patch", False):
            _INSTALLED = True
            return True
        _ORIGINAL_RUN = cur
        _patched_run_entry_pipeline._entry_controller_lock_wait_patch = True  # type: ignore[attr-defined]
        _patched_run_entry_pipeline._original = cur  # type: ignore[attr-defined]
        ec.run_entry_pipeline = _patched_run_entry_pipeline
        _INSTALLED = True
        logger.warning(
            "[ENTRY CONTROLLER LOCK WAIT] installed v1 wait_sec=%.1f",
            _env_float("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC", 35.0),
        )
        return True
    except Exception:
        logger.exception("[ENTRY CONTROLLER LOCK WAIT] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY CONTROLLER LOCK WAIT] auto install failed")


__all__ = ["install"]
