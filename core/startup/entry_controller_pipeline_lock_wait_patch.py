# ============================================================
# File   : core/startup/entry_controller_pipeline_lock_wait_patch.py
# Version: V5-SUMMARY-AI-STALE-PIPELINE-LOCK-RECOVERY
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
#   - 待機時間の既定を75秒へ延長。
#
# V5:
#   - SUMMARY の既定待機を短縮し、35秒 lock_timeout を防ぐ。
#   - timeout 時、entry_execute_timeout_guard の inflight が空なら stale lock と判定。
#   - stale lock 判定時は ec._pipeline_lock を安全側で再生成し、元 pipeline を1回だけ実行。
#   - 古すぎる SUMMARY pending だけ prune。fresh pending は残す。
#
# V6:
#   - _safe_log_skip / _install_log_skip_final_guard を撤去。これは
#     run_entry_pipeline 呼び出しの度に ec._log_skip を「元を一切呼ばない
#     ログのみ版」へ強制上書きしており、entry_log_skip_reason_collision_patch の
#     ランキングpending prune と entry_controller_audit_patch の監査DB記録が
#     恒久的に無効化されていた。両機能は entry_controller._log_skip 本体
#     (Ver2.7) へ直接統合されたため、このガードは不要かつ有害だった。
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
_ORIGINAL_RUN = None
_LAST_STALE_RESET_AT = 0.0

# _log_skip を "元を一切呼ばずログのみ" の _safe_log_skip へ恒久的に強制上書きしていた
# _install_log_skip_final_guard は撤去済み。entry_controller._log_skip 本体 (Ver2.7) が
# reason衝突回避 + 監査ログ記録 + ランキングpending pruneを直接行うため、このガードは
# 逆にそれらの機能を無効化する副作用しかなかった。

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
        return float(str(v).replace(",", ""))
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


def _norm_str(v: Any) -> str:
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


def _parse_dt(v: Any) -> dt.datetime | None:
    try:
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)
        if v is None or str(v).strip() == "":
            return None
        s = str(v).strip()
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return dt.datetime.strptime(s, fmt)
            except Exception:
                continue
    except Exception:
        return None
    return None


def _entry_age_sec(entry: dict[str, Any]) -> float | None:
    if not isinstance(entry, dict):
        return None
    for key in ("created_at", "pending_created_at", "updated_at", "entry_time", "datetime"):
        ts = _parse_dt(entry.get(key))
        if ts is not None:
            try:
                return max(0.0, (dt.datetime.now() - ts).total_seconds())
            except Exception:
                return None
    return None


def _prune_old_pending(source: str, *, max_age_sec: float | None = None, max_remove: int | None = None, reason: str = "lock_timeout_old_pending") -> int:
    source_u = _normalize_source(source)
    if max_age_sec is None:
        if source_u == "SUMMARY":
            max_age_sec = _env_float("ENTRY_CONTROLLER_SUMMARY_PENDING_PRUNE_AGE_SEC", 90.0)
        else:
            max_age_sec = _env_float("ENTRY_CONTROLLER_PENDING_PRUNE_AGE_SEC", 120.0)
    try:
        import trading.entry.pending_manager as pm

        def pred(_sym: str, entry: dict[str, Any]) -> bool:
            if not isinstance(entry, dict):
                return False
            if _normalize_source(entry.get("source")) != source_u:
                return False
            age = _entry_age_sec(entry)
            return age is not None and age >= float(max_age_sec)

        removed = int(pm.prune_entries(pred, reason=reason, max_remove=max_remove))
        if removed:
            logger.warning(
                "[ENTRY CONTROLLER LOCK WAIT] old pending pruned source=%s removed=%s max_age=%.1fs reason=%s snapshot=%s",
                source_u,
                removed,
                float(max_age_sec),
                reason,
                _pending_snapshot_for_source(source_u),
            )
        return removed
    except Exception:
        logger.exception("[ENTRY CONTROLLER LOCK WAIT] old pending prune failed source=%s", source_u)
        return 0


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
            base = _env_float("ENTRY_CONTROLLER_SUMMARY_LOCK_WAIT_SEC", 8.0)
        else:
            base = _env_float("ENTRY_CONTROLLER_LOCK_WAIT_SEC", 8.0)
        cap = _env_float("ENTRY_CONTROLLER_SUMMARY_LOCK_WAIT_MAX_SEC", 8.0)
        return max(0.0, min(float(base), float(cap)))
    if os.getenv("ENTRY_CONTROLLER_LOCK_WAIT_SEC") is not None:
        base = _env_float("ENTRY_CONTROLLER_LOCK_WAIT_SEC", 12.0)
    else:
        base = _env_float("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC", 12.0)
    cap = _env_float("ENTRY_CONTROLLER_LOCK_WAIT_MAX_SEC", 12.0)
    return max(0.0, min(float(base), float(cap)))


def _poll_sec() -> float:
    if os.getenv("ENTRY_CONTROLLER_LOCK_WAIT_POLL_SEC") is not None:
        return max(0.05, _env_float("ENTRY_CONTROLLER_LOCK_WAIT_POLL_SEC", 0.20))
    return max(0.05, _env_float("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_POLL_SEC", 0.20))


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
                "[ENTRY CONTROLLER LOCK WAIT] timeout source=%s waited=%.3fs pending=%s snapshot=%s",
                source_u,
                waited,
                _pending_count_for_source(source_u),
                _pending_snapshot_for_source(source_u),
            )
            return False, waited, "timeout"
        time.sleep(poll)


def _entry_execute_inflight_count() -> int:
    try:
        import core.startup.entry_execute_timeout_guard_patch as eg
        inflight = getattr(eg, "_INFLIGHT", {})
        if isinstance(inflight, dict):
            n = 0
            for info in inflight.values():
                if isinstance(info, dict) and not bool(info.get("done")):
                    n += 1
            return int(n)
    except Exception:
        pass
    return 0


def _can_reset_stale_lock(source: str) -> tuple[bool, str]:
    source_u = _normalize_source(source)
    if source_u != "SUMMARY" and not _env_bool("ENTRY_CONTROLLER_STALE_LOCK_RESET_NON_SUMMARY", False):
        return False, "source_not_allowed"
    if not _env_bool("ENTRY_CONTROLLER_STALE_LOCK_RESET_ENABLED", True):
        return False, "disabled"
    pending = _pending_count_for_source(source_u)
    min_pending = int(_env_float("ENTRY_CONTROLLER_STALE_LOCK_MIN_PENDING", 1.0))
    if pending < min_pending:
        return False, f"pending_low:{pending}<{min_pending}"
    inflight = _entry_execute_inflight_count()
    if inflight > 0:
        return False, f"entry_execute_inflight:{inflight}"
    global _LAST_STALE_RESET_AT
    cooldown = _env_float("ENTRY_CONTROLLER_STALE_LOCK_RESET_COOLDOWN_SEC", 10.0)
    now = time.time()
    if now - float(_LAST_STALE_RESET_AT or 0.0) < cooldown:
        return False, "cooldown"
    return True, "ok"


def _reset_pipeline_lock_if_stale(ec: Any, *, source: str, waited: float) -> bool:
    ok, why = _can_reset_stale_lock(source)
    source_u = _normalize_source(source)
    if not ok:
        logger.warning(
            "[ENTRY CONTROLLER LOCK WAIT] stale lock reset skipped source=%s reason=%s waited=%.3fs pending=%s snapshot=%s",
            source_u,
            why,
            waited,
            _pending_count_for_source(source_u),
            _pending_snapshot_for_source(source_u),
        )
        return False
    try:
        old_lock = getattr(ec, "_pipeline_lock", None)
        # 元が Lock でも RLock でも、新しい RLock で再入性を持たせる。
        ec._pipeline_lock = threading.RLock()
        global _LAST_STALE_RESET_AT
        _LAST_STALE_RESET_AT = time.time()
        logger.warning(
            "[ENTRY CONTROLLER LOCK WAIT] STALE_LOCK_RESET source=%s waited=%.3fs old_lock=%r new_lock=%r pending=%s snapshot=%s",
            source_u,
            waited,
            old_lock,
            getattr(ec, "_pipeline_lock", None),
            _pending_count_for_source(source_u),
            _pending_snapshot_for_source(source_u),
        )
        return True
    except Exception:
        logger.exception("[ENTRY CONTROLLER LOCK WAIT] stale lock reset failed source=%s", source_u)
        return False


def _timeout_result(source: str, waited: float, reason: str, *, stale_reset: bool = False) -> dict[str, Any]:
    source_u = _normalize_source(source)
    return {
        "executed": False,
        "approved_count": 0,
        "result": None,
        "skip_reason": "entry_controller_lock_timeout_stale_reset_failed" if stale_reset else "entry_controller_lock_timeout_retryable",
        "lock_wait_source": source_u,
        "lock_wait_reason": reason,
        "waited_sec": round(float(waited), 3),
        "pending_count": _pending_count_for_source(source_u),
        "pending_snapshot": _pending_snapshot_for_source(source_u),
        "entry_execute_inflight": _entry_execute_inflight_count(),
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
        if _wait_enabled_for_source(source_u):
            import trading.handlers.entry_controller as ec
            before = _pending_count_for_source(source_u)
            logger.warning(
                "[ENTRY CONTROLLER LOCK WAIT] dispatch start source=%s pending=%s snapshot=%s timeout=%.3fs",
                source_u,
                before,
                _pending_snapshot_for_source(source_u),
                _timeout_sec(source_u),
            )
            ok, waited, reason = _wait_until_entry_lock_free(ec, source=source_u)
            if not ok and reason == "timeout":
                _prune_old_pending(source_u, reason="lock_timeout_old_pending")
                if _reset_pipeline_lock_if_stale(ec, source=source_u, waited=waited):
                    logger.warning(
                        "[ENTRY CONTROLLER LOCK WAIT] retry original after stale reset source=%s pending=%s snapshot=%s",
                        source_u,
                        _pending_count_for_source(source_u),
                        _pending_snapshot_for_source(source_u),
                    )
                    return _ORIGINAL_RUN(*args, **kwargs)
                if _env_bool("ENTRY_CONTROLLER_LOCK_WAIT_TIMEOUT_SKIP_ORIGINAL", True):
                    return _timeout_result(source_u, waited, reason, stale_reset=True)
        return _ORIGINAL_RUN(*args, **kwargs)
    except TypeError as e:
        if "multiple values for argument 'reason'" in str(e):
            logger.warning(
                "[ENTRY CONTROLLER LOCK WAIT] swallowed _log_skip reason collision source=%s pending=%s snapshot=%s",
                source_u,
                _pending_count_for_source(source_u),
                _pending_snapshot_for_source(source_u),
            )
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
        cur = getattr(ec, "run_entry_pipeline", None)
        if not callable(cur):
            logger.warning("[ENTRY CONTROLLER LOCK WAIT] target missing")
            return False
        if getattr(cur, "_entry_controller_lock_wait_patch_v5", False):
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
        _patched_run_entry_pipeline._entry_controller_lock_wait_patch_v5 = True  # type: ignore[attr-defined]
        _patched_run_entry_pipeline._original = original  # type: ignore[attr-defined]
        ec.run_entry_pipeline = _patched_run_entry_pipeline
        _INSTALLED = True
        logger.warning(
            "[ENTRY CONTROLLER LOCK WAIT] installed v5 sources=%s wait_sec=%.1f summary_wait_sec=%.1f timeout_skip_original=%s stale_reset=%s log_skip_final_guard=True",
            sorted(_env_list("ENTRY_CONTROLLER_LOCK_WAIT_SOURCES", "RANKING,TONOSAMA,SUMMARY")),
            _timeout_sec("RANKING"),
            _timeout_sec("SUMMARY"),
            _env_bool("ENTRY_CONTROLLER_LOCK_WAIT_TIMEOUT_SKIP_ORIGINAL", True),
            _env_bool("ENTRY_CONTROLLER_STALE_LOCK_RESET_ENABLED", True),
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
