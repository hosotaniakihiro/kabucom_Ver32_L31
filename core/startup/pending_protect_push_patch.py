# ============================================================
# File   : core/startup/pending_protect_push_patch.py
# Version: V1.0-PENDING-PROTECT-PUSH-ROTATION
# ------------------------------------------------------------
# 【目的】
#   A/B 50銘柄ローテーション中、エントリー候補銘柄が反対側にいて
#   板が取れない問題を減らす。
#
# 【動作】
#   pending_manager.add_pending() をラップし、pending化した銘柄を
#   global_data.recent_entry_symbols / last_entry_candidates /
#   recent_ai_ok_symbols に登録する。
#
#   rotation_core は protected symbols を A/B 両面へ入れるため、
#   次サイクル以降、エントリー候補はPUSH登録から外れにくくなる。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

from global_state import global_data

logger = logging.getLogger(__name__)
_PATCHED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        if s.endswith(".T"):
            s = s[:-2]
        if not s or s in {"NONE", "NULL", "NAN", "NA", "-", "0"}:
            return ""
        return s
    except Exception:
        return ""


def _dedupe_keep_order(items: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        s = _norm_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _protect_symbol(symbol: Any, *, source: Any = None, side: Any = None) -> None:
    if not _env_bool("PENDING_PROTECT_PUSH_SYMBOLS", True):
        return
    sym = _norm_symbol(symbol)
    if not sym:
        return

    max_keep = _env_int("PENDING_PROTECT_PUSH_MAX_KEEP", 50)

    try:
        for attr in ("recent_entry_symbols", "last_entry_candidates", "recent_ai_ok_symbols"):
            cur = getattr(global_data, attr, None)
            if cur is None:
                cur_list = []
            elif isinstance(cur, (list, tuple, set)):
                cur_list = list(cur)
            elif isinstance(cur, dict):
                cur_list = list(cur.keys())
            else:
                cur_list = [cur]

            syms = _dedupe_keep_order([sym] + cur_list)
            if max_keep > 0:
                syms = syms[:max_keep]
            setattr(global_data, attr, syms)

        logger.warning(
            "[PENDING PROTECT PUSH] symbol=%s source=%s side=%s recent_entry_symbols=%s",
            sym,
            source,
            side,
            getattr(global_data, "recent_entry_symbols", [])[:20],
        )
    except Exception:
        logger.debug("[PENDING PROTECT PUSH] failed symbol=%s", sym, exc_info=True)


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import trading.entry.pending_manager as pm
    except Exception:
        logger.exception("[PENDING PROTECT PUSH] import pending_manager failed")
        return False

    old_add_pending = getattr(pm, "add_pending", None)
    if not callable(old_add_pending):
        logger.warning("[PENDING PROTECT PUSH] add_pending not callable")
        return False

    if getattr(old_add_pending, "_pending_protect_push_v1", False):
        _PATCHED = True
        return True

    def _add_pending_with_push_protect(entry: dict) -> bool:
        ok = bool(old_add_pending(entry))
        try:
            if isinstance(entry, dict):
                # 追加成功時はもちろん、重複pendingでも候補として保護する。
                _protect_symbol(
                    entry.get("symbol"),
                    source=entry.get("source"),
                    side=entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"),
                )
        except Exception:
            logger.debug("[PENDING PROTECT PUSH] protect after add failed", exc_info=True)
        return ok

    _add_pending_with_push_protect._pending_protect_push_v1 = True  # type: ignore[attr-defined]
    _add_pending_with_push_protect._original = old_add_pending  # type: ignore[attr-defined]
    pm.add_pending = _add_pending_with_push_protect

    _PATCHED = True
    logger.warning(
        "[PENDING PROTECT PUSH] installed enabled=%s max_keep=%s",
        _env_bool("PENDING_PROTECT_PUSH_SYMBOLS", True),
        _env_int("PENDING_PROTECT_PUSH_MAX_KEEP", 50),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[PENDING PROTECT PUSH] auto install failed")

__all__ = ["install"]
