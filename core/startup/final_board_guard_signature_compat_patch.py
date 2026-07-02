# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/final_board_guard_signature_compat_patch.py
# Version: V3-MARK-NATIVE-GUARD-AND-SUMMARY-AI-REFILL
# ------------------------------------------------------------
# Purpose:
#   final_entry_safety_guard_patch Ver12 以降は _board_guard 自体が
#   4引数対応済みのため、ここで再wrapしない。
#
# V3:
#   - 古い V1 watcher が _board_guard を1秒ごとに再wrapする問題を停止。
#   - summary_ai_entry_hook_dataframe_truth_patch 側の旧compat watcherが
#     再wrapしないよう、現在の native guard に signature marker を付与。
#   - 既に旧compat wrapper が挟まっている場合は _original / compat_original を剥がして戻す。
#   - SUMMARY_AIの上位3件が blowoff で全落ちするケースに備え、候補選択プールだけ広げる。
#   - 実発注直前の df_exec は最大3件にcapし、同時発注数は増やしすぎない。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V3-MARK-NATIVE-GUARD-AND-SUMMARY-AI-REFILL"
_WATCHER_STARTED = False
_INSTALLED = False
_LAST_TARGET_ID: int | None = None
_SELECTION_PATCHED = False
_EXEC_CAP_PATCHED = False


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


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
    ok1 = _install_summary_ai_selection_pool_patch()
    ok2 = _install_entry_pipeline_exec_cap_patch()
    return bool(ok1 or ok2)


def _watcher() -> None:
    try:
        stable = 0
        last_id = None
        for i in range(30):
            time.sleep(1.0)
            ok = _apply(reason=f"watcher:{i + 1}")
            _install_candidate_refill_patches()
            cur_id = _LAST_TARGET_ID
            stable = stable + 1 if ok and cur_id == last_id and _SELECTION_PATCHED and _EXEC_CAP_PATCHED else 0
            last_id = cur_id
            if stable >= 3:
                logger.warning("[FINAL BOARD GUARD SIG COMPAT] watcher stable exit i=%s version=%s", i + 1, VERSION)
                return
        logger.warning("[FINAL BOARD GUARD SIG COMPAT] watcher done version=%s selection_patch=%s exec_cap=%s", VERSION, _SELECTION_PATCHED, _EXEC_CAP_PATCHED)
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
    logger.warning("[FINAL BOARD GUARD SIG COMPAT] installed=%s version=%s selection_patch=%s exec_cap=%s", _INSTALLED, VERSION, _SELECTION_PATCHED, _EXEC_CAP_PATCHED)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[FINAL BOARD GUARD SIG COMPAT] auto install failed")


__all__ = ["VERSION", "install"]
