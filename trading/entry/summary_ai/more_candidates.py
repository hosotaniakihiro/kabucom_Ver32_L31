# ============================================================
# File   : trading/entry/summary_ai/more_candidates.py
# Version: Ver1.8-SUMMARY-AI-APPROVED-ROW-RANGE-REPAIR
# ------------------------------------------------------------
# 旧 core/startup/summary_ai_more_candidates_patch.py を summary_ai 側へ移設した。
#
# 目的: AI に「もっとエントリーできるか」を確認させるため、
#       SUMMARY_AI runner へ渡す候補数を広げ、スコアしきい値を strict 側へ揃える。
#
# 旧 install() の内訳と行方:
#   _install_final_gate_relax_patch   -> trading/handlers/entry_controller.
#                                        apply_final_gate_relax_constants()
#   _install_range_pct_repair_patch   -> trading/entry/summary_ai/candidates.
#                                        _repair_range_pct_columns()
#                                        (attach_display_like_columns 本体の最終段)
#   _install_sell_short_ok_filter_patch -> trading/entry/summary_ai/candidates.
#                                        _apply_sell_short_ok_prefilter()
#                                        (_sell_candidates_from_prepared 本体)
#   _install_approved_row_range_repair_patch -> **削除**。
#                                        summary_ai_approved_row_range_repair_patch は
#                                        executor.build_approved_row へ本文化済みで、
#                                        中身が空の stub になっていた。
#   _install_low_move_pool_refill_patch -> このモジュールに残す。
#   runner ラップ (top_n 拡張 + スコア clamp) -> このモジュールに残す。
#
# low move pool refill と runner ラップを本文へ畳まない理由:
#   - refill 対象の executor._filter_blocked_ai_ok_items は executor 側の
#     core filter watcher が 0.5 秒ごとに base へ戻すため、このパッチの
#     watcher (1 秒ごと) と奪い合っている。実測でも起動 4 秒後の状態は
#     run ごとに base / refill 版のどちらにもなる。畳むと refill が常時有効に
#     なってしまい、観測挙動が変わる。
#   - runner ラップは strict_entry_defaults / direct_input_context などと
#     同じ属性を奪い合っており、install 順そのものが観測挙動になっている。
#
# 旧 install 元は main.py の patches リスト (同じ位置を維持)。
# ============================================================

from __future__ import annotations

import functools
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "Ver1.8-SUMMARY-AI-APPROVED-ROW-RANGE-REPAIR"
_INSTALLED = False
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
        x = float(str(v).replace(",", ""))
        if x != x or x in (float("inf"), float("-inf")):
            return float(default)
        return float(x)
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


def _install_low_move_pool_refill(reason: str = "install") -> bool:
    """AI_OK が 1 件以下へ潰れたとき、スコア順のプールから補充する。

    ラップ対象は executor._filter_blocked_ai_ok_items。executor 側の
    core filter watcher が base へ戻すので、掛け直しは毎回試みる
    (旧実装も同じく先頭に早期 return を置いていない)。
    """
    global _LOW_MOVE_POOL_PATCHED
    if not _env_bool("SUMMARY_AI_LOW_MOVE_POOL_REFILL", True):
        return False
    try:
        import trading.entry.summary_ai.executor as ex

        cur = getattr(ex, "_filter_blocked_ai_ok_items", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_low_move_pool_refill_v18", False) or getattr(cur, "_summary_ai_low_move_pool_refill_v17", False):
            _LOW_MOVE_POOL_PATCHED = True
            return True

        original = getattr(cur, "_original", cur)

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
                        reason, len(raw_items), len(out_items), len(merged),
                        _safe_symbol_list(out_items, 20), _safe_symbol_list(merged, 20), VERSION,
                    )
                    return merged
            except Exception:
                logger.exception("[SUMMARY AI LOW MOVE POOL REFILL] failed; use original filtered output")
            return out

        _filter_blocked_ai_ok_items_pool_refill._summary_ai_low_move_pool_refill_v16 = True  # type: ignore[attr-defined]
        _filter_blocked_ai_ok_items_pool_refill._summary_ai_low_move_pool_refill_v17 = True  # type: ignore[attr-defined]
        _filter_blocked_ai_ok_items_pool_refill._summary_ai_low_move_pool_refill_v18 = True  # type: ignore[attr-defined]
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
            ok1 = _install_low_move_pool_refill(reason=f"watcher:{i}")
            if i in (0, loops - 1):
                logger.warning("[SUMMARY AI MORE CANDIDATES] enforce i=%s/%s low_move_pool=%s version=%s", i, loops, ok1, VERSION)
            time.sleep(sleep_sec)

    threading.Thread(target=_watch, name="summary-ai-low-move-pool-refill", daemon=True).start()
    logger.warning("[SUMMARY AI LOW MOVE POOL REFILL] watcher started version=%s", VERSION)


def _strict_score_floor() -> float:
    return max(0.01, _env_float("SUMMARY_AI_MIN_SCORE", _env_float("SUMMARY_ENTRY_MIN_SCORE", _env_float("MIN_ENTRY_SCORE", 3.0))))


def _clamp_summary_ai_scores(kwargs: dict[str, Any]) -> dict[str, tuple[float | None, float]]:
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

    try:
        import trading.handlers.entry_controller as ec

        fn = getattr(ec, "apply_final_gate_relax_constants", None)
        if callable(fn):
            fn()
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES] final gate relax constants failed")

    _install_low_move_pool_refill()
    _start_low_move_pool_watcher()

    if _INSTALLED:
        logger.warning("[SUMMARY AI MORE CANDIDATES] already installed low_move_pool_refill=%s", _LOW_MOVE_POOL_PATCHED)
        return True

    if not _env_bool("SUMMARY_AI_MORE_CANDIDATES_ENABLED", True):
        logger.warning("[SUMMARY AI MORE CANDIDATES] disabled by env")
        return False

    try:
        import trading.entry.summary_ai.candidates as candidates
        import trading.entry.summary_ai.runner as runner

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
            logger.error("[SUMMARY AI MORE CANDIDATES] runner.run_summary_ai_entry_from_df not callable")
            return False

        if getattr(original, "_summary_ai_more_candidates_v18", False):
            _INSTALLED = True
            return True

        @functools.wraps(original)
        def _wrapped_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
            _install_low_move_pool_refill(reason="before_run")
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
                "[SUMMARY AI MORE CANDIDATES] run source=%s interval=%s top_n=%s max_candidates=%s candidate_limit=%s tonosama_max=%s bypass_slope=%s explicit_top_n=%s score_changes=%s low_move_pool_refill=%s version=%s",
                kwargs.get("source", "SUMMARY"), kwargs.get("interval", 1), kwargs.get("top_n"),
                kwargs.get("max_candidates"), kwargs.get("candidate_limit"), kwargs.get("tonosama_max_candidates"),
                bypass_slope, explicit_top_n, score_changes, _LOW_MOVE_POOL_PATCHED, VERSION,
            )
            return original(*args, **kwargs)

        for _mark in ("v1", "v12", "v13", "v14", "v15", "v16", "v17", "v18"):
            setattr(_wrapped_run_summary_ai_entry_from_df, f"_summary_ai_more_candidates_{_mark}", True)
        _wrapped_run_summary_ai_entry_from_df._original = original  # type: ignore[attr-defined]
        runner.run_summary_ai_entry_from_df = _wrapped_run_summary_ai_entry_from_df

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI MORE CANDIDATES] installed top_n=%s tonosama_max=%s bypass_slope=%s strict_min=%.2f low_move_pool_refill=%s version=%s",
            top_n, tonosama_max, bypass_slope, _strict_score_floor(), _LOW_MOVE_POOL_PATCHED, VERSION,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES] install failed")
        return False


__all__ = ["install", "VERSION"]
