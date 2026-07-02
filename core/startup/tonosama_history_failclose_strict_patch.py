# ============================================================
# File   : core/startup/tonosama_history_failclose_strict_patch.py
# Version: V3-STRICT-NO-RESCUE-NO-FAILOPEN
# ------------------------------------------------------------
# Purpose:
#   ユーザー運用方針「緩和しない」を最終的に保証する strict override。
#
#   - Tonosama volume_surge 履歴不足の controlled fail-open を止める
#   - Summary AI score_low rescue / 0.12 閾値を止める
#   - ENTRY FINAL FILTER の ATR/range fail-open を止める
#   - Ranking stale DB/global fallback fail-open を止める
#   - direction confirm の fail-open / reverse rescue を止める
#
# Notes:
#   既存patch群は起動順で後から緩和値を入れるため、このpatchは
#   watcherで一定時間、環境変数と主要関数を fail-close 側へ戻す。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3-STRICT-NO-RESCUE-NO-FAILOPEN"
_WATCHER_STARTED = False

_STRICT_VALUES = {
    # Tonosama: 履歴不足は通さない。raw1/DB履歴復旧は残す。
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

    # Summary AI: 1.00 / 0.12 系の緩和を禁止。
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

    # Entry controller thresholds.
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

    # Final entry filters: ATR/range/history/board missing は fail-open しない。
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

    # Direction guard: fail-open / reverse rescue しない。
    "ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_BUY": "0",
    "ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_SELL": "0",
    "ENTRY_DIRECTION_REVERSE_AGAINST_CLEAR_TREND": "0",
    "ENTRY_DIRECTION_REVERSE_HALF_SIZE": "0",
    "ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN": "0",
    "ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN": "0",

    # Ranking source fallback: stale DB/global を fail-open しない。
    "RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE": "0",
    "RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL": "0",
    "RANKING_ENTRY_SOURCE_DB_FAILOPEN_MAX_AGE_SEC": "300",
    "RANKING_ENTRY_SOURCE_FAILOPEN_GLOBAL_MAX_AGE_SEC": "300",

    # Other rescue knobs observed in logs.
    "RANKING_ENTRY_RUNTIME_RESCUE_ENABLED": "0",
    "ENTRY_FIRE_RESCUE_SCORE_LOW_ENABLED": "0",
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


def _patch_entry_order_builder_constants(*, reason: str) -> bool:
    try:
        import trading.handlers.entry_order_builder as eob
        changes: dict[str, tuple[Any, Any]] = {}
        updates = {
            "ENTRY_ORDER_REQUIRE_ATR": True,
            "ENTRY_ORDER_REQUIRE_HIGH_LOW": True,
        }
        for key, val in updates.items():
            old = getattr(eob, key, None)
            if old != val:
                setattr(eob, key, val)
                changes[key] = (old, val)
        if changes:
            logger.warning("[STRICT ENTRY DEFAULTS] entry_order_builder constants patched reason=%s changed=%s version=%s", reason, changes, VERSION)
        return True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] entry_order_builder constants patch skipped reason=%s", reason, exc_info=True)
        return False


def _patch_summary_ai_thresholds(*, reason: str) -> bool:
    ok_any = False
    try:
        from trading.entry.summary_ai import candidates as c
        updates = {
            "DEFAULT_MIN_BUY_SCORE": 3.0,
            "DEFAULT_MAX_SELL_SCORE": 3.0,
            "DEFAULT_MIN_BUY_SLOPE": 0.0,
            "DEFAULT_MAX_SELL_SLOPE": 0.0,
        }
        changed = {}
        for key, val in updates.items():
            old = getattr(c, key, None)
            if old != val:
                setattr(c, key, val)
                changed[key] = (old, val)
        if changed:
            logger.warning("[STRICT ENTRY DEFAULTS] summary_ai candidates constants patched reason=%s changed=%s version=%s", reason, changed, VERSION)
        ok_any = True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] summary_ai candidates patch skipped reason=%s", reason, exc_info=True)

    try:
        from trading.entry.summary_ai import runner as r
        cur = getattr(r, "run_summary_ai_entry_from_df", None)
        if callable(cur) and not getattr(cur, "_strict_entry_defaults_runner_v3", False):
            orig = getattr(cur, "_original", cur)

            @wraps(orig)
            def _strict_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
                kwargs["min_buy_score"] = max(float(kwargs.get("min_buy_score", 0) or 0), 3.0)
                kwargs["max_sell_score"] = max(float(kwargs.get("max_sell_score", 0) or 0), 3.0)
                kwargs.setdefault("use_pre_slope_filter", True)
                return orig(*args, **kwargs)

            _strict_run_summary_ai_entry_from_df._strict_entry_defaults_runner_v3 = True  # type: ignore[attr-defined]
            _strict_run_summary_ai_entry_from_df._original = orig  # type: ignore[attr-defined]
            r.run_summary_ai_entry_from_df = _strict_run_summary_ai_entry_from_df
            logger.warning("[STRICT ENTRY DEFAULTS] summary_ai runner strict wrapper installed reason=%s version=%s", reason, VERSION)
        ok_any = True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] summary_ai runner patch skipped reason=%s", reason, exc_info=True)
    return ok_any


def _patch_tonosama_volume_surge(*, reason: str) -> bool:
    try:
        import pandas as pd
        import trading.entry.tonosama.volume_surge as vs

        def _strict_force_failopen_enabled() -> bool:
            return False

        def _strict_failopen_reason() -> str:
            return "strict_disabled force=False early=False legacy_failopen=False allow_without_history=False"

        vs._force_failopen_enabled = _strict_force_failopen_enabled
        vs._failopen_reason = _strict_failopen_reason

        cur = getattr(vs, "build_scalping_feature_df", None)
        if callable(cur) and not getattr(cur, "_strict_no_history_failopen_v3", False):
            orig = getattr(cur, "_original", cur)

            @wraps(orig)
            def _strict_build_scalping_feature_df(*args: Any, **kwargs: Any):
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

            _strict_build_scalping_feature_df._strict_no_history_failopen_v3 = True  # type: ignore[attr-defined]
            _strict_build_scalping_feature_df._original = orig  # type: ignore[attr-defined]
            vs.build_scalping_feature_df = _strict_build_scalping_feature_df
            logger.warning("[STRICT ENTRY DEFAULTS] TONOSAMA volume_surge strict wrapper installed reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] TONOSAMA volume_surge patch skipped reason=%s", reason, exc_info=True)
        return False


def _patch_ranking_source_fallback(*, reason: str) -> bool:
    try:
        # Existing wrapper reads env flags at runtime. Setting them in _STRICT_VALUES is enough.
        import core.startup.ranking_entry_source_db_fallback_patch as p
        logger.warning(
            "[STRICT ENTRY DEFAULTS] ranking source fallback strict flags reason=%s db_failopen=%s global_failopen=%s version=%s",
            reason,
            os.environ.get("RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE"),
            os.environ.get("RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL"),
            VERSION,
        )
        return True
    except Exception:
        logger.debug("[STRICT ENTRY DEFAULTS] ranking source fallback patch skipped reason=%s", reason, exc_info=True)
        return False


def _patch_runtime_modules(*, reason: str) -> dict[str, bool]:
    return {
        "entry_controller": _patch_entry_controller_constants(reason=reason),
        "entry_order_builder": _patch_entry_order_builder_constants(reason=reason),
        "summary_ai": _patch_summary_ai_thresholds(reason=reason),
        "tonosama_volume_surge": _patch_tonosama_volume_surge(reason=reason),
        "ranking_source_fallback": _patch_ranking_source_fallback(reason=reason),
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
        _apply_strict_values(reason=f"watcher:{i + 1}")
        result = _patch_runtime_modules(reason=f"watcher:{i + 1}")
        if i in (0, loops - 1) or not all(result.values()):
            logger.warning("[STRICT ENTRY DEFAULTS] watcher enforce i=%s/%s result=%s version=%s", i + 1, loops, result, VERSION)
    logger.warning("[STRICT ENTRY DEFAULTS] watcher done loops=%s sleep=%.2f version=%s", loops, sleep_sec, VERSION)


def install() -> bool:
    global _WATCHER_STARTED
    try:
        changed = _apply_strict_values(reason="install")
        result = _patch_runtime_modules(reason="install")
        if not _WATCHER_STARTED:
            _WATCHER_STARTED = True
            threading.Thread(target=_watcher_loop, name="strict-entry-defaults-watch", daemon=True).start()
        logger.warning(
            "[STRICT ENTRY DEFAULTS] installed version=%s changed=%s result=%s watcher=%s tonosama_force=%s tonosama_failopen=%s allow_without=%s allow_missing=%s drop_missing=%s summary_min=%s score_rescue=%s atr_failopen=%s range_failopen=%s ranking_db_failopen=%s ranking_global_failopen=%s direction_buy_failopen=%s direction_sell_failopen=%s",
            VERSION,
            changed,
            result,
            _WATCHER_STARTED,
            os.environ.get("TONOSAMA_FORCE_SURGE_FAILOPEN"),
            os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING"),
            os.environ.get("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY"),
            os.environ.get("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY"),
            os.environ.get("TONOSAMA_DROP_HISTORY_MISSING_ENTRY"),
            os.environ.get("SUMMARY_AI_MIN_SCORE"),
            os.environ.get("SUMMARY_AI_RESCUE_SCORE_LOW"),
            os.environ.get("ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN"),
            os.environ.get("RANGE_5M_FILTER_NG_FAIL_OPEN"),
            os.environ.get("RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE"),
            os.environ.get("RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL"),
            os.environ.get("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_BUY"),
            os.environ.get("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_SELL"),
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
