# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_direct_timeout_continue_patch.py
# Version: V16-DISPATCH-INLINED-PENDING-GUARD-ONLY
# ------------------------------------------------------------
# Purpose:
#   SUMMARY AI direct_snapshot の timeout を、候補3銘柄まとめ投げで
#   全滅させない。ただし、AI_OK から時間が経った候補を
#   遅れて発注しない。
#
# V16:
#   - "fresh one-by-one timeout-continue" dispatch fallback (旧
#     _fallback_direct_dispatch_timeout_continue / _install_native_direct_guards)
#     は trading/entry/summary_ai/executor.py 本体 (REV12) へインライン化したため撤去。
#     register_pending_entries 向けの期限切れpending拒否ガードのみ、
#     このファイルに残す (register_pending_entries には他に無関係な2本の
#     既存パッチが別途あり、そちらは対象外)。
#   - _FRESHNESS_BY_SYMBOL はこのファイル内では誰も書き込まなくなったため、
#     このガードは entry dict 自身に埋め込まれた summary_ai_valid_until_ts
#     (executor.py 側で設定) を経由してのみ機能する。fail-open 設計のため、
#     万一値が無ければ従来通り許可する。
#
# V15:
#   - direct snapshot は必ず1銘柄ずつ実行する。
#   - timeoutした銘柄だけをスキップし、fresh window 内なら次のAI_OK候補へ進む。
#   - timeoutした内側スレッドが遅れて pending 登録へ進んでも、
#     register_pending_entries 直前で期限切れなら拒否する。
#   - 低出来高・低変動・blowoff・板ガードは緩めない。
# ============================================================
from __future__ import annotations

import functools
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V16-DISPATCH-INLINED-PENDING-GUARD-ONLY"
_INSTALLED = False
_PENDING_GUARD_INSTALLED = False

# symbol -> {valid_until_ts, approved_at_ts, max_age_sec}
_FRESHNESS_BY_SYMBOL: dict[str, dict[str, float]] = {}


def _install_pending_freshness_guard() -> bool:
    """timeoutした内側スレッドが遅れて pending 登録しても、期限切れなら拒否する。"""
    global _PENDING_GUARD_INSTALLED
    if _PENDING_GUARD_INSTALLED:
        return True
    try:
        from trading.summary import summary_entry as se

        cur = getattr(se, "register_pending_entries", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_fresh_pending_guard_v15", False):
            _PENDING_GUARD_INSTALLED = True
            return True

        original_register = getattr(cur, "_original", cur)

        @functools.wraps(original_register)
        def _register_pending_entries_fresh_guard(entries: list[dict[str, Any]], *args: Any, **kwargs: Any) -> int:
            try:
                if not entries:
                    return original_register(entries, *args, **kwargs)
                now = time.time()
                kept: list[dict[str, Any]] = []
                skipped: list[str] = []
                for entry in list(entries or []):
                    try:
                        if not isinstance(entry, dict):
                            kept.append(entry)
                            continue
                        sym = str(entry.get("symbol") or "").strip()
                        meta = _FRESHNESS_BY_SYMBOL.get(sym) if sym else None
                        valid_until = entry.get("summary_ai_valid_until_ts")
                        if valid_until is None and meta:
                            valid_until = meta.get("valid_until_ts")
                        if valid_until is not None and now > float(valid_until):
                            skipped.append(sym or "?")
                            continue
                        kept.append(entry)
                    except Exception:
                        kept.append(entry)
                if skipped:
                    logger.warning(
                        "[SUMMARY AI DIRECT FRESHNESS] pending skip expired symbols=%s kept=%s skipped=%s version=%s",
                        skipped,
                        len(kept),
                        len(skipped),
                        VERSION,
                    )
                if not kept:
                    return 0
                return original_register(kept, *args, **kwargs)
            except Exception:
                logger.exception("[SUMMARY AI DIRECT FRESHNESS] pending guard failed; use original")
                return original_register(entries, *args, **kwargs)

        _register_pending_entries_fresh_guard._summary_ai_fresh_pending_guard_v15 = True  # type: ignore[attr-defined]
        _register_pending_entries_fresh_guard._summary_ai_fresh_pending_guard_v14 = True  # type: ignore[attr-defined]
        _register_pending_entries_fresh_guard._original = original_register  # type: ignore[attr-defined]
        se.register_pending_entries = _register_pending_entries_fresh_guard
        _PENDING_GUARD_INSTALLED = True
        logger.warning("[SUMMARY AI DIRECT FRESHNESS] pending registration expiry guard installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT FRESHNESS] pending guard install failed")
        return False


# "fresh one-by-one timeout-continue" dispatch fallback (旧
# _fallback_direct_dispatch_timeout_continue / _install_native_direct_guards /
# _force_direct_one_by_one_env) は trading/entry/summary_ai/executor.py 本体
# (REV12) へインライン化済み。


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        ok = _install_pending_freshness_guard()
        _INSTALLED = bool(ok)
        logger.warning("[SUMMARY AI DIRECT TIMEOUT CONTINUE] installed version=%s (dispatch inlined) pending_guard=%s", VERSION, ok)
        return bool(ok)
    except Exception:
        logger.exception("[SUMMARY AI DIRECT TIMEOUT CONTINUE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI DIRECT TIMEOUT CONTINUE] auto install failed")


__all__ = ["install", "VERSION"]
