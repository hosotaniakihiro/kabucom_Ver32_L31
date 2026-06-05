# ============================================================
# File   : core/startup/ranking_precheck_pending_failopen_patch.py
# Version: V1.0-RANKING-PENDING-PRECHECK-FAILOPEN
# ------------------------------------------------------------
# 目的:
#   RANKING pending が既に作成されているのに、entry_controller 側の
#   ranking_snapshot_1min 鮮度チェックだけで発注前に止まる問題を防ぐ。
#
# 背景:
#   2026-06-05 14:48 ログでは、RANKINGで 3905/5016/4095/9831 の
#   pending は作成済み。しかし entry_controller 開始後、
#   ranking_snapshot_1min の latest が 2026-06-04 と判定され、
#   raw fallback中に controller timeout して発注まで進めなかった。
#
# 方針:
#   - pending_root 内に source=RANKING の候補がある場合は、
#     precheck を pending_ready として即OKにする。
#   - pending が無い場合は既存 precheck をそのまま使う。
#   - entry_controller が from-import 済みの場合も差し替える。
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
        logger.debug("[RANKING PRECHECK PENDING FAILOPEN] pending snapshot failed", exc_info=True)
        return 0, {}


def _pending_ready_result(total: int, counts: dict[str, int]) -> dict[str, Any]:
    return {
        "is_ready": True,
        "explicit_ready": False,
        "derived_ready": True,
        "has_snapshot": False,
        "snapshot_count": total,
        "ranking_type": "RANKING_PENDING",
        "source": "pending_failopen",
        "reason": "ranking_pending_exists",
        "pending_count": total,
        "pending_symbols": counts,
    }


def _patched_precheck_ranking_entry() -> dict[str, Any]:
    if _env_bool("RANKING_PRECHECK_PENDING_FAILOPEN_ENABLED", True):
        total, counts = _ranking_pending_snapshot()
        if total > 0:
            logger.warning(
                "[RANKING PRECHECK PENDING FAILOPEN] OK pending_count=%s symbols=%s -> skip stale snapshot precheck",
                total,
                counts,
            )
            return _pending_ready_result(total, counts)

    if callable(_ORIGINAL_PRECHECK):
        return _ORIGINAL_PRECHECK()

    return {
        "is_ready": False,
        "explicit_ready": False,
        "derived_ready": False,
        "has_snapshot": False,
        "snapshot_count": 0,
        "ranking_type": None,
        "source": "pending_failopen_patch",
        "reason": "original_precheck_unavailable",
    }


def install() -> bool:
    global _INSTALLED, _ORIGINAL_PRECHECK

    if _INSTALLED:
        return True

    try:
        import trading.handlers.entry_precheck_ranking as precheck_mod

        cur = getattr(precheck_mod, "precheck_ranking_entry", None)
        if not callable(cur):
            logger.warning("[RANKING PRECHECK PENDING FAILOPEN] target precheck not callable")
            return False
        if getattr(cur, "_ranking_precheck_pending_failopen", False):
            _INSTALLED = True
            return True

        _ORIGINAL_PRECHECK = cur
        _patched_precheck_ranking_entry._ranking_precheck_pending_failopen = True  # type: ignore[attr-defined]
        precheck_mod.precheck_ranking_entry = _patched_precheck_ranking_entry

        # entry_controller が既に from-import 済みの場合も差し替える。
        try:
            import trading.handlers.entry_controller as ec
            ec.precheck_ranking_entry = _patched_precheck_ranking_entry
        except Exception:
            pass

        _INSTALLED = True
        logger.warning("[RANKING PRECHECK PENDING FAILOPEN] installed enabled=%s", _env_bool("RANKING_PRECHECK_PENDING_FAILOPEN_ENABLED", True))
        return True
    except Exception:
        logger.exception("[RANKING PRECHECK PENDING FAILOPEN] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING PRECHECK PENDING FAILOPEN] auto install failed")


__all__ = ["install"]
