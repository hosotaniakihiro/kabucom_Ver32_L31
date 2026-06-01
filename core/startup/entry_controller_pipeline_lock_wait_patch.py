# ============================================================
# File   : core/startup/entry_controller_pipeline_lock_wait_patch.py
# Version: V2-RANKING-TONOSAMA-ENTRY-CONTROLLER-LOCK-WAIT-DEFER
# ------------------------------------------------------------
# 目的:
#   entry_controller.run_entry_pipeline() は内部で _pipeline_lock を
#   blocking=False で取得するため、別 pipeline 実行中に dispatch されると
#   "ENTRY PIPELINE already running → skip" で即終了する。
#
#   ログでは、RANKING / TONOSAMA の pending は作成済みなのに、
#   lock timeout 後に original を呼び、結局 original 側で skip され、
#   entry_controller_no_order として扱われていた。
#
# 方針:
#   - RANKING / TONOSAMA は entry_controller._pipeline_lock が空くまで待つ
#   - timeout した場合は original を呼ばず、pending を次サイクルへ持ち越す
#   - original を空振り実行して pending があるのに no_order と見える状態を避ける
#
# ENV:
#   ENTRY_CONTROLLER_LOCK_WAIT_ENABLED=1
#   ENTRY_CONTROLLER_LOCK_WAIT_SOURCES=RANKING,TONOSAMA
#   ENTRY_CONTROLLER_LOCK_WAIT_SEC=45
#   ENTRY_CONTROLLER_LOCK_WAIT_POLL_SEC=0.25
#   ENTRY_CONTROLLER_LOCK_WAIT_TIMEOUT_SKIP_ORIGINAL=1
#
# backward compatible:
#   ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED
#   ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC
#   ENTRY_CONTROLLER_RANKING_LOCK_WAIT_POLL_SEC
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


def _env_list(name: str, default: str) -> set[str]:
    try:
        v = os.getenv(name, default)
        return {str(x).strip().upper() for x in str(v).replace(";", ",").split(",") if str(x).strip()}
    except Exception:
        return {str(x).strip().upper() for x in default.split(",") if str(x).strip()}


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


def _pending_snapshot_for_source(source: str) -> dict[str, int]:
    source_u = _normalize_source(source)
    out: dict[str, int] = {}
    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", None)
        if not isinstance(root, dict):
            return out
        for sym, bucket in root.items():
            entries = bucket if isinstance(bucket, (list, tuple, set)) else [bucket]
            n = 0
            for entry in entries:
                if isinstance(entry, dict) and source_u == _normalize_source(entry.get("source")):
                    n += 1
            if n:
                out[str(sym)] = n
    except Exception:
        pass
    return out


def _wait_enabled_for_source(source: str) -> bool:
    source_u = _normalize_source(source)
    if not source_u:
        return False

    # legacy switch still respected
    if source_u == "RANKING" and not _env_bool("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED", True):
        return False

    if not _env_bool("ENTRY_CONTROLLER_LOCK_WAIT_ENABLED", True):
        return False

    return source_u in _env_list("ENTRY_CONTROLLER_LOCK_WAIT_SOURCES", "RANKING,TONOSAMA")


def _timeout_sec() -> float:
    if os.getenv("ENTRY_CONTROLLER_LOCK_WAIT_SEC") is not None:
        return max(0.0, _env_float("ENTRY_CONTROLLER_LOCK_WAIT_SEC", 45.0))
    return max(0.0, _env_float("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC", 45.0))


def _poll_sec() -> float:
    if os.getenv("ENTRY_CONTROLLER_LOCK_WAIT_POLL_SEC") is not None:
        return max(0.05, _env_float("ENTRY_CONTROLLER_LOCK_WAIT_POLL_SEC", 0.25))
    return max(0.05, _env_float("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_POLL_SEC", 0.25))


def _wait_until_entry_lock_free(ec: Any, *, source: str) -> tuple[bool, float, str]:
    source_u = _normalize_source(source)
    if not _wait_enabled_for_source(source_u):
        return False, 0.0, "disabled"

    lock = getattr(ec, "_pipeline_lock", None)
    if lock is None:
        return False, 0.0, "lock_missing"

    timeout = _timeout_sec()
    poll = _poll_sec()
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
                        "[ENTRY CONTROLLER LOCK WAIT] lock free source=%s waited=%.3fs pending=%s snapshot=%s",
                        source_u,
                        waited,
                        _pending_count_for_source(source_u),
                        _pending_snapshot_for_source(source_u),
                    )
                return True, waited, "lock_free"
        except Exception:
            return False, waited, "lock_probe_failed"

        waited = time.perf_counter() - started
        if waited >= timeout:
            logger.warning(
                "[ENTRY CONTROLLER LOCK WAIT] timeout source=%s waited=%.3fs pending=%s snapshot=%s -> defer next cycle",
                source_u,
                waited,
                _pending_count_for_source(source_u),
                _pending_snapshot_for_source(source_u),
            )
            return False, waited, "timeout"

        time.sleep(poll)


def _timeout_result(source: str, waited: float, reason: str) -> dict[str, Any]:
    source_u = _normalize_source(source)
    return {
        "executed": False,
        "approved_count": 0,
        "result": None,
        "skip_reason": "entry_controller_lock_wait_timeout",
        "lock_wait_source": source_u,
        "lock_wait_reason": reason,
        "waited_sec": round(float(waited), 3),
        "pending_count": _pending_count_for_source(source_u),
        "pending_snapshot": _pending_snapshot_for_source(source_u),
        "retry_next_cycle": True,
    }


def _patched_run_entry_pipeline(*args, **kwargs):
    source = kwargs.get("pipeline_source")
    if source is None and args:
        # original は keyword-only だが念のため
        source = None
    source_u = _normalize_source(source)

    try:
        if _wait_enabled_for_source(source_u):
            import trading.handlers.entry_controller as ec

            before = _pending_count_for_source(source_u)
            logger.warning(
                "[ENTRY CONTROLLER LOCK WAIT] dispatch start source=%s pending=%s snapshot=%s",
                source_u,
                before,
                _pending_snapshot_for_source(source_u),
            )
            ok, waited, reason = _wait_until_entry_lock_free(ec, source=source_u)
            if not ok and reason == "timeout" and _env_bool("ENTRY_CONTROLLER_LOCK_WAIT_TIMEOUT_SKIP_ORIGINAL", True):
                # ここで original を呼ぶと original 側で
                # "ENTRY PIPELINE already running → skip" となり、pending があるのに
                # no_order 扱いになる。pending は残して次サイクルへ回す。
                return _timeout_result(source_u, waited, reason)

        return _ORIGINAL_RUN(*args, **kwargs)

    except Exception:
        logger.exception("[ENTRY CONTROLLER LOCK WAIT] patched run_entry_pipeline failed")
        return _ORIGINAL_RUN(*args, **kwargs) if callable(_ORIGINAL_RUN) else None


def install() -> bool:
    global _INSTALLED, _ORIGINAL_RUN

    try:
        import trading.handlers.entry_controller as ec

        cur = getattr(ec, "run_entry_pipeline", None)
        if not callable(cur):
            logger.warning("[ENTRY CONTROLLER LOCK WAIT] target missing")
            return False

        # If an old v1 wrapper is already installed, unwrap it and install v2.
        if getattr(cur, "_entry_controller_lock_wait_patch_v2", False):
            _INSTALLED = True
            return True

        original = getattr(cur, "_original", None) if getattr(cur, "_entry_controller_lock_wait_patch", False) else cur
        if not callable(original):
            original = cur

        _ORIGINAL_RUN = original
        _patched_run_entry_pipeline._entry_controller_lock_wait_patch = True  # type: ignore[attr-defined]
        _patched_run_entry_pipeline._entry_controller_lock_wait_patch_v2 = True  # type: ignore[attr-defined]
        _patched_run_entry_pipeline._original = original  # type: ignore[attr-defined]

        ec.run_entry_pipeline = _patched_run_entry_pipeline
        _INSTALLED = True

        logger.warning(
            "[ENTRY CONTROLLER LOCK WAIT] installed v2 sources=%s wait_sec=%.1f timeout_skip_original=%s",
            sorted(_env_list("ENTRY_CONTROLLER_LOCK_WAIT_SOURCES", "RANKING,TONOSAMA")),
            _timeout_sec(),
            _env_bool("ENTRY_CONTROLLER_LOCK_WAIT_TIMEOUT_SKIP_ORIGINAL", True),
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
