# -*- coding: utf-8 -*-
# File   : core/startup/strict_entry_defaults.py
# ------------------------------------------------------------
# 旧 core/startup/tonosama_history_failclose_strict_patch.py
# (V4-STRICT-QUIET-WATCHER) の本文化。
#
# 中身のうち「各モジュールの定数/関数をどう変えるか」は全て本体側へ移した:
#   trading/handlers/entry_controller.apply_strict_entry_constants()
#   trading/handlers/entry_order_builder.apply_strict_order_constants()
#   trading/entry/summary_ai/candidates.apply_strict_candidate_constants()
#   trading/entry/summary_ai/runner.install_strict_kwargs_floor()
#   trading/entry/tonosama/volume_surge.install_strict_history_failclose()
#
# ここに残しているのは「いつ・何回 適用するか」だけ:
#   - STRICT_ENV の強制適用 (setdefault ではなく上書き)
#   - 起動直後 180 秒間、毎秒の再適用 (watcher)
# watcher は load-bearing。fast_startup / more_candidates / oneshot_limit が
# あとから緩めた定数を strict 側へ引き戻す最終決定権を持つ層で、
# 実測でも MAX_APPROVED_PER_RUN や MIN_SUMMARY_SCORE_* の定常値を決めている。
#
# install 元は core/startup/runtime_env/defaults_patch._install_strict_entry_defaults()
# (context="main" のみ / DISABLE_STRICT_ENTRY_DEFAULTS_PATCH=1 で無効化)。
# ------------------------------------------------------------
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
VERSION = "V4-STRICT-QUIET-WATCHER"
_WATCHER_STARTED = False
_LAST_RANKING_FLAGS: tuple[str | None, str | None] | None = None

STRICT_ENV = {
    "TONOSAMA_FORCE_HISTORY_FAILCLOSE": "1",
    "TONOSAMA_FORCE_SURGE_FAILOPEN": "0",
    "TONOSAMA_ALLOW_EARLY_SURGE_FAILOPEN": "0",
    "TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING": "0",
    "TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY": "0",
    "TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY": "0",
    "TONOSAMA_DROP_HISTORY_MISSING_ENTRY": "1",
    "TONOSAMA_HISTORY_MISSING_QUALITY_GUARD": "1",
    "TONOSAMA_RAW1_RESAMPLE_FALLBACK": "1",
    "TONOSAMA_RAW1_HISTORY_RESAMPLE": "1",
    "TONOSAMA_PUSH_RAW_DB_HISTORY_ENABLED": "1",
    "SUMMARY_ENTRY_MIN_SCORE": "3.00",
    "SUMMARY_AI_MIN_SCORE": "3.00",
    "ENTRY_GATE_MIN_SCORE": "3.00",
    "SUMMARY_AI_MIN_BUY_SCORE": "3.00",
    "SUMMARY_AI_MIN_SELL_SCORE": "3.00",
    "SUMMARY_AI_MIN_BUY": "3.00",
    "SUMMARY_AI_MIN_SELL": "3.00",
    "SUMMARY_AI_MIN_CONF": "0.60",
    "SUMMARY_AI_RESCUE_SCORE_LOW": "0",
    "SUMMARY_AI_SCORE_LOW_RESCUE_CONFIDENCE": "0.60",
    "SUMMARY_AI_DIRECT_LIQ_REQUIRE_DATA": "1",
    "SUMMARY_ENTRY_REQUIRE_3MIN_READY": "1",
    "SUMMARY_ENTRY_REQUIRE_MTF": "1",
    "SUMMARY_AI_REQUIRE_MTF": "1",
    "SUMMARY_ENTRY_ALLOW_1MIN_FALLBACK": "0",
    "ENTRY_BYPASS_SLOPE_FILTER": "0",
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
    "ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN": "0",
    "ATR_1M_FILTER_TONOSAMA_ERROR_FAIL_OPEN": "0",
    "RANGE_5M_FILTER_NG_FAIL_OPEN": "0",
    "RANGE_5M_FILTER_RECURSION_FAIL_OPEN": "0",
    "RANGE_5M_FILTER_ERROR_FAIL_OPEN": "0",
    "ENTRY_ORDER_REQUIRE_ATR": "1",
    "ENTRY_ORDER_REQUIRE_HIGH_LOW": "1",
    "ENTRY_ALLOW_ENTRY_WITHOUT_BOARD": "0",
    "ENTRY_BOARD_MISSING_HARD_BLOCK": "1",
    "ENTRY_LIMIT_ALLOW_WITHOUT_BOARD": "0",
    "ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_BUY": "0",
    "ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_SELL": "0",
    "ENTRY_DIRECTION_REVERSE_AGAINST_CLEAR_TREND": "0",
    "ENTRY_DIRECTION_REVERSE_HALF_SIZE": "0",
    "ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN": "0",
    "ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN": "0",
    "RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE": "0",
    "RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL": "0",
    "RANKING_ENTRY_SOURCE_DB_FAILOPEN_MAX_AGE_SEC": "300",
    "RANKING_ENTRY_SOURCE_FAILOPEN_GLOBAL_MAX_AGE_SEC": "300",
    "RANKING_ENTRY_RUNTIME_RESCUE_ENABLED": "0",
    "ENTRY_FIRE_RESCUE_SCORE_LOW_ENABLED": "0",
}


def _apply_env(reason: str) -> dict[str, tuple[str | None, str]]:
    changed: dict[str, tuple[str | None, str]] = {}
    for k, v in STRICT_ENV.items():
        old = os.environ.get(k)
        os.environ[k] = v
        if str(old) != str(v):
            changed[k] = (old, v)
    if changed:
        logger.warning("[STRICT ENTRY DEFAULTS] applied reason=%s version=%s changed=%s", reason, VERSION, changed)
    return changed


def _apply_entry_controller(reason: str) -> bool:
    try:
        import trading.handlers.entry_controller as ec

        changed = ec.apply_strict_entry_constants()
        if changed:
            logger.warning("[STRICT ENTRY DEFAULTS] entry_controller constants patched reason=%s changed=%s version=%s", reason, changed, VERSION)
        return True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] entry_controller constants patch skipped reason=%s", reason, exc_info=True)
        return False


def _apply_order_builder(reason: str) -> bool:
    try:
        import trading.handlers.entry_order_builder as eob

        changed = eob.apply_strict_order_constants()
        if changed:
            logger.warning("[STRICT ENTRY DEFAULTS] entry_order_builder constants patched reason=%s changed=%s version=%s", reason, changed, VERSION)
        return True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] entry_order_builder constants patch skipped reason=%s", reason, exc_info=True)
        return False


def _apply_summary_ai(reason: str) -> bool:
    ok = False
    try:
        from trading.entry.summary_ai import candidates as c

        changed = c.apply_strict_candidate_constants()
        if changed:
            logger.warning("[STRICT ENTRY DEFAULTS] summary_ai candidates constants patched reason=%s changed=%s version=%s", reason, changed, VERSION)
        ok = True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] summary_ai candidates patch skipped reason=%s", reason, exc_info=True)
    try:
        from trading.entry.summary_ai import runner as r

        r.install_strict_kwargs_floor()
        ok = True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] summary_ai runner patch skipped reason=%s", reason, exc_info=True)
    return ok


def _apply_tonosama(reason: str) -> bool:
    try:
        import trading.entry.tonosama.volume_surge as vs

        return bool(vs.install_strict_history_failclose())
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] TONOSAMA volume_surge patch skipped reason=%s", reason, exc_info=True)
        return False


def _log_ranking_source(reason: str) -> bool:
    """ranking source fail-open フラグの状態ログ。

    実体 (ranking_entry_source_db_fallback_patch) は
    trading/ranking/entry_from_ranking.py へ本文化済みで、
    ここは STRICT_ENV が効いているかをログへ出すだけ。
    """
    global _LAST_RANKING_FLAGS
    try:
        flags = (
            os.environ.get("RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE"),
            os.environ.get("RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL"),
        )
        if reason == "install" or _LAST_RANKING_FLAGS != flags or flags != ("0", "0"):
            logger.warning("[STRICT ENTRY DEFAULTS] ranking source fallback strict flags reason=%s db_failopen=%s global_failopen=%s version=%s", reason, flags[0], flags[1], VERSION)
        _LAST_RANKING_FLAGS = flags
        return True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] ranking source fallback patch skipped reason=%s", reason, exc_info=True)
        return False


def _apply_all(reason: str) -> dict[str, bool]:
    return {
        "entry_controller": _apply_entry_controller(reason),
        "entry_order_builder": _apply_order_builder(reason),
        "summary_ai": _apply_summary_ai(reason),
        "tonosama_volume_surge": _apply_tonosama(reason),
        "ranking_source_fallback": _log_ranking_source(reason),
    }


def _watcher_loop() -> None:
    try:
        loops = int(float(os.getenv("STRICT_ENTRY_DEFAULTS_WATCH_LOOPS", "180") or "180"))
    except Exception:
        loops = 180
    try:
        sleep_sec = float(os.getenv("STRICT_ENTRY_DEFAULTS_WATCH_SLEEP", "1.0") or "1.0")
    except Exception:
        sleep_sec = 1.0
    loops = max(1, loops)
    sleep_sec = max(0.2, sleep_sec)
    for i in range(loops):
        time.sleep(sleep_sec)
        changed = _apply_env(f"watcher:{i + 1}")
        result = _apply_all(f"watcher:{i + 1}")
        if i in (0, loops - 1) or changed or not all(result.values()):
            logger.warning("[STRICT ENTRY DEFAULTS] watcher enforce i=%s/%s changed=%s result=%s version=%s", i + 1, loops, bool(changed), result, VERSION)
    logger.warning("[STRICT ENTRY DEFAULTS] watcher done loops=%s sleep=%.2f version=%s", loops, sleep_sec, VERSION)


def install() -> bool:
    global _WATCHER_STARTED
    try:
        changed = _apply_env("install")
        result = _apply_all("install")
        if not _WATCHER_STARTED:
            _WATCHER_STARTED = True
            threading.Thread(target=_watcher_loop, name="strict-entry-defaults-watch", daemon=True).start()
        logger.warning(
            "[STRICT ENTRY DEFAULTS] installed version=%s changed=%s result=%s watcher=%s tonosama_force=%s tonosama_failopen=%s summary_min=%s ranking_db_failopen=%s ranking_global_failopen=%s",
            VERSION, changed, result, _WATCHER_STARTED,
            os.environ.get("TONOSAMA_FORCE_SURGE_FAILOPEN"),
            os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING"),
            os.environ.get("SUMMARY_AI_MIN_SCORE"),
            os.environ.get("RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE"),
            os.environ.get("RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL"),
        )
        return True
    except Exception:
        logger.exception("[STRICT ENTRY DEFAULTS] install failed")
        return False


__all__ = ["install", "VERSION", "STRICT_ENV"]
