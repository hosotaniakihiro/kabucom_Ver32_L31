# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/entry_fire_rescue_runtime_patch.py
# Version: V1-ENTRY-FIRE-RESCUE-LOW-SCORE-RANKING-TIMEOUT
# ------------------------------------------------------------
# Purpose:
#   2026-06-12 logs showed entry did not reach order dispatch because:
#     - SUMMARY AI / candidate thresholds still assumed old 5.0/1.0 scale
#       while current PUSH scores are about 0.1-0.3.
#     - 3min/MTF readiness can be zero during rotation/Yahoo fallback, killing
#       candidates before 1min strong rows can be evaluated.
#     - ranking entry build is hard-capped at 18s and repeatedly enters cooldown
#       when elapsed is only slightly above 18s.
#
#   This patch is intentionally runtime-only and fail-open only for the score
#   scale mismatch. Liquidity / price / daily-risk / order guards remain intact.
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-ENTRY-FIRE-RESCUE-LOW-SCORE-RANKING-TIMEOUT"
_INSTALLED = False
_WATCHER_STARTED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _set_default_envs() -> None:
    """Set operator-overridable defaults for the current low-score scale."""
    defaults = {
        # Current summary score scale in logs is roughly 0.01-0.33, not 1-5.
        "SUMMARY_ENTRY_MIN_SCORE": "0.12",
        "SUMMARY_AI_MIN_SCORE": "0.12",
        "ENTRY_GATE_MIN_SCORE": "0.12",
        "SUMMARY_AI_MIN_BUY_SCORE": "0.12",
        "SUMMARY_AI_MIN_SELL_SCORE": "0.12",
        "SUMMARY_AI_MAX_SELL_SCORE_FOR_BUY": "0.35",
        "SUMMARY_AI_MAX_BUY_SCORE_FOR_SELL": "0.35",
        # Let strong 1min rows continue even while 3min/MTF/display_ready is warming.
        "SUMMARY_ENTRY_ALLOW_1MIN_FALLBACK": "1",
        "SUMMARY_ENTRY_REQUIRE_3MIN_READY": "0",
        "SUMMARY_ENTRY_REQUIRE_MTF": "0",
        "SUMMARY_AI_REQUIRE_MTF": "0",
        "ENTRY_BYPASS_SLOPE_FILTER": "1",
        # AI gate direct dispatch should not fail solely because summary DB has no
        # freshly materialized row during PUSH rotation; later order guards still run.
        "SUMMARY_AI_DIRECT_LIQ_REQUIRE_DATA": "0",
        # Ranking: avoid 18s timeout -> 30s cooldown loop observed in logs.
        "RANKING_ENTRY_BUILD_TIMEOUT_SEC": "30",
        "RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC": "18",
        "RANKING_ENTRY_RUNTIME_BUDGET_SEC": "30",
        "RANKING_ENTRY_TIMEOUT_COOLDOWN_SEC": "10",
        "RANKING_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC": "10",
        "RANKING_ENTRY_FAST_MAX_SYMBOLS": "30",
        "RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS": "30",
        "RANKING_ENTRY_FAST_MAX_PER_SIDE": "15",
        "RANKING_ENTRY_FAST_MAX_PER_TYPE": "8",
        "RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS": "200",
    }
    for k, v in defaults.items():
        os.environ.setdefault(k, v)


def _score_for_side(item: dict[str, Any]) -> float:
    side = str(item.get("side") or item.get("ai_side") or "BUY").upper()
    if side == "SELL":
        return max(
            _safe_float(item.get("sell_score")),
            abs(_safe_float(item.get("score_total"))),
            abs(_safe_float(item.get("final_score"))),
        )
    return max(
        _safe_float(item.get("buy_score")),
        _safe_float(item.get("score_total")),
        _safe_float(item.get("final_score")),
    )


def _patch_candidates() -> bool:
    try:
        from trading.entry.summary_ai import candidates as c

        c.DEFAULT_MIN_BUY_SCORE = min(float(getattr(c, "DEFAULT_MIN_BUY_SCORE", 5.0)), _env_float("SUMMARY_AI_MIN_BUY_SCORE", 0.12))
        c.DEFAULT_MAX_SELL_SCORE = min(float(getattr(c, "DEFAULT_MAX_SELL_SCORE", 2.0)), _env_float("SUMMARY_AI_MIN_SELL_SCORE", 0.12))
        c.DEFAULT_MIN_BUY_SLOPE = min(float(getattr(c, "DEFAULT_MIN_BUY_SLOPE", 0.01)), _env_float("SUMMARY_AI_MIN_BUY_SLOPE", 0.0))
        c.DEFAULT_MAX_SELL_SLOPE = max(float(getattr(c, "DEFAULT_MAX_SELL_SLOPE", -0.01)), _env_float("SUMMARY_AI_MAX_SELL_SLOPE", 0.0))
        return True
    except Exception:
        logger.debug("[ENTRY FIRE RESCUE] candidates patch skipped", exc_info=True)
        return False


def _patch_runner() -> bool:
    try:
        from trading.entry.summary_ai import runner as r

        cur = getattr(r, "run_summary_ai_entry_from_df", None)
        if not callable(cur):
            return False
        if getattr(cur, "_entry_fire_rescue_v1", False):
            return True
        orig = cur

        @wraps(orig)
        def patched_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
            min_buy = _env_float("SUMMARY_AI_MIN_BUY_SCORE", _env_float("SUMMARY_ENTRY_MIN_SCORE", 0.12))
            min_sell = _env_float("SUMMARY_AI_MIN_SELL_SCORE", _env_float("SUMMARY_ENTRY_MIN_SCORE", 0.12))
            # If callers still pass old-scale defaults (5.0 / 2.0 / 1.0), convert to current score scale.
            if "min_buy_score" not in kwargs or _safe_float(kwargs.get("min_buy_score"), 999.0) >= 1.0:
                kwargs["min_buy_score"] = min_buy
            if "max_sell_score" not in kwargs or _safe_float(kwargs.get("max_sell_score"), 999.0) >= 1.0:
                kwargs["max_sell_score"] = min_sell
            kwargs.setdefault("use_pre_slope_filter", not _env_bool("ENTRY_BYPASS_SLOPE_FILTER", True))
            return orig(*args, **kwargs)

        patched_run_summary_ai_entry_from_df._entry_fire_rescue_v1 = True  # type: ignore[attr-defined]
        patched_run_summary_ai_entry_from_df._original = orig  # type: ignore[attr-defined]
        r.run_summary_ai_entry_from_df = patched_run_summary_ai_entry_from_df

        # Aliases call run_summary_ai_entry_from_df by module global, so replacing it is enough.
        try:
            r.DEFAULT_MIN_TOP10_SLOPE = min(float(getattr(r, "DEFAULT_MIN_TOP10_SLOPE", 0.01)), 0.0)
        except Exception:
            pass
        return True
    except Exception:
        logger.debug("[ENTRY FIRE RESCUE] runner patch skipped", exc_info=True)
        return False


def _patch_ai_gate() -> bool:
    try:
        from trading.entry.summary_ai import ai_gate_runner as g

        cur = getattr(g, "run_ai_gate_for_candidates", None)
        if not callable(cur):
            return False
        if getattr(cur, "_entry_fire_rescue_v1", False):
            return True
        orig = cur

        @wraps(orig)
        def patched_run_ai_gate_for_candidates(*args: Any, **kwargs: Any):
            results = orig(*args, **kwargs)
            if not _env_bool("SUMMARY_AI_RESCUE_SCORE_LOW", True):
                return results
            threshold = _env_float("SUMMARY_AI_MIN_SCORE", _env_float("SUMMARY_ENTRY_MIN_SCORE", 0.12))
            rescued = []
            try:
                for item in list(results or []):
                    if not isinstance(item, dict):
                        continue
                    if bool(item.get("allow")):
                        continue
                    reason = str(item.get("reason") or "")
                    if "score_low" not in reason:
                        continue
                    score = _score_for_side(item)
                    if score < threshold:
                        continue
                    item["allow"] = True
                    item["confidence"] = max(_safe_float(item.get("confidence"), 0.0), _env_float("SUMMARY_AI_SCORE_LOW_RESCUE_CONFIDENCE", 0.66))
                    item["reason"] = f"{reason}|score_low_rescued:{score:.4f}>={threshold:.4f}"
                    item["model_used"] = str(item.get("model_used") or "SCORE_LOW_RESCUE")
                    rescued.append(str(item.get("symbol") or ""))
                if rescued:
                    logger.warning("[ENTRY FIRE RESCUE] SUMMARY_AI score_low rescued threshold=%.4f symbols=%s", threshold, rescued[:30])
            except Exception:
                logger.exception("[ENTRY FIRE RESCUE] score_low rescue failed")
            return results

        patched_run_ai_gate_for_candidates._entry_fire_rescue_v1 = True  # type: ignore[attr-defined]
        patched_run_ai_gate_for_candidates._original = orig  # type: ignore[attr-defined]
        g.run_ai_gate_for_candidates = patched_run_ai_gate_for_candidates
        try:
            from trading.entry.summary_ai import runner as r
            r.run_ai_gate_for_candidates = patched_run_ai_gate_for_candidates
        except Exception:
            pass
        return True
    except Exception:
        logger.debug("[ENTRY FIRE RESCUE] ai gate patch skipped", exc_info=True)
        return False


def _patch_ranking_timeout_controller() -> bool:
    try:
        import core.startup.ranking_entry_controller_timeout_patch as p

        if getattr(p, "_entry_fire_rescue_relaxed_v1", False):
            return True

        def relaxed_force_runtime_timeouts(tasks) -> None:
            try:
                build_cap = p._cap_env_float("RANKING_ENTRY_BUILD_TIMEOUT_SEC", default=30.0, lower=10.0, upper=30.0, force=True)
                controller_cap = p._cap_env_float("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", default=18.0, lower=8.0, upper=20.0, force=True)
                runtime_cap = p._cap_env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", default=30.0, lower=10.0, upper=30.0, force=True)
                max_pending = p._cap_env_int("RANKING_ENTRY_MAX_PENDING_PER_RUN", default=3, lower=1, upper=3, force=True)
                os.environ["RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS"] = str(p._cap_env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", default=30, lower=8, upper=30, force=True))
                os.environ["RANKING_ENTRY_FAST_MAX_SYMBOLS"] = str(p._cap_env_int("RANKING_ENTRY_FAST_MAX_SYMBOLS", default=30, lower=8, upper=30, force=True))
                os.environ["RANKING_ENTRY_FAST_MAX_PER_SIDE"] = str(p._cap_env_int("RANKING_ENTRY_FAST_MAX_PER_SIDE", default=15, lower=3, upper=15, force=True))
                os.environ["RANKING_ENTRY_FAST_MAX_PER_TYPE"] = str(p._cap_env_int("RANKING_ENTRY_FAST_MAX_PER_TYPE", default=8, lower=2, upper=8, force=True))
                os.environ["RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS"] = str(p._cap_env_int("RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS", default=200, lower=80, upper=200, force=True))
                tasks.RANKING_ENTRY_BUILD_TIMEOUT_SEC = build_cap
                tasks.RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC = controller_cap
                try:
                    tasks.RANKING_ENTRY_MAX_PENDING_PER_RUN = max_pending
                except Exception:
                    pass
                logger.warning(
                    "[ENTRY FIRE RESCUE] relaxed ranking caps build=%.1fs controller=%.1fs runtime=%.1fs max_pending=%s",
                    build_cap,
                    controller_cap,
                    runtime_cap,
                    max_pending,
                )
            except Exception:
                logger.debug("[ENTRY FIRE RESCUE] relaxed ranking caps failed", exc_info=True)

        p._force_runtime_timeouts = relaxed_force_runtime_timeouts
        p._entry_fire_rescue_relaxed_v1 = True
        return True
    except Exception:
        logger.debug("[ENTRY FIRE RESCUE] ranking timeout patch skipped", exc_info=True)
        return False


def _patch_once() -> dict[str, bool]:
    _set_default_envs()
    return {
        "candidates": _patch_candidates(),
        "runner": _patch_runner(),
        "ai_gate": _patch_ai_gate(),
        "ranking_timeout": _patch_ranking_timeout_controller(),
    }


def _watch_loop() -> None:
    loops = max(1, int(_env_float("ENTRY_FIRE_RESCUE_WATCH_LOOPS", 24)))
    sleep_sec = max(0.5, _env_float("ENTRY_FIRE_RESCUE_WATCH_SLEEP_SEC", 1.0))
    for i in range(loops):
        result = _patch_once()
        if i in (0, loops - 1) or all(result.values()):
            logger.warning("[ENTRY FIRE RESCUE] enforce i=%s/%s result=%s", i + 1, loops, result)
        if all(result.values()):
            break
        time.sleep(sleep_sec)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("ENTRY_FIRE_RESCUE_ENABLED", True):
        logger.warning("[ENTRY FIRE RESCUE] disabled by env")
        return False
    result = _patch_once()
    _INSTALLED = True
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watch_loop, name="entry-fire-rescue-enforcer", daemon=True).start()
    logger.warning("[ENTRY FIRE RESCUE] installed version=%s result=%s watcher=%s", VERSION, result, _WATCHER_STARTED)
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY FIRE RESCUE] auto install failed")


__all__ = ["VERSION", "install"]
