# ============================================================
# File   : core/startup/summary_entry_pending_existing_fix_patch.py
# Version: Ver01-EXISTING-PENDING-COUNTS-AS-REGISTERED
# ------------------------------------------------------------
# SUMMARY_AI で approved はあるのに、pending登録が duplicate で0件扱いになり、
# run_summary_entry_executor が no_pending_registered で止まる問題を修正する。
#
# 症状:
#   [SUMMARY_ENTRY] executor stopped reason=no_pending_registered
#   [SUMMARY AI ASYNC ENTRY] worker done ... registered=0 ... skip_reason=no_pending_registered
#
# 原因:
#   pending_manager.add_pending() は同一 identity 重複時 False を返す。
#   しかし既存 pending が残っているなら entry_controller は処理可能なため、
#   registered=0 として pipeline を止めるのは誤り。
#
# 方針:
#   - add_pending False でも、同一 identity が bucket に既にあれば registered とみなす
#   - 古い pending があって逆方向などで拒否された場合は従来通り rejected
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_REGISTER = None


def _norm(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v).strip().upper()
    except Exception:
        return ""


def _norm_interval(v: Any) -> str:
    try:
        if v is None or v == "":
            return ""
        s = str(v).strip()
        return s[:-2] if s.endswith(".0") else s
    except Exception:
        return ""


def _norm_side(v: Any) -> str:
    s = _norm(v)
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _identity(e: Dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _norm(e.get("source")),
        _norm(e.get("entry_type")),
        _norm_side(e.get("side") or e.get("entry_decision") or e.get("ai_side")),
        _norm_interval(e.get("interval")),
    )


def _symbol(e: Dict[str, Any]) -> str:
    s = str(e.get("symbol") or "").strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


def _has_same_pending(symbol: str, entry: Dict[str, Any]) -> bool:
    try:
        from trading.entry.pending_manager import get_bucket
        target = _identity(entry)
        for old in get_bucket(symbol):
            if isinstance(old, dict) and _identity(old) == target:
                return True
    except Exception:
        logger.debug("[SUMMARY ENTRY PENDING FIX] same pending check failed", exc_info=True)
    return False


def _patched_register_pending_entries(entries: List[Dict[str, Any]]) -> int:
    try:
        from trading.entry.pending_manager import add_pending, snapshot_root

        registered = 0
        rejected = 0
        existing = 0

        if not entries:
            logger.info("[SUMMARY ENTRY PENDING FIX] pending registration skipped reason=no_entries")
            return 0

        for entry in entries:
            try:
                if not isinstance(entry, dict):
                    rejected += 1
                    continue
                sym = _symbol(entry)
                if not sym:
                    rejected += 1
                    logger.warning("[SUMMARY ENTRY PENDING FIX] pending skip reason=no_symbol entry=%s", entry)
                    continue
                entry["symbol"] = sym
                ok = bool(add_pending(entry))
                if ok:
                    registered += 1
                    continue

                if _has_same_pending(sym, entry):
                    existing += 1
                    registered += 1
                    logger.warning(
                        "[SUMMARY ENTRY PENDING FIX] duplicate existing pending counts as registered symbol=%s identity=%s",
                        sym,
                        _identity(entry),
                    )
                    continue

                rejected += 1
                logger.warning(
                    "[SUMMARY ENTRY PENDING FIX] pending rejected symbol=%s identity=%s entry=%s",
                    sym,
                    _identity(entry),
                    entry,
                )
            except Exception:
                rejected += 1
                logger.exception("[SUMMARY ENTRY PENDING FIX] pending add failed entry=%s", entry)

        logger.warning(
            "[SUMMARY ENTRY PENDING FIX] pending registration done entries=%s registered=%s existing=%s rejected=%s root=%s",
            len(entries),
            registered,
            existing,
            rejected,
            snapshot_root(),
        )
        return registered
    except Exception as e:
        logger.exception("[SUMMARY ENTRY PENDING FIX] patched register failed err=%s", e)
        if callable(_ORIG_REGISTER):
            return int(_ORIG_REGISTER(entries) or 0)
        return 0


def install() -> bool:
    global _INSTALLED, _ORIG_REGISTER
    if _INSTALLED:
        return True
    try:
        import trading.summary.summary_entry as se

        cur = getattr(se, "register_pending_entries", None)
        if getattr(cur, "_summary_entry_pending_existing_fix_v1", False):
            _INSTALLED = True
            return True

        _ORIG_REGISTER = cur
        _patched_register_pending_entries._summary_entry_pending_existing_fix_v1 = True  # type: ignore[attr-defined]
        se.register_pending_entries = _patched_register_pending_entries

        _INSTALLED = True
        logger.warning("[SUMMARY ENTRY PENDING FIX] installed")
        return True
    except Exception as e:
        logger.exception("[SUMMARY ENTRY PENDING FIX] install failed err=%s", e)
        return False

try:
    install()
except Exception as e:
    logger.exception("[SUMMARY ENTRY PENDING FIX] auto install failed err=%s", e)

__all__ = ["install"]
