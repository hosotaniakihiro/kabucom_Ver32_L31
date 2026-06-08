# ============================================================
# File   : core/startup/ranking_precheck_pending_failopen_patch.py
# Version: V2.0-RANKING-PENDING-PRECHECK-STRICT
# ------------------------------------------------------------
# 目的:
#   以前は RANKING pending が存在すると stale snapshot precheck を
#   fail-open していた。しかし pending が先に作られてしまうと、
#   db=None/latest=None のまま entry_controller が RANKING を実行する。
#
# 背景ログ:
#   [RANKING PRECHECK PENDING FAILOPEN] OK pending_count=5 -> skip stale snapshot precheck
#   [RANKING PRECHECK OK] type=RANKING_PENDING source=pending_failopen db=None latest=None
#
# 方針 V2:
#   - デフォルトでは fail-open しない。
#   - pending があっても元の precheck を実行する。
#   - 元precheckが明確にOKなら通す。
#   - 元precheck不可かつ明示 env 有効時のみ旧fail-openを許す。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_PRECHECK = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _norm(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _ranking_pending_snapshot() -> tuple[int, dict[str, int]]:
    try:
        from global_state import global_data

        root = getattr(global_data, "pending_entries", None)
        if not isinstance(root, dict):
            return 0, {}

        counts: dict[str, int] = {}
        total = 0
        for sym, bucket in list(root.items()):
            if not isinstance(bucket, (list, tuple)):
                continue
            c = 0
            for e in bucket:
                if not isinstance(e, dict):
                    continue
                src = _norm(e.get("source"))
                et = _norm(e.get("entry_type"))
                if src == "RANKING" or et == "RANKING":
                    c += 1
            if c > 0:
                counts[str(sym)] = c
                total += c
        return total, counts
    except Exception:
        logger.debug("[RANKING PRECHECK PENDING STRICT] pending snapshot failed", exc_info=True)
        return 0, {}


def _pending_ready_result(total: int, counts: dict[str, int]) -> dict[str, Any]:
    return {
        "is_ready": True,
        "explicit_ready": False,
        "derived_ready": True,
        "has_snapshot": False,
        "snapshot_count": total,
        "ranking_type": "RANKING_PENDING",
        "source": "pending_failopen_explicit",
        "reason": "ranking_pending_exists_explicit_failopen",
        "pending_count": total,
        "pending_symbols": counts,
    }


def _not_ready_result(reason: str, total: int, counts: dict[str, int], original: Any = None) -> dict[str, Any]:
    return {
        "is_ready": False,
        "explicit_ready": False,
        "derived_ready": False,
        "has_snapshot": False,
        "snapshot_count": 0,
        "ranking_type": None,
        "source": "pending_strict_patch",
        "reason": reason,
        "pending_count": total,
        "pending_symbols": counts,
        "original": original if isinstance(original, dict) else None,
    }


def _patched_precheck_ranking_entry() -> dict[str, Any]:
    total, counts = _ranking_pending_snapshot()

    # Default: pending does NOT bypass the real ranking snapshot precheck.
    if callable(_ORIGINAL_PRECHECK):
        ret = _ORIGINAL_PRECHECK()
        try:
            ok = bool(ret.get("is_ready") if isinstance(ret, dict) else ret)
        except Exception:
            ok = False
        if ok:
            if total > 0:
                logger.info(
                    "[RANKING PRECHECK PENDING STRICT] original precheck OK with pending_count=%s symbols=%s",
                    total,
                    counts,
                )
            return ret

        if total > 0:
            if _env_bool("RANKING_PRECHECK_PENDING_FAILOPEN_ENABLED", False):
                logger.warning(
                    "[RANKING PRECHECK PENDING STRICT] explicit fail-open enabled pending_count=%s symbols=%s original=%s",
                    total,
                    counts,
                    ret,
                )
                return _pending_ready_result(total, counts)
            logger.warning(
                "[RANKING PRECHECK PENDING STRICT] NG pending exists but snapshot precheck failed -> fail closed pending_count=%s symbols=%s original=%s",
                total,
                counts,
                ret,
            )
            return _not_ready_result("ranking_snapshot_precheck_failed_pending_not_allowed", total, counts, ret)
        return ret if isinstance(ret, dict) else _not_ready_result("original_precheck_false", total, counts, None)

    if total > 0 and _env_bool("RANKING_PRECHECK_PENDING_FAILOPEN_ENABLED", False):
        logger.warning(
            "[RANKING PRECHECK PENDING STRICT] original unavailable but explicit fail-open enabled pending_count=%s symbols=%s",
            total,
            counts,
        )
        return _pending_ready_result(total, counts)

    return _not_ready_result("original_precheck_unavailable", total, counts, None)


def install() -> bool:
    global _INSTALLED, _ORIGINAL_PRECHECK

    if _INSTALLED:
        return True

    try:
        os.environ.setdefault("RANKING_PRECHECK_PENDING_FAILOPEN_ENABLED", "0")

        import trading.handlers.entry_precheck_ranking as precheck_mod

        cur = getattr(precheck_mod, "precheck_ranking_entry", None)
        if not callable(cur):
            logger.warning("[RANKING PRECHECK PENDING STRICT] target precheck not callable")
            return False
        if getattr(cur, "_ranking_precheck_pending_strict_v2", False):
            _INSTALLED = True
            return True

        _ORIGINAL_PRECHECK = getattr(cur, "_original", cur)
        _patched_precheck_ranking_entry._ranking_precheck_pending_strict_v2 = True  # type: ignore[attr-defined]
        _patched_precheck_ranking_entry._original = _ORIGINAL_PRECHECK  # type: ignore[attr-defined]
        precheck_mod.precheck_ranking_entry = _patched_precheck_ranking_entry

        try:
            import trading.handlers.entry_controller as ec
            ec.precheck_ranking_entry = _patched_precheck_ranking_entry
        except Exception:
            pass

        _INSTALLED = True
        logger.warning(
            "[RANKING PRECHECK PENDING STRICT] installed pending_failopen_enabled=%s",
            os.getenv("RANKING_PRECHECK_PENDING_FAILOPEN_ENABLED"),
        )
        return True
    except Exception:
        logger.exception("[RANKING PRECHECK PENDING STRICT] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING PRECHECK PENDING STRICT] auto install failed")


__all__ = ["install"]
