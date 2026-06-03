# ============================================================
# File   : core/startup/entry_controller_pipeline_lock_wait_patch.py
# Version: V4-SUMMARY-AI-LOCK-WAIT-RETRYABLE
# ------------------------------------------------------------
# 目的:
#   entry_controller.run_entry_pipeline() は内部で _pipeline_lock を
#   blocking=False で取得するため、別 pipeline 実行中に dispatch されると
#   "ENTRY PIPELINE already running → skip" で即終了する。
#
# V4:
#   - lock wait 対象に SUMMARY を追加。
#   - Summary AI が approved / pending 登録済みなのに controller lock timeout で
#     注文まで進まないケースを retryable として返す。
#   - 待機時間の既定を 75秒へ延長。Tonosama / Ranking が重い場面でも
#     Summary AI の pending をすぐ捨てない。
#   - _log_skip(symbol, reason, **detail) の reason キー衝突防御は維持。
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_RUN = None
_ORIGINAL_LOG_SKIP = None

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
    if source_u == "RANKING" and not _env_bool("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED", True):
        return False
    if not _env_bool("ENTRY_CONTROLLER_LOCK_WAIT_ENABLED", True):
        return False
    return source_u in _env_list("ENTRY_CONTROLLER_LOCK_WAIT_SOURCES", "RANKING,TONOSAMA,SUMMARY")


def _timeout_sec(source: str = "") -> float:
    source_u = _normalize_source(source)
    if source_u == "SUMMARY":
        if os.getenv("ENTRY_CONTROLLER_SUMMARY_LOCK_WAIT_SEC") is not None:
            return max(0.0, _env_float("ENTRY_CONTROLLER_SUMMARY_LOCK_WAIT_SEC", 75.0))
        return max(0.0, _env_float("ENTRY_CONTROLLER_LOCK_WAIT_SEC", 75.0))
    if os.getenv("ENTRY_CONTROLLER_LOCK_WAIT_SEC") is not None:
        return max(0.0, _env_float("ENTRY_CONTROLLER_LOCK_WAIT_SEC", 75.0))
    return max(0.0, _env_float("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC", 75.0))


def _poll_sec() -> float:
    if os.getenv("ENTRY_CONTROLLER_LOCK_WAIT_POLL_SEC") is not None:
        return max(0.05, _env_float("ENTRY_CONTROLLER_LOCK_WAIT_POLL_SEC", 0.25))
    return max(0.05, _env_float("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_POLL_SEC", 0.25))


def _safe_log_skip(symbol: Any, skip_reason: Any = None, *args, **kwargs):
    """_log_skip(symbol, reason, **detail) の reason キー衝突を完全吸収する。"""
    try:
        if "reason" in kwargs:
            kwargs.setdefault("detail_reason", kwargs.pop("reason"))
        if args:
            kwargs.setdefault("extra_args", args)
        try:
            import trading.handlers.entry_controller as ec
            lg = getattr(ec, "logger", logger)
        except Exception:
            lg = logger
        lg.info("⛔ ENTRY_SKIP %s reason=%s detail=%s", symbol, skip_reason, kwargs)
    except Exception:
        logger.debug("[ENTRY CONTROLLER LOCK WAIT] safe_log_skip failed", exc_info=True)
    return None


def _install_log_skip_final_guard() -> bool:
    global _ORIGINAL_LOG_SKIP
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_log_skip", None)
        if getattr(cur, "_entry_log_skip_final_guard_v4", False):
            return True
        if callable(cur) and _ORIGINAL_LOG_SKIP is None:
            _ORIGINAL_LOG_SKIP = cur
        _safe_log_skip._entry_log_skip_final_guard_v4 = True  # type: ignore[attr-defined]
        _safe_log_skip._entry_log_skip_final_guard_v3 = True  # type: ignore[attr-defined]
        _safe_log_skip._original = cur  # type: ignore[attr-defined]
        ec._log_skip = _safe_log_skip
        return True
    except Exception:
        logger.debug("[ENTRY CONTROLLER LOCK WAIT] install log_skip final guard failed", exc_info=True)
        return False


def _wait_until_entry_lock_free(ec: Any, *, source: str) -> tuple[bool, float, str]:
    source_u = _normalize_source(source)
    if not _wait_enabled_for_source(source_u):
        return False, 0.0, "disabled"
    lock = getattr(ec, "_pipeline_lock", None)
    if lock is None:
        return False, 0.0, "lock_missing"

    timeout = _timeout_sec(source_u)
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
                "[ENTRY CONTROLLER LOCK WAIT] timeout source=%s waited=%.3fs pending=%s snapshot=%s -> retryable defer",
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
        "skip_reason": "entry_controller_lock_timeout_retryable",
        "lock_wait_source": source_u,
        "lock_wait_reason": reason,
        "waited_sec": round(float(waited), 3),
        "pending_count": _pending_count_for_source(source_u),
        "pending_snapshot": _pending_snapshot_for_source(source_u),
        "retryable": True,
        "retry_next_cycle": True,
        "pending_kept": True,
    }


def _patched_run_entry_pipeline(*args, **kwargs):
    source = kwargs.get("pipeline_source")
    if source is None and args:
        source = None
    source_u = _normalize_source(source)

    try:
        _install_log_skip_final_guard()
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
                return _timeout_result(source_u, waited, reason)
        _install_log_skip_final_guard()
        return _ORIGINAL_RUN(*args, **kwargs)
    except TypeError as e:
        if "multiple values for argument 'reason'" in str(e):
            logger.warning(
                "[ENTRY CONTROLLER LOCK WAIT] swallowed _log_skip reason collision source=%s pending=%s snapshot=%s",
                source_u,
                _pending_count_for_source(source_u),
                _pending_snapshot_for_source(source_u),
            )
            _install_log_skip_final_guard()
            return {
                "executed": False,
                "approved_count": 0,
                "result": None,
                "skip_reason": "log_skip_reason_collision_swallowed",
                "lock_wait_source": source_u,
                "pending_count": _pending_count_for_source(source_u),
                "pending_snapshot": _pending_snapshot_for_source(source_u),
                "retryable": True,
                "retry_next_cycle": True,
                "pending_kept": True,
            }
        logger.exception("[ENTRY CONTROLLER LOCK WAIT] patched run_entry_pipeline failed")
        return None
    except Exception:
        logger.exception("[ENTRY CONTROLLER LOCK WAIT] patched run_entry_pipeline failed")
        return None


def install() -> bool:
    global _INSTALLED, _ORIGINAL_RUN
    try:
        import trading.handlers.entry_controller as ec
        _install_log_skip_final_guard()
        cur = getattr(ec, "run_entry_pipeline", None)
        if not callable(cur):
            logger.warning("[ENTRY CONTROLLER LOCK WAIT] target missing")
            return False
        if getattr(cur, "_entry_controller_lock_wait_patch_v4", False):
            _INSTALLED = True
            return True
        original = getattr(cur, "_original", None) if getattr(cur, "_entry_controller_lock_wait_patch", False) else cur
        if not callable(original):
            original = cur
        _ORIGINAL_RUN = original
        _patched_run_entry_pipeline._entry_controller_lock_wait_patch = True  # type: ignore[attr-defined]
        _patched_run_entry_pipeline._entry_controller_lock_wait_patch_v2 = True  # type: ignore[attr-defined]
        _patched_run_entry_pipeline._entry_controller_lock_wait_patch_v3 = True  # type: ignore[attr-defined]
        _patched_run_entry_pipeline._entry_controller_lock_wait_patch_v4 = True  # type: ignore[attr-defined]
        _patched_run_entry_pipeline._original = original  # type: ignore[attr-defined]
        ec.run_entry_pipeline = _patched_run_entry_pipeline
        _INSTALLED = True
        logger.warning(
            "[ENTRY CONTROLLER LOCK WAIT] installed v4 sources=%s wait_sec=%.1f summary_wait_sec=%.1f timeout_skip_original=%s log_skip_final_guard=True",
            sorted(_env_list("ENTRY_CONTROLLER_LOCK_WAIT_SOURCES", "RANKING,TONOSAMA,SUMMARY")),
            _timeout_sec("RANKING"),
            _timeout_sec("SUMMARY"),
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
