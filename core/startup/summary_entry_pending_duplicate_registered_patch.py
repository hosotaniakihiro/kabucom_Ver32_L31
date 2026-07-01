# ============================================================
# File   : core/startup/summary_entry_pending_duplicate_registered_patch.py
# Version: V1-DUPLICATE-PENDING-COUNTS-AS-REGISTERED
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI direct dispatch が approved rows を持っていても、
#   pending_manager.add_pending() が duplicate existing を False として返すと、
#   run_summary_entry_executor が no_pending_registered で止まり発注まで進まない。
#
# 対策:
#   summary_entry.register_pending_entries を runtime patch し、add_pending=False でも
#   pending root に同一 identity が既に存在する場合は registered 相当として数える。
#   BUY/SELL混在など、本当に危険な reject は has_identity=False のため登録扱いにしない。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_REGISTER_PENDING_ENTRIES = None


def _safe_symbol(row: dict[str, Any]) -> str:
    try:
        s = str(row.get("symbol") or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _patched_register_pending_entries(entries):
    try:
        import trading.summary.summary_entry as se
        from trading.entry.pending_manager import add_pending, has_identity, snapshot_root

        registered = 0
        rejected = 0
        duplicate_existing = 0

        if not entries:
            logger.info("[SUMMARY ENTRY DUP REGISTER PATCH] skipped reason=no_entries")
            return 0

        for entry in list(entries or []):
            try:
                if not isinstance(entry, dict):
                    rejected += 1
                    continue

                symbol = _safe_symbol(entry)
                if not symbol:
                    rejected += 1
                    logger.warning("[SUMMARY ENTRY DUP REGISTER PATCH] skip reason=no_symbol entry=%s", entry)
                    continue

                entry["symbol"] = symbol
                entry["entry_type"] = entry.get("entry_type") or getattr(se, "DEFAULT_ENTRY_TYPE", "SUMMARY_AI")
                entry["source"] = entry.get("source") or getattr(se, "DEFAULT_SOURCE", "SUMMARY")

                try:
                    side = se._normalize_side(entry)
                except Exception:
                    side = str(entry.get("side") or entry.get("entry_decision") or entry.get("ai_side") or "BUY").upper()
                entry["side"] = side
                entry["entry_decision"] = side

                try:
                    entry["interval"] = se._safe_interval(entry.get("interval"))
                except Exception:
                    pass

                logger.info(
                    "[SUMMARY ENTRY DUP REGISTER PATCH] pending add request symbol=%s side=%s entry_type=%s source=%s interval=%s",
                    entry.get("symbol"), entry.get("side"), entry.get("entry_type"), entry.get("source"), entry.get("interval"),
                )

                ok = bool(add_pending(entry))
                if ok:
                    registered += 1
                    logger.info("[SUMMARY ENTRY DUP REGISTER PATCH] pending added symbol=%s side=%s", symbol, side)
                    continue

                # add_pending=False は duplicate existing でも返る。
                # 同一identityが既にrootにある場合は、発注パイプラインの対象が存在するため登録済み扱いにする。
                try:
                    if has_identity(symbol, entry):
                        duplicate_existing += 1
                        registered += 1
                        logger.warning(
                            "[SUMMARY ENTRY DUP REGISTER PATCH] duplicate existing treated as registered symbol=%s side=%s identity_exists=True root=%s",
                            symbol, side, snapshot_root(),
                        )
                        continue
                except Exception:
                    logger.debug("[SUMMARY ENTRY DUP REGISTER PATCH] has_identity check failed symbol=%s", symbol, exc_info=True)

                rejected += 1
                logger.warning(
                    "[SUMMARY ENTRY DUP REGISTER PATCH] pending rejected symbol=%s side=%s entry_type=%s source=%s interval=%s root=%s",
                    entry.get("symbol"), entry.get("side"), entry.get("entry_type"), entry.get("source"), entry.get("interval"), snapshot_root(),
                )
            except Exception:
                rejected += 1
                logger.exception("[SUMMARY ENTRY DUP REGISTER PATCH] pending add failed entry=%s", entry)

        logger.warning(
            "[SUMMARY ENTRY DUP REGISTER PATCH] pending registration done entries=%s registered=%s duplicate_existing=%s rejected=%s root=%s",
            len(entries or []), registered, duplicate_existing, rejected, snapshot_root(),
        )
        return registered
    except Exception:
        logger.exception("[SUMMARY ENTRY DUP REGISTER PATCH] patched register failed")
        if callable(_ORIG_REGISTER_PENDING_ENTRIES):
            return _ORIG_REGISTER_PENDING_ENTRIES(entries)
        return 0


def install() -> bool:
    global _INSTALLED, _ORIG_REGISTER_PENDING_ENTRIES
    if _INSTALLED:
        return True
    try:
        import trading.summary.summary_entry as se
        cur = getattr(se, "register_pending_entries", None)
        if not callable(cur):
            logger.warning("[SUMMARY ENTRY DUP REGISTER PATCH] target missing")
            return False
        if getattr(cur, "_summary_entry_dup_registered_v1", False):
            _INSTALLED = True
            return True
        _ORIG_REGISTER_PENDING_ENTRIES = getattr(cur, "_original", cur)
        _patched_register_pending_entries._summary_entry_dup_registered_v1 = True  # type: ignore[attr-defined]
        _patched_register_pending_entries._original = _ORIG_REGISTER_PENDING_ENTRIES  # type: ignore[attr-defined]
        se.register_pending_entries = _patched_register_pending_entries
        _INSTALLED = True
        logger.warning("[SUMMARY ENTRY DUP REGISTER PATCH] installed v1 duplicate_pending_counts_as_registered=True")
        return True
    except Exception:
        logger.exception("[SUMMARY ENTRY DUP REGISTER PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY ENTRY DUP REGISTER PATCH] auto install failed")


__all__ = ["install"]
