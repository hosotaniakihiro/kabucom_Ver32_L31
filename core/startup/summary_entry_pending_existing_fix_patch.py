# ============================================================
# File   : core/startup/summary_entry_pending_existing_fix_patch.py
# Version: Ver02-REFRESH-DUPLICATE-PENDING
# ------------------------------------------------------------
# SUMMARY_AI で approved はあるのに、pending登録が duplicate で0件扱いになり、
# run_summary_entry_executor が no_pending_registered で止まる問題を修正する。
#
# Ver01:
#   - add_pending False でも、同一 identity が bucket に既にあれば registered とみなす。
#
# Ver02:
#   - 既存pendingを「登録済み」と数えるだけでは、古い候補/古いentry_rowが残る。
#   - 同一 identity の重複pendingは、新しい候補で bucket を置換する。
#   - これにより pending duplicate skipped 後も、最新のscore/side/rowで entry pipeline に流す。
#   - opposite side / mixed side など、同一identity以外の拒否は従来通り rejected。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_REGISTER = None

_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_SET = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE_SET:
            return True
        if s in _FALSE_SET:
            return False
    except Exception:
        pass
    return bool(default)


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


def _identity(e: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        _norm(e.get("source")),
        _norm(e.get("entry_type")),
        _norm_side(e.get("side") or e.get("entry_decision") or e.get("ai_side")),
        _norm_interval(e.get("interval")),
    )


def _symbol(e: Dict[str, Any]) -> str:
    s = str(e.get("symbol") or "").strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


def _stamp(entry: Dict[str, Any]) -> None:
    try:
        now = dt.datetime.now()
        entry["created_at"] = entry.get("created_at") or now
        entry["pending_refreshed_at"] = now
        entry["pending_duplicate_refreshed"] = True
    except Exception:
        pass


def _same_pending_rows(symbol: str, entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        from trading.entry.pending_manager import get_bucket

        target = _identity(entry)
        return [old for old in get_bucket(symbol) if isinstance(old, dict) and _identity(old) == target]
    except Exception:
        logger.debug("[SUMMARY ENTRY PENDING FIX] same pending rows check failed", exc_info=True)
        return []


def _refresh_same_pending(symbol: str, entry: Dict[str, Any]) -> bool:
    """
    add_pending() が同一 identity duplicate で False を返した場合、
    古いpendingを最新entryで置換する。
    """
    if not _env_bool("SUMMARY_PENDING_DUPLICATE_REFRESH_ENABLED", True):
        return False
    try:
        from trading.entry.pending_manager import get_bucket, replace_bucket

        target = _identity(entry)
        bucket = get_bucket(symbol)
        if not any(isinstance(old, dict) and _identity(old) == target for old in bucket):
            return False

        kept: List[Dict[str, Any]] = []
        removed = 0
        old_samples: List[Dict[str, Any]] = []
        for old in bucket:
            if isinstance(old, dict) and _identity(old) == target:
                removed += 1
                if len(old_samples) < 3:
                    old_samples.append({
                        "score": old.get("score"),
                        "score_buy": old.get("score_buy"),
                        "score_sell": old.get("score_sell"),
                        "side": old.get("side"),
                        "datetime": old.get("datetime"),
                        "created_at": old.get("created_at"),
                    })
                continue
            kept.append(old)

        _stamp(entry)
        replace_bucket(symbol, kept + [entry])
        logger.warning(
            "[SUMMARY ENTRY PENDING FIX] duplicate pending refreshed symbol=%s identity=%s removed=%s old_samples=%s new_score=%s new_side=%s new_datetime=%s",
            symbol,
            target,
            removed,
            old_samples,
            entry.get("score", entry.get("final_score")),
            entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"),
            entry.get("datetime"),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY ENTRY PENDING FIX] duplicate refresh failed symbol=%s identity=%s", symbol, _identity(entry))
        return False


def _patched_register_pending_entries(entries: List[Dict[str, Any]]) -> int:
    try:
        from trading.entry.pending_manager import add_pending, snapshot_root

        registered = 0
        rejected = 0
        refreshed = 0
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

                same_rows = _same_pending_rows(sym, entry)
                if same_rows:
                    existing += 1
                    if _refresh_same_pending(sym, entry):
                        refreshed += 1
                    registered += 1
                    logger.warning(
                        "[SUMMARY ENTRY PENDING FIX] duplicate existing pending treated as registered symbol=%s identity=%s refreshed=%s",
                        sym,
                        _identity(entry),
                        bool(same_rows),
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
            "[SUMMARY ENTRY PENDING FIX] pending registration done entries=%s registered=%s existing=%s refreshed=%s rejected=%s root=%s",
            len(entries),
            registered,
            existing,
            refreshed,
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
        if getattr(cur, "_summary_entry_pending_existing_fix_v2", False):
            _INSTALLED = True
            return True

        _ORIG_REGISTER = cur
        _patched_register_pending_entries._summary_entry_pending_existing_fix_v2 = True  # type: ignore[attr-defined]
        se.register_pending_entries = _patched_register_pending_entries

        _INSTALLED = True
        logger.warning("[SUMMARY ENTRY PENDING FIX] installed v2 duplicate_refresh=%s", os.getenv("SUMMARY_PENDING_DUPLICATE_REFRESH_ENABLED", "1"))
        return True
    except Exception as e:
        logger.exception("[SUMMARY ENTRY PENDING FIX] install failed err=%s", e)
        return False


try:
    install()
except Exception as e:
    logger.exception("[SUMMARY ENTRY PENDING FIX] auto install failed err=%s", e)


__all__ = ["install"]
