# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/entry_fire_rescue_runtime_patch.py
# Version: V3-ENTRY-FIRE-RESCUE-PRE-AI-SELL-CREDIT-FILTER
# ------------------------------------------------------------
# Purpose:
#   2026-06-12 logs showed entry did not reach order dispatch because:
#     - SUMMARY AI / candidate thresholds still assumed old 5.0/1.0 scale
#       while current PUSH scores are about 0.1-0.3.
#     - 3min/MTF readiness can be zero during rotation/Yahoo fallback, killing
#       candidates before 1min strong rows can be evaluated.
#     - ranking entry build is hard-capped at 18s and repeatedly enters cooldown
#       when elapsed is only slightly above 18s.
#     - SELL candidates with explicit short_ok=0 / sell_target=0 reached AI_OK
#       and were removed later, consuming the approved slot.
#
# V3:
#   - Remove explicit SELL-credit-NG rows before run_ai_gate_for_candidates().
#     This prevents non-shortable names such as 4889/7352 from becoming AI_OK
#     and wasting approved slots.  The existing approved-stage filter remains
#     as a second safety net.
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3-ENTRY-FIRE-RESCUE-PRE-AI-SELL-CREDIT-FILTER"
_INSTALLED = False
_WATCHER_STARTED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
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
        return float(str(v).replace(",", ""))
    except Exception:
        return default


def _set_default_envs() -> None:
    """Set operator-overridable defaults for the current low-score scale."""
    defaults = {
        "SUMMARY_ENTRY_MIN_SCORE": "0.12",
        "SUMMARY_AI_MIN_SCORE": "0.12",
        "ENTRY_GATE_MIN_SCORE": "0.12",
        "SUMMARY_AI_MIN_BUY_SCORE": "0.12",
        "SUMMARY_AI_MIN_SELL_SCORE": "0.12",
        "SUMMARY_AI_MAX_SELL_SCORE_FOR_BUY": "0.35",
        "SUMMARY_AI_MAX_BUY_SCORE_FOR_SELL": "0.35",
        "SUMMARY_ENTRY_ALLOW_1MIN_FALLBACK": "1",
        "SUMMARY_ENTRY_REQUIRE_3MIN_READY": "0",
        "SUMMARY_ENTRY_REQUIRE_MTF": "0",
        "SUMMARY_AI_REQUIRE_MTF": "0",
        "ENTRY_BYPASS_SLOPE_FILTER": "1",
        "SUMMARY_AI_DIRECT_LIQ_REQUIRE_DATA": "0",
        "SUMMARY_AI_SELL_CREDIT_PREFILTER": "1",
        "SUMMARY_AI_SELL_CREDIT_PREFILTER_BEFORE_AI": "1",
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


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".T"):
            s = s[:-2]
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _norm_side(v: Any, default: str = "BUY") -> str:
    try:
        s = str(v or default).strip().upper()
        return s if s in {"BUY", "SELL"} else default
    except Exception:
        return default


def _score_for_side(item: dict[str, Any]) -> float:
    side = _norm_side(item.get("side") or item.get("ai_side"), "BUY")
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


def _as_plain_dict(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return dict(v)
    try:
        if hasattr(v, "to_dict"):
            d = v.to_dict()
            if isinstance(d, dict):
                return dict(d)
    except Exception:
        pass
    return {}


def _merged_item_dicts(item: Any) -> list[dict[str, Any]]:
    root = _as_plain_dict(item)
    dicts: list[dict[str, Any]] = []
    if root:
        dicts.append(root)
        for k in ("ai_row", "source_row", "row", "summary_row"):
            v = root.get(k)
            if isinstance(v, dict):
                dicts.append(dict(v))
    return dicts


def _pick_from_item(item: Any, *keys: str) -> Any:
    for d in _merged_item_dicts(item):
        for k in keys:
            if k in d:
                return d.get(k)
    return None


def _flag_is_explicit_false(v: Any) -> bool:
    if v is None:
        return False
    try:
        if isinstance(v, bool):
            return not v
        s = str(v).strip().lower()
        if s in {"", "none", "nan", "null"}:
            return False
        return s in {"0", "false", "no", "n", "ng", "不可", "×", "x"}
    except Exception:
        return False


def _sell_credit_block_reason(item: Any) -> tuple[bool, str, dict[str, Any]]:
    """Return block reason only when the row explicitly says SELL is not allowed."""
    side = _norm_side(
        _pick_from_item(item, "side", "ai_side", "entry_decision", "signal"),
        "BUY",
    )
    if side != "SELL":
        return False, "", {}

    symbol = _norm_symbol(_pick_from_item(item, "symbol", "Symbol", "code", "銘柄コード"))
    checks = {
        "short_ok": _pick_from_item(item, "short_ok", "shortable", "short_sale_ok", "is_short_ok", "short_sellable"),
        "sell_target": _pick_from_item(item, "sell_target", "is_sell_target", "can_sell", "sellable"),
        "margin_sellable": _pick_from_item(item, "margin_sellable", "credit_sellable", "can_margin_sell"),
    }
    explicit_ng = {k: v for k, v in checks.items() if _flag_is_explicit_false(v)}
    if not explicit_ng:
        return False, "", {}

    detail = {"symbol": symbol, "side": side, **explicit_ng}
    return True, "sell_credit_guard_ng", detail


def _prefilter_sell_credit_ng_candidates(candidates: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Filter explicit SELL credit NG rows while preserving DataFrame/list shape."""
    if not _env_bool("SUMMARY_AI_SELL_CREDIT_PREFILTER_BEFORE_AI", True):
        return candidates, []
    if candidates is None:
        return candidates, []

    try:
        import pandas as pd
        if isinstance(candidates, pd.DataFrame):
            if candidates.empty:
                return candidates, []
            keep_idx = []
            skipped: list[dict[str, Any]] = []
            for idx, row in candidates.iterrows():
                item = row.to_dict()
                blocked, reason, detail = _sell_credit_block_reason(item)
                if blocked:
                    skipped.append({"reason": reason, **detail})
                    continue
                keep_idx.append(idx)
            if skipped:
                filtered = candidates.loc[keep_idx].copy()
                return filtered, skipped
            return candidates, []
    except Exception:
        logger.debug("[ENTRY FIRE RESCUE] dataframe SELL credit prefilter skipped", exc_info=True)

    try:
        seq = list(candidates or [])
    except Exception:
        return candidates, []
    kept = []
    skipped = []
    for item in seq:
        blocked, reason, detail = _sell_credit_block_reason(item)
        if blocked:
            skipped.append({"reason": reason, **detail})
            continue
        kept.append(item)
    return kept, skipped


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
        if getattr(cur, "_entry_fire_rescue_v3", False):
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(orig)
        def patched_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
            min_buy = _env_float("SUMMARY_AI_MIN_BUY_SCORE", _env_float("SUMMARY_ENTRY_MIN_SCORE", 0.12))
            min_sell = _env_float("SUMMARY_AI_MIN_SELL_SCORE", _env_float("SUMMARY_ENTRY_MIN_SCORE", 0.12))
            if "min_buy_score" not in kwargs or _safe_float(kwargs.get("min_buy_score"), 999.0) >= 1.0:
                kwargs["min_buy_score"] = min_buy
            if "max_sell_score" not in kwargs or _safe_float(kwargs.get("max_sell_score"), 999.0) >= 1.0:
                kwargs["max_sell_score"] = min_sell
            kwargs.setdefault("use_pre_slope_filter", not _env_bool("ENTRY_BYPASS_SLOPE_FILTER", True))
            return orig(*args, **kwargs)

        patched_run_summary_ai_entry_from_df._entry_fire_rescue_v3 = True  # type: ignore[attr-defined]
        patched_run_summary_ai_entry_from_df._entry_fire_rescue_v2 = True  # type: ignore[attr-defined]
        patched_run_summary_ai_entry_from_df._original = orig  # type: ignore[attr-defined]
        r.run_summary_ai_entry_from_df = patched_run_summary_ai_entry_from_df
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
        if getattr(cur, "_entry_fire_rescue_v3", False):
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(orig)
        def patched_run_ai_gate_for_candidates(*args: Any, **kwargs: Any):
            call_args = list(args)
            call_kwargs = dict(kwargs)
            candidate_obj = call_args[0] if call_args else call_kwargs.get("candidates_df")
            filtered_obj, skipped = _prefilter_sell_credit_ng_candidates(candidate_obj)
            if skipped:
                if call_args:
                    call_args[0] = filtered_obj
                else:
                    call_kwargs["candidates_df"] = filtered_obj
                logger.warning(
                    "[ENTRY FIRE RESCUE] SUMMARY_AI SELL credit pre-AI filter before=%s after=%s skipped=%s",
                    len(candidate_obj) if hasattr(candidate_obj, "__len__") else -1,
                    len(filtered_obj) if hasattr(filtered_obj, "__len__") else -1,
                    skipped[:80],
                )

            results = orig(*call_args, **call_kwargs)
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

        patched_run_ai_gate_for_candidates._entry_fire_rescue_v3 = True  # type: ignore[attr-defined]
        patched_run_ai_gate_for_candidates._entry_fire_rescue_v2 = True  # type: ignore[attr-defined]
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


def _patch_sell_credit_prefilter() -> bool:
    try:
        from trading.entry.summary_ai import executor as e

        cur = getattr(e, "_filter_blocked_ai_ok_items", None)
        if not callable(cur):
            return False
        if getattr(cur, "_entry_fire_rescue_sell_credit_v3", False):
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(orig)
        def patched_filter_blocked_ai_ok_items(ok_items):
            if not _env_bool("SUMMARY_AI_SELL_CREDIT_PREFILTER", True):
                return orig(ok_items)
            if not ok_items:
                return orig(ok_items)
            try:
                kept, skipped = _prefilter_sell_credit_ng_candidates(ok_items)
                if skipped:
                    logger.warning(
                        "[ENTRY FIRE RESCUE] SUMMARY_AI SELL credit approved-stage safety filter before=%s after=%s skipped=%s",
                        len(ok_items),
                        len(kept) if hasattr(kept, "__len__") else -1,
                        skipped[:80],
                    )
                return orig(kept)
            except Exception:
                logger.exception("[ENTRY FIRE RESCUE] SELL credit prefilter failed; fail-open to original")
                return orig(ok_items)

        patched_filter_blocked_ai_ok_items._entry_fire_rescue_sell_credit_v3 = True  # type: ignore[attr-defined]
        patched_filter_blocked_ai_ok_items._entry_fire_rescue_sell_credit_v2 = True  # type: ignore[attr-defined]
        patched_filter_blocked_ai_ok_items._original = orig  # type: ignore[attr-defined]
        e._filter_blocked_ai_ok_items = patched_filter_blocked_ai_ok_items
        return True
    except Exception:
        logger.debug("[ENTRY FIRE RESCUE] sell credit prefilter patch skipped", exc_info=True)
        return False


def _patch_ranking_timeout_controller() -> bool:
    try:
        import core.startup.ranking_entry_controller_timeout_patch as p

        if getattr(p, "_entry_fire_rescue_relaxed_v3", False):
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
        p._entry_fire_rescue_relaxed_v3 = True
        p._entry_fire_rescue_relaxed_v2 = True
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
        "sell_credit_prefilter": _patch_sell_credit_prefilter(),
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
