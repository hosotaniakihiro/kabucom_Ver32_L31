# ============================================================
# File   : core/startup/summary_ai_more_candidates_patch.py
# Version: Ver1.6-SUMMARY-AI-LOWMOVE-POOL-REFILL
# ------------------------------------------------------------
# Purpose:
#   AIに「もっとエントリーできるか」を確認させるため、
#   SUMMARY_AI runner へ渡す候補数を起動時に拡張する。
#
# Ver1.6:
#   - SUMMARY AI LOW MOVE PREFILTER が全候補を LOW_MOVE と見て
#     safety rescue で1件だけ残し、その1件が blowoff/後段NGで終わる問題を補正。
#   - pre-approval の候補プールだけ複数件へ戻す。
#   - 低出来高・低変動・blowoff・板ガードは緩めない。後段 entry_pipeline/final guard は従来通り通す。
#
# Ver1.5:
#   - strict default は 3.00 なのに、後段 runner が min_buy=4.00 で動き、
#     blowoff/low-move後の補充候補が足りなくなる問題を修正。
#   - min_buy_score / max_sell_score を「緩和」ではなく strict基準の3.00へ上限補正する。
#   - top_n / candidate_limit / max_candidates も未指定時だけ拡張する。
#
# Ver1.4:
#   - A案: SUMMARY_AI の SELL 候補を AI gate 前に short_ok=1 だけへ絞る。
#     short_ok=0 / sell_target=0 の銘柄は SELL_TOP_READY に出さない。
# ============================================================

from __future__ import annotations

import functools
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "Ver1.6-SUMMARY-AI-LOWMOVE-POOL-REFILL"
_INSTALLED = False
_SELL_SHORT_OK_FILTER_INSTALLED = False
_LOW_MOVE_POOL_PATCHED = False
_LOW_MOVE_POOL_WATCHER_STARTED = False


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _row_dict(x: Any) -> dict[str, Any]:
    try:
        if isinstance(x, dict):
            return x
        if hasattr(x, "to_dict"):
            d = x.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _pick_symbol(x: Any) -> str:
    d = _row_dict(x)
    return str(d.get("symbol") or d.get("Symbol") or getattr(x, "symbol", "") or "").strip()


def _safe_symbol_list(obj: Any, n: int = 20) -> list[str]:
    try:
        if obj is not None and hasattr(obj, "empty") and not obj.empty and "symbol" in obj.columns:
            return list(obj["symbol"].astype(str).head(n))
    except Exception:
        pass
    try:
        return [_pick_symbol(x) for x in list(obj or [])[:n] if _pick_symbol(x)]
    except Exception:
        return []


def _score_for_side_local(x: Any) -> float:
    d = _row_dict(x)
    side = str(d.get("side") or d.get("ai_side") or d.get("entry_side") or d.get("entry_decision") or "").upper()
    if side == "SELL":
        return max(
            _safe_float(d.get("score_sell"), 0.0),
            _safe_float(d.get("sell_score"), 0.0),
            abs(_safe_float(d.get("score_total"), 0.0)) if _safe_float(d.get("score_total"), 0.0) < 0 else 0.0,
            abs(_safe_float(d.get("final_score"), 0.0)) if _safe_float(d.get("final_score"), 0.0) < 0 else 0.0,
        )
    return max(
        _safe_float(d.get("score_buy"), 0.0),
        _safe_float(d.get("buy_score"), 0.0),
        _safe_float(d.get("score_total"), 0.0),
        _safe_float(d.get("final_score"), 0.0),
        _safe_float(d.get("score"), 0.0),
    )


def _sort_candidates_local(items: list[Any]) -> list[Any]:
    try:
        import trading.entry.summary_ai.executor as ex
        sort_key = getattr(ex, "_sort_key", None)
        if callable(sort_key):
            return sorted(items, key=sort_key, reverse=True)
    except Exception:
        pass
    return sorted(items, key=_score_for_side_local, reverse=True)


def _install_controller_enrich_patch() -> bool:
    try:
        from core.startup.summary_controller_enrich_runtime_patch import install as install_enrich
        ok = bool(install_enrich())
        logger.warning("[SUMMARY AI MORE CANDIDATES PATCH] summary_controller_enrich_runtime_patch installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES PATCH] summary_controller_enrich_runtime_patch install failed")
        return False


def _install_final_gate_relax_patch() -> bool:
    try:
        from core.startup.entry_controller_final_gate_relax_patch import install as install_relax
        ok = bool(install_relax())
        logger.warning("[SUMMARY AI MORE CANDIDATES PATCH] entry_controller_final_gate_relax_patch installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES PATCH] entry_controller_final_gate_relax_patch install failed")
        return False


def _install_direct_dispatch_patch() -> bool:
    try:
        from core.startup.summary_ai_async_direct_dispatch_patch import install as install_direct
        ok = bool(install_direct())
        logger.warning("[SUMMARY AI MORE CANDIDATES PATCH] summary_ai_async_direct_dispatch_patch installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES PATCH] summary_ai_async_direct_dispatch_patch install failed")
        return False


def _install_sell_short_ok_filter_patch() -> bool:
    global _SELL_SHORT_OK_FILTER_INSTALLED
    if _SELL_SHORT_OK_FILTER_INSTALLED:
        return True
    if not _env_bool("SUMMARY_AI_SELL_SHORT_OK_PREFILTER", True):
        logger.warning("[SUMMARY AI SELL SHORT_OK PREFILTER] disabled by env")
        return False
    try:
        import pandas as pd
        import trading.entry.summary_ai.candidates as candidates
        from AI.sell_credit_guard import can_sell_symbol

        current = getattr(candidates, "_sell_candidates_from_prepared", None)
        if not callable(current):
            logger.warning("[SUMMARY AI SELL SHORT_OK PREFILTER] target not callable")
            return False
        if getattr(current, "_summary_ai_sell_short_ok_prefilter_v1", False):
            _SELL_SHORT_OK_FILTER_INSTALLED = True
            return True

        original = current

        @functools.wraps(original)
        def _wrapped_sell_candidates_from_prepared(*args: Any, **kwargs: Any):
            out = original(*args, **kwargs)
            try:
                if not isinstance(out, pd.DataFrame) or out.empty:
                    return out
                before = len(out)
                keep_idx: list[int] = []
                skipped: list[dict[str, Any]] = []
                for idx, row in out.iterrows():
                    try:
                        row_dict = row.to_dict() if hasattr(row, "to_dict") else dict(row)
                        symbol = str(row_dict.get("symbol") or "").strip()
                        if can_sell_symbol(row_dict, default=False):
                            keep_idx.append(idx)
                        else:
                            skipped.append({"symbol": symbol, "reason": "short_ok_not_1"})
                    except Exception as e:
                        skipped.append({"symbol": str(getattr(row, "symbol", "")), "reason": f"guard_error:{e}"})
                if len(keep_idx) == before:
                    return out
                filtered = out.loc[keep_idx].copy().reset_index(drop=True) if keep_idx else out.iloc[0:0].copy()
                logger.warning(
                    "[SUMMARY AI SELL SHORT_OK PREFILTER] filtered before=%s after=%s skipped=%s kept_symbols=%s",
                    before,
                    len(filtered),
                    skipped[:20],
                    _safe_symbol_list(filtered, 20),
                )
                return filtered
            except Exception:
                logger.exception("[SUMMARY AI SELL SHORT_OK PREFILTER] failed; return original candidates")
                return out

        _wrapped_sell_candidates_from_prepared._summary_ai_sell_short_ok_prefilter_v1 = True  # type: ignore[attr-defined]
        _wrapped_sell_candidates_from_prepared._original = original  # type: ignore[attr-defined]
        candidates._sell_candidates_from_prepared = _wrapped_sell_candidates_from_prepared
        _SELL_SHORT_OK_FILTER_INSTALLED = True
        logger.warning("[SUMMARY AI SELL SHORT_OK PREFILTER] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY AI SELL SHORT_OK PREFILTER] install failed")
        return False


def _install_low_move_pool_refill_patch(reason: str = "install") -> bool:
    """Keep more candidates when the pre-approval low-move wrapper rescues only one row.

    This does not submit orders directly and does not bypass final guards. It only prevents
    a single rescued blowoff/low-liquidity row from starving the candidate pool.
    """
    global _LOW_MOVE_POOL_PATCHED
    if not _env_bool("SUMMARY_AI_LOW_MOVE_POOL_REFILL", True):
        return False
    try:
        import trading.entry.summary_ai.executor as ex
        cur = getattr(ex, "_filter_blocked_ai_ok_items", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_low_move_pool_refill_v16", False):
            _LOW_MOVE_POOL_PATCHED = True
            return True

        original = cur

        @functools.wraps(original)
        def _filter_blocked_ai_ok_items_pool_refill(ok_items):
            out = original(ok_items)
            try:
                raw_items = list(ok_items or [])
                out_items = list(out or [])
                min_before = _env_int("SUMMARY_AI_LOW_MOVE_POOL_REFILL_MIN_BEFORE", 4)
                trigger_after = _env_int("SUMMARY_AI_LOW_MOVE_POOL_REFILL_TRIGGER_AFTER", 1)
                pool_n = max(1, _env_int("SUMMARY_AI_LOW_MOVE_POOL_REFILL_SIZE", _env_int("SUMMARY_AI_EXECUTOR_SELECTION_POOL", 12)))
                if len(raw_items) >= min_before and len(out_items) <= trigger_after:
                    ordered = _sort_candidates_local(raw_items)
                    refill = ordered[:min(pool_n, len(ordered))]
                    # 既存フィルタ結果を先頭に残し、重複symbolを避けて補充。
                    seen = set(_safe_symbol_list(out_items, 100))
                    merged = list(out_items)
                    for x in refill:
                        sym = _pick_symbol(x)
                        if sym and sym in seen:
                            continue
                        merged.append(x)
                        if sym:
                            seen.add(sym)
                        if len(merged) >= pool_n:
                            break
                    logger.warning(
                        "[SUMMARY AI LOW MOVE POOL REFILL] applied reason=%s before=%s after_original=%s after_refill=%s original_symbols=%s refill_symbols=%s version=%s",
                        reason,
                        len(raw_items),
                        len(out_items),
                        len(merged),
                        _safe_symbol_list(out_items, 20),
                        _safe_symbol_list(merged, 20),
                        VERSION,
                    )
                    return merged
            except Exception:
                logger.exception("[SUMMARY AI LOW MOVE POOL REFILL] failed; use original filtered output")
            return out

        _filter_blocked_ai_ok_items_pool_refill._summary_ai_low_move_pool_refill_v16 = True  # type: ignore[attr-defined]
        _filter_blocked_ai_ok_items_pool_refill._original = original  # type: ignore[attr-defined]
        ex._filter_blocked_ai_ok_items = _filter_blocked_ai_ok_items_pool_refill
        _LOW_MOVE_POOL_PATCHED = True
        logger.warning("[SUMMARY AI LOW MOVE POOL REFILL] installed reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.debug("[SUMMARY AI LOW MOVE POOL REFILL] install not ready reason=%s", reason, exc_info=True)
        return False


def _start_low_move_pool_watcher() -> None:
    global _LOW_MOVE_POOL_WATCHER_STARTED
    if _LOW_MOVE_POOL_WATCHER_STARTED:
        return
    _LOW_MOVE_POOL_WATCHER_STARTED = True

    def _watch() -> None:
        loops = max(1, _env_int("SUMMARY_AI_LOW_MOVE_POOL_REFILL_WATCH_LOOPS", 20))
        sleep_sec = max(0.5, _env_float("SUMMARY_AI_LOW_MOVE_POOL_REFILL_WATCH_SLEEP", 1.0))
        for i in range(loops):
            ok = _install_low_move_pool_refill_patch(reason=f"watcher:{i}")
            if i in (0, loops - 1):
                logger.warning("[SUMMARY AI LOW MOVE POOL REFILL] enforce i=%s/%s ok=%s version=%s", i, loops, ok, VERSION)
            time.sleep(sleep_sec)

    threading.Thread(target=_watch, name="summary-ai-low-move-pool-refill", daemon=True).start()
    logger.warning("[SUMMARY AI LOW MOVE POOL REFILL] watcher started version=%s", VERSION)


def _strict_score_floor() -> float:
    return max(
        0.01,
        _env_float("SUMMARY_AI_MIN_SCORE", _env_float("SUMMARY_ENTRY_MIN_SCORE", _env_float("MIN_ENTRY_SCORE", 3.0))),
    )


def _clamp_summary_ai_scores(kwargs: dict[str, Any]) -> dict[str, tuple[float | None, float]]:
    """Keep runner thresholds aligned to strict defaults. This avoids accidental min_buy=4.00 starvation."""
    strict_min = _strict_score_floor()
    changed: dict[str, tuple[float | None, float]] = {}
    for key in ("min_buy_score", "min_buy", "min_score"):
        old_raw = kwargs.get(key)
        old = _safe_float(old_raw, 0.0) if old_raw is not None else None
        if old is None or old <= 0 or old > strict_min:
            kwargs[key] = strict_min
            changed[key] = (old, strict_min)
    for key in ("max_sell_score", "min_sell_score", "max_sell", "min_sell"):
        old_raw = kwargs.get(key)
        old = _safe_float(old_raw, 0.0) if old_raw is not None else None
        if old is None or old <= 0 or old > strict_min:
            kwargs[key] = strict_min
            changed[key] = (old, strict_min)
    return changed


def install() -> bool:
    global _INSTALLED

    _install_controller_enrich_patch()
    _install_final_gate_relax_patch()
    _install_direct_dispatch_patch()
    _install_sell_short_ok_filter_patch()
    _install_low_move_pool_refill_patch()
    _start_low_move_pool_watcher()

    if _INSTALLED:
        logger.warning("[SUMMARY AI MORE CANDIDATES PATCH] already installed")
        return True

    if not _env_bool("SUMMARY_AI_MORE_CANDIDATES_ENABLED", True):
        logger.warning("[SUMMARY AI MORE CANDIDATES PATCH] disabled by env")
        return False

    try:
        import trading.entry.summary_ai.runner as runner
        import trading.entry.summary_ai.candidates as candidates

        top_n = max(60, _env_int("SUMMARY_AI_ENTRY_TOP_N", _env_int("SUMMARY_AI_TOP_N", 60)))
        tonosama_max = max(60, _env_int("SUMMARY_AI_TONOSAMA_MAX_CANDIDATES", top_n))
        bypass_slope = _env_bool("SUMMARY_AI_ENTRY_BYPASS_SLOPE_FILTER", False)

        try:
            runner.DEFAULT_AI_ENTRY_TOP_N = top_n
            runner.DEFAULT_TONOSAMA_AI_CANDIDATES = tonosama_max
        except Exception:
            pass

        try:
            candidates.DEFAULT_TOP_N = top_n
        except Exception:
            pass

        original = getattr(runner, "run_summary_ai_entry_from_df", None)
        if not callable(original):
            logger.error("[SUMMARY AI MORE CANDIDATES PATCH] runner.run_summary_ai_entry_from_df not callable")
            return False

        if getattr(original, "_summary_ai_more_candidates_v16", False):
            _INSTALLED = True
            return True

        @functools.wraps(original)
        def _wrapped_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
            _install_low_move_pool_refill_patch(reason="before_run")
            explicit_top_n = any(k in kwargs for k in ("top_n", "max_candidates", "candidate_limit"))
            if not explicit_top_n:
                kwargs["top_n"] = top_n
            else:
                try:
                    kwargs["top_n"] = max(int(kwargs.get("top_n") or 0), top_n)
                except Exception:
                    kwargs["top_n"] = top_n

            kwargs.setdefault("max_candidates", top_n)
            kwargs.setdefault("candidate_limit", top_n)

            if "tonosama_max_candidates" not in kwargs:
                kwargs["tonosama_max_candidates"] = tonosama_max

            if bypass_slope and "use_pre_slope_filter" not in kwargs:
                kwargs["use_pre_slope_filter"] = False

            score_changes = _clamp_summary_ai_scores(kwargs)
            logger.warning(
                "[SUMMARY AI MORE CANDIDATES PATCH] run source=%s interval=%s top_n=%s max_candidates=%s candidate_limit=%s tonosama_max=%s bypass_slope=%s explicit_top_n=%s score_changes=%s sell_short_ok_prefilter=True low_move_pool_refill=%s version=%s",
                kwargs.get("source", "SUMMARY"),
                kwargs.get("interval", 1),
                kwargs.get("top_n"),
                kwargs.get("max_candidates"),
                kwargs.get("candidate_limit"),
                kwargs.get("tonosama_max_candidates"),
                bypass_slope,
                explicit_top_n,
                score_changes,
                _LOW_MOVE_POOL_PATCHED,
                VERSION,
            )
            return original(*args, **kwargs)

        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v1 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v12 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v13 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v14 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v15 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v16 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._original = original  # type: ignore[attr-defined]
        runner.run_summary_ai_entry_from_df = _wrapped_run_summary_ai_entry_from_df

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI MORE CANDIDATES PATCH] installed top_n=%s tonosama_max=%s bypass_slope=%s strict_min=%.2f final_gate_relax=True direct_dispatch=True sell_short_ok_prefilter=True low_move_pool_refill=True version=%s",
            top_n,
            tonosama_max,
            bypass_slope,
            _strict_score_floor(),
            VERSION,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES PATCH] install failed")
        return False


__all__ = ["install", "VERSION"]
