# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/final_board_guard_signature_compat_patch.py
# Version: V4-SUMMARY-AI-CURRENT-DF-FRESHNESS
# ------------------------------------------------------------
# Purpose:
#   final_entry_safety_guard_patch Ver12 以降は _board_guard 自体が
#   4引数対応済みのため、ここで再wrapしない。
#
# V4:
#   - 古い V1 watcher が _board_guard を1秒ごとに再wrapする問題を停止。
#   - summary_ai_entry_hook_dataframe_truth_patch 側の旧compat watcherが
#     再wrapしないよう、現在の native guard に signature marker を付与。
#   - SUMMARY_AIの上位3件が blowoff で全落ちするケースに備え、候補選択プールだけ広げる。
#   - 実発注直前の df_exec は最大3件にcapし、同時発注数は増やしすぎない。
#   - SUMMARY_AI安全鮮度チェックが、現在AIへ渡された最新DFではなく古い
#     global_data.push_df / raw fallback を見て stale_push_1m になる問題を修正。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V4-SUMMARY-AI-CURRENT-DF-FRESHNESS"
_WATCHER_STARTED = False
_INSTALLED = False
_LAST_TARGET_ID: int | None = None
_SELECTION_PATCHED = False
_EXEC_CAP_PATCHED = False
_CURRENT_DF_PATCHED = False
_CURRENT_AI_INPUT = threading.local()


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
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


def _is_legacy_wrapper(fn: Any) -> bool:
    return bool(
        getattr(fn, "_final_board_guard_signature_compat_v1", False)
        or getattr(fn, "_final_entry_board_guard_compat", False)
        or getattr(fn, "_final_entry_board_guard_compat_v15", False)
        or getattr(fn, "_final_entry_board_guard_compat_v16", False)
    )


def _unwrap(fn: Any) -> Any:
    seen: set[int] = set()
    cur = fn
    while callable(cur) and id(cur) not in seen:
        seen.add(id(cur))
        nxt = (
            getattr(cur, "_final_entry_board_guard_compat_original", None)
            or getattr(cur, "_original", None)
            or getattr(cur, "_original_board_guard", None)
        )
        if callable(nxt) and nxt is not cur:
            cur = nxt
            continue
        break
    return cur


def _mark_signature_safe(fn: Any) -> Any:
    try:
        setattr(fn, "_final_board_guard_signature_v2", True)
        setattr(fn, "_final_board_guard_signature_runtime", True)
        setattr(fn, "_final_board_guard_signature_compat_v2", True)
    except Exception:
        pass
    return fn


def _apply(reason: str = "install") -> bool:
    global _LAST_TARGET_ID
    try:
        import core.startup.final_entry_safety_guard_patch as fsg

        cur = getattr(fsg, "_board_guard", None)
        if not callable(cur):
            logger.warning("[FINAL BOARD GUARD SIG COMPAT] target _board_guard missing reason=%s version=%s", reason, VERSION)
            return False

        base = _unwrap(cur)
        if not callable(base):
            base = cur

        _mark_signature_safe(base)
        try:
            fsg._board_guard = base
            fsg._patched_board_guard = base
        except Exception:
            pass

        cur_id = id(base)
        if _LAST_TARGET_ID != cur_id or _is_legacy_wrapper(cur):
            logger.warning(
                "[FINAL BOARD GUARD SIG COMPAT] native guard marked reason=%s unwrapped=%s cur=%s base=%s version=%s",
                reason,
                _is_legacy_wrapper(cur),
                getattr(cur, "__name__", type(cur).__name__),
                getattr(base, "__name__", type(base).__name__),
                VERSION,
            )
        _LAST_TARGET_ID = cur_id
        return True
    except Exception:
        logger.exception("[FINAL BOARD GUARD SIG COMPAT] apply failed reason=%s version=%s", reason, VERSION)
        return False


def _latest_age_sec(df: Any) -> tuple[bool, float | None, Any, int, int]:
    try:
        import pandas as pd
        if not isinstance(df, pd.DataFrame) or df.empty:
            return False, None, None, 0, 0
        dt_col = None
        for c in ("datetime", "end_time", "last_update", "updated_at", "inserted_at"):
            if c in df.columns:
                dt_col = c
                break
        if not dt_col:
            return False, None, None, len(df), 0
        latest = pd.to_datetime(df[dt_col], errors="coerce").max()
        if pd.isna(latest):
            return False, None, None, len(df), 0
        try:
            latest_py = latest.to_pydatetime().replace(tzinfo=None)
        except Exception:
            latest_py = latest
        age = (dt.datetime.now().replace(tzinfo=None) - latest_py).total_seconds()
        score_nonzero = 0
        for c in ("score", "score_total", "final_score", "display_score", "score_buy", "score_sell", "buy_score", "sell_score"):
            if c in df.columns:
                try:
                    score_nonzero = max(score_nonzero, int((pd.to_numeric(df[c], errors="coerce").fillna(0).abs() > 0).sum()))
                except Exception:
                    pass
        return True, float(age), latest_py, len(df), score_nonzero
    except Exception:
        return False, None, None, 0, 0


def _install_current_df_freshness_patch() -> bool:
    """Let SUMMARY_AI safety guard trust the current scored df passed into the runner."""
    global _CURRENT_DF_PATCHED
    if _CURRENT_DF_PATCHED:
        return True
    try:
        import trading.entry.summary_ai.runner as runner
        import core.startup.summary_ai_candidate_refill_patch as guard

        cur_run = getattr(runner, "run_summary_ai_entry_from_df", None)
        cur_reason = getattr(guard, "_summary_ai_entry_unsafe_reason", None)
        if not callable(cur_run) or not callable(cur_reason):
            return False
        if getattr(cur_run, "_summary_ai_current_df_freshness_v1", False) and getattr(cur_reason, "_summary_ai_current_df_freshness_v1", False):
            _CURRENT_DF_PATCHED = True
            return True

        orig_run = cur_run
        orig_reason = cur_reason

        def _extract_df(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            if args:
                return args[0]
            for key in ("summary_df", "df", "source_df", "base_df"):
                if key in kwargs:
                    return kwargs.get(key)
            return None

        def _patched_unsafe_reason():
            df = getattr(_CURRENT_AI_INPUT, "df", None)
            ok, age, latest, rows, score_nonzero = _latest_age_sec(df)
            max_age = _env_float("SUMMARY_AI_MAX_PUSH_1M_AGE_SEC", 120.0)
            # 現在AIへ渡されたDFが新鮮で、かつスコア付きなら、このDFを優先する。
            if ok and age is not None and age <= max_age and rows > 0 and score_nonzero > 0:
                logger.warning(
                    "[SUMMARY AI SAFETY GUARD] current AI df freshness OK rows=%s latest=%s age=%.1f max=%.1f score_nonzero=%s version=%s",
                    rows,
                    latest,
                    age,
                    max_age,
                    score_nonzero,
                    VERSION,
                )
                return None
            return orig_reason()

        def _patched_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
            old = getattr(_CURRENT_AI_INPUT, "df", None)
            _CURRENT_AI_INPUT.df = _extract_df(args, kwargs)
            try:
                return orig_run(*args, **kwargs)
            finally:
                _CURRENT_AI_INPUT.df = old

        _patched_unsafe_reason._summary_ai_current_df_freshness_v1 = True  # type: ignore[attr-defined]
        _patched_unsafe_reason._original = orig_reason  # type: ignore[attr-defined]
        _patched_run_summary_ai_entry_from_df._summary_ai_current_df_freshness_v1 = True  # type: ignore[attr-defined]
        _patched_run_summary_ai_entry_from_df._original = orig_run  # type: ignore[attr-defined]
        guard._summary_ai_entry_unsafe_reason = _patched_unsafe_reason
        runner.run_summary_ai_entry_from_df = _patched_run_summary_ai_entry_from_df
        _CURRENT_DF_PATCHED = True
        logger.warning("[SUMMARY AI SAFETY GUARD] current df freshness patch installed version=%s", VERSION)
        return True
    except Exception:
        logger.debug("[SUMMARY AI SAFETY GUARD] current df freshness patch not ready", exc_info=True)
        return False


def _install_summary_ai_selection_pool_patch() -> bool:
    """Keep hard order cap, but let filters choose from more than the top 3."""
    global _SELECTION_PATCHED
    if _SELECTION_PATCHED:
        return True
    try:
        import trading.entry.summary_ai.executor as ex

        cur = getattr(ex, "_select_ai_ok_items", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_selection_pool_v1", False):
            _SELECTION_PATCHED = True
            return True

        def _select_ai_ok_items_pool(ok_items, *, max_entries: int):
            if not ok_items:
                return []
            kept = ex._filter_blocked_ai_ok_items(ok_items)
            hard_cap = int(ex._effective_max_entries(max_entries))
            pool_n = max(hard_cap, _env_int("SUMMARY_AI_EXECUTOR_SELECTION_POOL", 20))
            pool_n = min(max(1, pool_n), max(1, len(kept)))
            selected = sorted(kept, key=ex._sort_key, reverse=True)[:pool_n]
            logger.warning(
                "[SUMMARY AI EXECUTOR] selection pool requested=%s hard_cap=%s pool=%s ok_total=%s selected_head=%s version=%s",
                max_entries,
                hard_cap,
                pool_n,
                len(kept),
                [
                    {
                        "symbol": ex._pick_symbol(x),
                        "side": ex._pick_side(x),
                        "price": ex._pick_price(x),
                        "score": round(ex._score_for_side(x), 3),
                    }
                    for x in selected[:10]
                ],
                VERSION,
            )
            return selected

        _select_ai_ok_items_pool._summary_ai_selection_pool_v1 = True  # type: ignore[attr-defined]
        _select_ai_ok_items_pool._original = cur  # type: ignore[attr-defined]
        ex._select_ai_ok_items = _select_ai_ok_items_pool
        _SELECTION_PATCHED = True
        logger.warning("[SUMMARY AI EXECUTOR] selection pool patch installed pool=%s version=%s", _env_int("SUMMARY_AI_EXECUTOR_SELECTION_POOL", 20), VERSION)
        return True
    except Exception:
        logger.debug("[SUMMARY AI EXECUTOR] selection pool patch not ready", exc_info=True)
        return False


def _install_entry_pipeline_exec_cap_patch() -> bool:
    """Cap actual df_exec rows after all filters. This keeps simultaneous orders <= 3."""
    global _EXEC_CAP_PATCHED
    if _EXEC_CAP_PATCHED:
        return True
    try:
        import pandas as pd
        import trading.summary.pipeline.entry_pipeline as ep

        cur = getattr(ep, "_build_exec_dataframe", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_exec_cap_v1", False):
            _EXEC_CAP_PATCHED = True
            return True

        def _build_exec_dataframe_capped(rows, interval):
            df = cur(rows, interval)
            try:
                if not isinstance(df, pd.DataFrame) or df.empty:
                    return df
                cap = max(1, _env_int("SUMMARY_AI_MAX_REAL_ENTRIES", _env_int("ENTRY_MAX_CONCURRENT_ORDERS", 3)))
                source_s = "|".join(df.get("source", pd.Series([], dtype=str)).astype(str).str.upper().head(20).tolist()) if "source" in df.columns else ""
                entry_s = "|".join(df.get("entry_type", pd.Series([], dtype=str)).astype(str).str.upper().head(20).tolist()) if "entry_type" in df.columns else ""
                if ("SUMMARY" in source_s or "PUSH" in source_s or "SUMMARY_AI" in entry_s) and len(df) > cap:
                    before = len(df)
                    df = df.head(cap).copy()
                    logger.warning("[entry_pipeline] capped SUMMARY_AI executable rows before=%s after=%s cap=%s version=%s", before, len(df), cap, VERSION)
            except Exception:
                logger.exception("[entry_pipeline] executable cap failed; use uncapped df")
            return df

        _build_exec_dataframe_capped._summary_ai_exec_cap_v1 = True  # type: ignore[attr-defined]
        _build_exec_dataframe_capped._original = cur  # type: ignore[attr-defined]
        ep._build_exec_dataframe = _build_exec_dataframe_capped
        _EXEC_CAP_PATCHED = True
        logger.warning("[entry_pipeline] SUMMARY_AI executable cap patch installed cap=%s version=%s", _env_int("SUMMARY_AI_MAX_REAL_ENTRIES", 3), VERSION)
        return True
    except Exception:
        logger.debug("[entry_pipeline] executable cap patch not ready", exc_info=True)
        return False


def _install_candidate_refill_patches() -> bool:
    ok0 = _install_current_df_freshness_patch()
    ok1 = _install_summary_ai_selection_pool_patch()
    ok2 = _install_entry_pipeline_exec_cap_patch()
    return bool(ok0 or ok1 or ok2)


def _watcher() -> None:
    try:
        stable = 0
        last_id = None
        for i in range(30):
            time.sleep(1.0)
            ok = _apply(reason=f"watcher:{i + 1}")
            _install_candidate_refill_patches()
            cur_id = _LAST_TARGET_ID
            stable = stable + 1 if ok and cur_id == last_id and _SELECTION_PATCHED and _EXEC_CAP_PATCHED and _CURRENT_DF_PATCHED else 0
            last_id = cur_id
            if stable >= 3:
                logger.warning("[FINAL BOARD GUARD SIG COMPAT] watcher stable exit i=%s version=%s", i + 1, VERSION)
                return
        logger.warning("[FINAL BOARD GUARD SIG COMPAT] watcher done version=%s selection_patch=%s exec_cap=%s current_df=%s", VERSION, _SELECTION_PATCHED, _EXEC_CAP_PATCHED, _CURRENT_DF_PATCHED)
    except Exception:
        logger.exception("[FINAL BOARD GUARD SIG COMPAT] watcher failed version=%s", VERSION)


def _start_watcher() -> None:
    global _WATCHER_STARTED
    if _WATCHER_STARTED:
        return
    _WATCHER_STARTED = True
    threading.Thread(target=_watcher, name="final-board-guard-signature-compat", daemon=True).start()
    logger.warning("[FINAL BOARD GUARD SIG COMPAT] watcher started version=%s", VERSION)


def install() -> bool:
    global _INSTALLED
    ok = _apply("install")
    _install_candidate_refill_patches()
    _start_watcher()
    _INSTALLED = bool(ok)
    logger.warning("[FINAL BOARD GUARD SIG COMPAT] installed=%s version=%s selection_patch=%s exec_cap=%s current_df=%s", _INSTALLED, VERSION, _SELECTION_PATCHED, _EXEC_CAP_PATCHED, _CURRENT_DF_PATCHED)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[FINAL BOARD GUARD SIG COMPAT] auto install failed")


__all__ = ["VERSION", "install"]
