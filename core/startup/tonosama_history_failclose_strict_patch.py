# ============================================================
# File   : core/startup/tonosama_history_failclose_strict_patch.py
# Version: V2-STRICT-ENTRY-THRESHOLD-AND-TONOSAMA-FAILCLOSE
# ------------------------------------------------------------
# Purpose:
#   tonosama_history_missing_guard_patch.py の raw1履歴復旧ロジックは残しつつ、
#   履歴不足時の fail-open だけを最終的に fail-close へ戻す。
#
#   併せて、SUMMARY_AI 閾値を 1.00 へ緩和する runtime patch より後でも、
#   strict 値へ戻す。
#
# Why:
#   ユーザー運用方針は「緩和しない」。
#   history_missing / surge history missing のまま entry を許可すると、
#   低出来高・低変動銘柄が通る可能性がある。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
VERSION = "V2-STRICT-ENTRY-THRESHOLD-AND-TONOSAMA-FAILCLOSE"
_WATCHER_STARTED = False

_STRICT_VALUES = {
    # Tonosama: 履歴不足は通さない。raw1/DB履歴復旧は残す。
    "TONOSAMA_FORCE_HISTORY_FAILCLOSE": "1",
    "TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING": "0",
    "TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY": "0",
    "TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY": "0",
    "TONOSAMA_DROP_HISTORY_MISSING_ENTRY": "1",
    "TONOSAMA_HISTORY_MISSING_QUALITY_GUARD": "1",
    "TONOSAMA_RAW1_RESAMPLE_FALLBACK": "1",
    "TONOSAMA_RAW1_HISTORY_RESAMPLE": "1",
    "TONOSAMA_PUSH_RAW_DB_HISTORY_ENABLED": "1",

    # Summary AI: 1.00系の緩和を禁止し、最低3.00/信頼度0.60へ戻す。
    "SUMMARY_AI_MIN_BUY": "3.00",
    "SUMMARY_AI_MIN_SELL": "3.00",
    "SUMMARY_AI_MIN_CONF": "0.60",
    "ENTRY_CONTROLLER_MIN_SUMMARY_SCORE_BUY": "3.00",
    "ENTRY_CONTROLLER_MIN_SUMMARY_SCORE_SELL": "3.00",
    "ENTRY_CONTROLLER_MIN_COMPOSITE_SCORE_BUY": "3.00",
    "ENTRY_CONTROLLER_MIN_COMPOSITE_SCORE_SELL": "3.00",
    "ENTRY_CONTROLLER_MIN_AI_CONFIDENCE_BUY": "0.60",
    "ENTRY_CONTROLLER_MIN_AI_CONFIDENCE_SELL": "0.60",
    "MIN_ENTRY_SCORE": "3.00",
    "MIN_ENTRY_SCORE_BUY_SUMMARY": "3.00",
    "MIN_ENTRY_SCORE_SELL_SUMMARY": "3.00",
    "MIN_SUMMARY_SCORE_BUY": "3.00",
    "MIN_SUMMARY_SCORE_SELL": "3.00",
    "MIN_COMPOSITE_SCORE_BUY": "3.00",
    "MIN_COMPOSITE_SCORE_SELL": "3.00",
}


def _apply_strict_values(*, reason: str) -> dict[str, tuple[str | None, str]]:
    changed: dict[str, tuple[str | None, str]] = {}
    for key, val in _STRICT_VALUES.items():
        old = os.environ.get(key)
        os.environ[key] = val
        if str(old) != str(val):
            changed[key] = (old, val)
    if changed:
        logger.warning("[STRICT ENTRY DEFAULTS] applied reason=%s version=%s changed=%s", reason, VERSION, changed)
    return changed


def _patch_entry_controller_constants(*, reason: str) -> bool:
    """entry_controller が環境変数を読まずモジュール定数を使う場合の保険。"""
    ok = False
    try:
        import trading.handlers.entry_controller as ec
        updates = {
            "MIN_SUMMARY_SCORE_BUY": 3.0,
            "MIN_SUMMARY_SCORE_SELL": 3.0,
            "MIN_COMPOSITE_SCORE_BUY": 3.0,
            "MIN_COMPOSITE_SCORE_SELL": 3.0,
            "MIN_AI_CONFIDENCE_BUY": 0.60,
            "MIN_AI_CONFIDENCE_SELL": 0.60,
            "MAX_APPROVED_PER_RUN": 3,
        }
        changed: dict[str, tuple[object, object]] = {}
        for key, val in updates.items():
            old = getattr(ec, key, None)
            if old != val:
                setattr(ec, key, val)
                changed[key] = (old, val)
        if changed:
            logger.warning("[STRICT ENTRY DEFAULTS] entry_controller constants patched reason=%s changed=%s version=%s", reason, changed, VERSION)
        ok = True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] entry_controller constants patch skipped reason=%s", reason, exc_info=True)
    return ok


def _watcher_loop() -> None:
    try:
        loops = int(float(os.getenv("STRICT_ENTRY_DEFAULTS_WATCH_LOOPS", "90") or "90"))
    except Exception:
        loops = 90
    try:
        sleep_sec = float(os.getenv("STRICT_ENTRY_DEFAULTS_WATCH_SLEEP", "1.0") or "1.0")
    except Exception:
        sleep_sec = 1.0
    loops = max(1, loops)
    sleep_sec = max(0.2, sleep_sec)
    for i in range(loops):
        time.sleep(sleep_sec)
        _apply_strict_values(reason=f"watcher:{i + 1}")
        _patch_entry_controller_constants(reason=f"watcher:{i + 1}")
    logger.warning("[STRICT ENTRY DEFAULTS] watcher done loops=%s sleep=%.2f version=%s", loops, sleep_sec, VERSION)


def install() -> bool:
    global _WATCHER_STARTED
    try:
        changed = _apply_strict_values(reason="install")
        const_ok = _patch_entry_controller_constants(reason="install")
        if not _WATCHER_STARTED:
            _WATCHER_STARTED = True
            threading.Thread(target=_watcher_loop, name="strict-entry-defaults-watch", daemon=True).start()
        logger.warning(
            "[STRICT ENTRY DEFAULTS] installed version=%s changed=%s const_ok=%s watcher=%s tonosama_failopen=%s allow_without=%s allow_missing=%s drop_missing=%s summary_min_buy=%s summary_min_sell=%s composite_buy=%s composite_sell=%s conf=%s",
            VERSION,
            changed,
            const_ok,
            _WATCHER_STARTED,
            os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING"),
            os.environ.get("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY"),
            os.environ.get("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY"),
            os.environ.get("TONOSAMA_DROP_HISTORY_MISSING_ENTRY"),
            os.environ.get("SUMMARY_AI_MIN_BUY"),
            os.environ.get("SUMMARY_AI_MIN_SELL"),
            os.environ.get("MIN_COMPOSITE_SCORE_BUY"),
            os.environ.get("MIN_COMPOSITE_SCORE_SELL"),
            os.environ.get("SUMMARY_AI_MIN_CONF"),
        )
        return True
    except Exception:
        logger.exception("[STRICT ENTRY DEFAULTS] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[STRICT ENTRY DEFAULTS] auto install failed")


__all__ = ["install", "VERSION"]
