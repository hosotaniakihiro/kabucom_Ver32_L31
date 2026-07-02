# -*- coding: utf-8 -*-
# File: core/startup/tonosama_history_failclose_strict_patch.py
# Version: V4-STRICT-QUIET-WATCHER
from __future__ import annotations

import logging
import os
import threading
import time
from functools import wraps
from typing import Any

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


def _patch_entry_controller(reason: str) -> bool:
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
        changed = {}
        for k, v in updates.items():
            old = getattr(ec, k, None)
            if old != v:
                setattr(ec, k, v)
                changed[k] = (old, v)
        if changed:
            logger.warning("[STRICT ENTRY DEFAULTS] entry_controller constants patched reason=%s changed=%s version=%s", reason, changed, VERSION)
        return True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] entry_controller constants patch skipped reason=%s", reason, exc_info=True)
        return False


def _patch_order_builder(reason: str) -> bool:
    try:
        import trading.handlers.entry_order_builder as eob
        changed = {}
        for k, v in {"ENTRY_ORDER_REQUIRE_ATR": True, "ENTRY_ORDER_REQUIRE_HIGH_LOW": True}.items():
            old = getattr(eob, k, None)
            if old != v:
                setattr(eob, k, v)
                changed[k] = (old, v)
        if changed:
            logger.warning("[STRICT ENTRY DEFAULTS] entry_order_builder constants patched reason=%s changed=%s version=%s", reason, changed, VERSION)
        return True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] entry_order_builder constants patch skipped reason=%s", reason, exc_info=True)
        return False


def _patch_summary_ai(reason: str) -> bool:
    ok = False
    try:
        from trading.entry.summary_ai import candidates as c
        changed = {}
        for k, v in {"DEFAULT_MIN_BUY_SCORE": 3.0, "DEFAULT_MAX_SELL_SCORE": 3.0, "DEFAULT_MIN_BUY_SLOPE": 0.0, "DEFAULT_MAX_SELL_SLOPE": 0.0}.items():
            old = getattr(c, k, None)
            if old != v:
                setattr(c, k, v)
                changed[k] = (old, v)
        if changed:
            logger.warning("[STRICT ENTRY DEFAULTS] summary_ai candidates constants patched reason=%s changed=%s version=%s", reason, changed, VERSION)
        ok = True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] summary_ai candidates patch skipped reason=%s", reason, exc_info=True)
    try:
        from trading.entry.summary_ai import runner as r
        cur = getattr(r, "run_summary_ai_entry_from_df", None)
        if callable(cur) and not getattr(cur, "_strict_entry_defaults_runner_v4", False):
            orig = getattr(cur, "_original", cur)
            @wraps(orig)
            def wrapped(*args: Any, **kwargs: Any):
                kwargs["min_buy_score"] = max(float(kwargs.get("min_buy_score", 0) or 0), 3.0)
                kwargs["max_sell_score"] = max(float(kwargs.get("max_sell_score", 0) or 0), 3.0)
                kwargs.setdefault("use_pre_slope_filter", True)
                return orig(*args, **kwargs)
            wrapped._strict_entry_defaults_runner_v3 = True  # type: ignore[attr-defined]
            wrapped._strict_entry_defaults_runner_v4 = True  # type: ignore[attr-defined]
            wrapped._original = orig  # type: ignore[attr-defined]
            r.run_summary_ai_entry_from_df = wrapped
            logger.warning("[STRICT ENTRY DEFAULTS] summary_ai runner strict wrapper installed reason=%s version=%s", reason, VERSION)
        ok = True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] summary_ai runner patch skipped reason=%s", reason, exc_info=True)
    return ok


def _patch_tonosama(reason: str) -> bool:
    try:
        import pandas as pd
        import trading.entry.tonosama.volume_surge as vs
        vs._force_failopen_enabled = lambda: False
        vs._failopen_reason = lambda: "strict_disabled force=False early=False legacy_failopen=False allow_without_history=False"
        cur = getattr(vs, "build_scalping_feature_df", None)
        if callable(cur) and not getattr(cur, "_strict_no_history_failopen_v4", False):
            orig = getattr(cur, "_original", cur)
            @wraps(orig)
            def wrapped(*args: Any, **kwargs: Any):
                out = orig(*args, **kwargs)
                try:
                    if out is None or not isinstance(out, pd.DataFrame) or out.empty:
                        return out
                    hist_missing = out.get("_volume_surge_history_missing", pd.Series(False, index=out.index)).fillna(False).astype(bool)
                    failopen = out.get("_volume_surge_failopen", pd.Series(False, index=out.index)).fillna(False).astype(bool)
                    bad = hist_missing | failopen
                    if bool(bad.any()):
                        before = len(out)
                        sample = out.loc[bad, [c for c in ["symbol", "symbolname", "_volume_surge_history_missing", "_volume_surge_failopen"] if c in out.columns]].head(20).to_dict("records")
                        out = out.loc[~bad].copy()
                        logger.warning("[STRICT ENTRY DEFAULTS] TONOSAMA drop history_missing/failopen rows reason=%s before=%s after=%s dropped=%s sample=%s", reason, before, len(out), int(bad.sum()), sample)
                    return out
                except Exception:
                    logger.exception("[STRICT ENTRY DEFAULTS] TONOSAMA strict drop failed; returning original output")
                    return out
            wrapped._strict_no_history_failopen_v3 = True  # type: ignore[attr-defined]
            wrapped._strict_no_history_failopen_v4 = True  # type: ignore[attr-defined]
            wrapped._original = orig  # type: ignore[attr-defined]
            vs.build_scalping_feature_df = wrapped
            logger.warning("[STRICT ENTRY DEFAULTS] TONOSAMA volume_surge strict wrapper installed reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] TONOSAMA volume_surge patch skipped reason=%s", reason, exc_info=True)
        return False


def _patch_ranking_source(reason: str) -> bool:
    global _LAST_RANKING_FLAGS
    try:
        import core.startup.ranking_entry_source_db_fallback_patch as _p  # noqa: F401
        flags = (os.environ.get("RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE"), os.environ.get("RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL"))
        if reason == "install" or _LAST_RANKING_FLAGS != flags or flags != ("0", "0"):
            logger.warning("[STRICT ENTRY DEFAULTS] ranking source fallback strict flags reason=%s db_failopen=%s global_failopen=%s version=%s", reason, flags[0], flags[1], VERSION)
        _LAST_RANKING_FLAGS = flags
        return True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] ranking source fallback patch skipped reason=%s", reason, exc_info=True)
        return False


def _patch_all(reason: str) -> dict[str, bool]:
    return {
        "entry_controller": _patch_entry_controller(reason),
        "entry_order_builder": _patch_order_builder(reason),
        "summary_ai": _patch_summary_ai(reason),
        "tonosama_volume_surge": _patch_tonosama(reason),
        "ranking_source_fallback": _patch_ranking_source(reason),
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
        result = _patch_all(f"watcher:{i + 1}")
        if i in (0, loops - 1) or changed or not all(result.values()):
            logger.warning("[STRICT ENTRY DEFAULTS] watcher enforce i=%s/%s changed=%s result=%s version=%s", i + 1, loops, bool(changed), result, VERSION)
    logger.warning("[STRICT ENTRY DEFAULTS] watcher done loops=%s sleep=%.2f version=%s", loops, sleep_sec, VERSION)


def install() -> bool:
    global _WATCHER_STARTED
    try:
        changed = _apply_env("install")
        result = _patch_all("install")
        if not _WATCHER_STARTED:
            _WATCHER_STARTED = True
            threading.Thread(target=_watcher_loop, name="strict-entry-defaults-watch", daemon=True).start()
        logger.warning("[STRICT ENTRY DEFAULTS] installed version=%s changed=%s result=%s watcher=%s tonosama_force=%s tonosama_failopen=%s summary_min=%s ranking_db_failopen=%s ranking_global_failopen=%s", VERSION, changed, result, _WATCHER_STARTED, os.environ.get("TONOSAMA_FORCE_SURGE_FAILOPEN"), os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING"), os.environ.get("SUMMARY_AI_MIN_SCORE"), os.environ.get("RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE"), os.environ.get("RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL"))
        return True
    except Exception:
        logger.exception("[STRICT ENTRY DEFAULTS] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[STRICT ENTRY DEFAULTS] auto install failed")

__all__ = ["install", "VERSION"]
