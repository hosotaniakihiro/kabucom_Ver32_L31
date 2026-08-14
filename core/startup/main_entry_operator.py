# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/main_entry_operator.py
# Version: V2-MAIN-ENTRY-FRESH-RAW-SUMMARYAI
# ------------------------------------------------------------
# 旧 core/startup/main_entry_runtime_operator_fix_patch.py。
#
# main.py は判断/発注プロセスで、PUSH DB の保存は main_database.py が持つ。
# main.py 側を軽く保ったまま、in-process の merged summary が古いだけで
# Summary-AI / Tonosama が止まらないようにする運用ポリシー。
#
# 旧 install() の内訳と行方:
#   _set_env_defaults              -> このファイル (env の強制代入)
#   _patch_summary_ai_hook_once    -> このファイル
#       (hook.run_summary_ai_entry_safe は direct_input_context の watcher と
#        奪い合っており、install 順そのものが観測挙動なのでラッパーのまま)
#   _patch_tonosama_memory_history_once
#       -> trading/entry/tonosama/history_missing_guard.
#          _load_push_raw_db_1m_history() 本体へ本文化
#          (包む相手が他に無いので畳める。ネイティブは *_core へ改名)
#   データ取得ヘルパ (メモリ PUSH / PUSH raw DB / overlay / publish)
#       -> trading/summary/push_raw_1m_source.py
#   _patch_summary_parallel_attrs  -> **削除**。
#       scheduler_jobs/summary/parallel_intervals.py は該当名のモジュール属性を
#       持たず (env でしか読まない)、hasattr ガードで毎回素通りしていた。
#       実測でも 4 属性はすべて存在しない。
#
# 適用スコープ: install 元は usercustomize.py の
# _install_summary_mtf_duplicate_datetime_guard() だけ (main.py プロセス限定)。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V2-MAIN-ENTRY-FRESH-RAW-SUMMARYAI"
_INSTALLED = False
_WATCHER_STARTED = False
_PATCHED_SUMMARY_AI_HOOK = False
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_on(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return int(default)


def _force_env(name: str, value: str, changed: dict[str, tuple[str | None, str]] | None = None) -> None:
    try:
        old = os.getenv(name)
        os.environ[name] = str(value)
        if changed is not None and str(old or "") != str(value):
            changed[name] = (old, str(value))
    except Exception:
        pass


def _set_env_defaults() -> None:
    changed: dict[str, tuple[str | None, str]] = {}

    # 2) main.py should not wait for 3m/5m PUSH or ranking summary jobs.
    for name, value in {
        "SUMMARY_MAIN_WAIT_PUSH_1M_ONLY": "1",
        "SUMMARY_MAIN_BG_LONG_PUSH_ENABLED": "0",
        "SUMMARY_PUSH_BG_ALL_INTERVALS": "0",
        "SUMMARY_PUSH_BG_LONG_INTERVALS": "0",
        "SUMMARY_PUSH_DISPLAY_ALL_INTERVALS": "0",
        "SUMMARY_PUSH_FORCE_1_3_5": "0",
        "SUMMARY_PARALLEL_FORCE_1_3_5": "0",
        "SUMMARY_PARALLEL_MAIN_ENTRY_ONLY": "1",
        "SUMMARY_RUN_ENTRY_ON_1M_ONLY": "1",
        "SUMMARY_PARALLEL_RANKING_ENABLED": "0",
        "SUMMARY_RANKING_PARALLEL_ENABLED": "0",
        "SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC": "18",
        "SUMMARY_PARALLEL_TIMEOUT_MIN_SEC": "18",
        "SUMMARY_PARALLEL_TIMEOUT_SEC": "18",
        "SUMMARY_PARALLEL_MIN_TIMEOUT_SEC": "18",
        "SUMMARY_CHILD_JOB_TIMEOUT_SEC": "18",
        "SUMMARY_PARENT_TICK_TIMEOUT_SEC": "25",
        "SUMMARY_PARALLEL_PARENT_TIMEOUT_SEC": "25",
        "SUMMARY_MAIN_TICK_TIMEOUT_CAP_SEC": "18",
        "SUMMARY_MTF_PUSH_RAW_FALLBACK_ENABLED": "1",
        "SUMMARY_MTF_DIFF_FROM_1M_ENABLED": "1",
        "SUMMARY_LATEST_PREFER_HEALTH": "1",
    }.items():
        _force_env(name, value, changed)

    # PUSH DB writer復旧は今回対象外。writer_ready=False / memory_only=True を
    # 理由に SUMMARY_AI を止めず、fresh raw DB overlayで候補行の時刻だけ古い問題を救済する。
    for name, value in {
        "SUMMARY_AI_REQUIRE_PUSH_WRITER_READY": "0",
        "SUMMARY_AI_REQUIRE_FRESH_PUSH_1M": "0",
        "SUMMARY_AI_WRITER_CHECK_ALLOW_FRESH_RAW_DB": "1",
        "SUMMARY_AI_SCORE_BRIDGE_ENABLED": "1",
        "SUMMARY_AI_REFILL_RETRY_WITHOUT_TONOSAMA": "1",
        "SUMMARY_AI_REFILL_TOP_N": "80",
        "SUMMARY_AI_REFILL_RETRY_TOP_N": "100",
        "SUMMARY_AI_REFILL_TONOSAMA_MAX_CANDIDATES": "80",
        "SUMMARY_AI_REFILL_RETRY_TONOSAMA_MAX_CANDIDATES": "100",
        "SUMMARY_AI_FRESH_RAW_OVERLAY_ENABLED": "1",
        "SUMMARY_AI_FRESH_RAW_OVERLAY_MAX_AGE_SEC": "180",
        "SUMMARY_AI_FRESH_RAW_OVERLAY_STALE_SEC": "180",
        "SUMMARY_AI_ENTRY_TOP_N": "80",
        "SUMMARY_AI_ENTRY_TONOSAMA_MAX_CANDIDATES": "80",
    }.items():
        _force_env(name, value, changed)

    # Tonosama no-ratio recovery should be able to use memory PUSH too.
    for name, value in {
        "TONOSAMA_RAW1_HISTORY_RESAMPLE": "1",
        "TONOSAMA_RAW1_RESAMPLE_FALLBACK": "1",
        "TONOSAMA_PUSH_MEMORY_HISTORY_ENABLED": "1",
        "TONOSAMA_PUSH_MEMORY_HISTORY_LOOKBACK_MIN": "30",
        "TONOSAMA_PUSH_MEMORY_HISTORY_MAX_ROWS": "20000",
        "TONOSAMA_PUSH_MEMORY_HISTORY_MIN_VOLUME_NONZERO": "1",
        "TONOSAMA_SURGE_RATIO_MIN_PERIODS": "1",
    }.items():
        _force_env(name, value, changed)

    if changed:
        logger.warning("[MAIN ENTRY OPERATOR FIX] env applied version=%s changed=%s", VERSION, {k: v[1] for k, v in changed.items()})

def _patch_summary_ai_hook_once() -> bool:
    global _PATCHED_SUMMARY_AI_HOOK
    if _PATCHED_SUMMARY_AI_HOOK:
        return True
    try:
        import scheduler_jobs.summary.summary_ai_entry_hook_v20 as hook
        from trading.summary.push_raw_1m_source import overlay_summary_ai_df_with_fresh_raw

        cur = getattr(hook, "run_summary_ai_entry_safe", None)
        if not callable(cur):
            return False
        if getattr(cur, "_fresh_raw_overlay_v2", False):
            _PATCHED_SUMMARY_AI_HOOK = True
            return True
        orig = getattr(cur, "_original", cur)

        def patched_run_summary_ai_entry_safe(interval: int, now: dt.datetime, df=None, *args, source: str = "SUMMARY", **kwargs):
            new_df = overlay_summary_ai_df_with_fresh_raw(df, interval=int(interval), now=now)
            return orig(interval, now, new_df, *args, source=source, **kwargs)

        patched_run_summary_ai_entry_safe._fresh_raw_overlay_v2 = True  # type: ignore[attr-defined]
        patched_run_summary_ai_entry_safe._original = orig  # type: ignore[attr-defined]
        hook.run_summary_ai_entry_safe = patched_run_summary_ai_entry_safe
        _PATCHED_SUMMARY_AI_HOOK = True
        logger.warning("[SUMMARY AI FRESH RAW OVERLAY] hook patched version=%s", VERSION)
        return True
    except Exception:
        logger.debug("[SUMMARY AI FRESH RAW OVERLAY] hook patch wait/failed", exc_info=True)
        return False


def _watcher() -> None:
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        try:
            _set_env_defaults()
            _patch_summary_ai_hook_once()
            if _PATCHED_SUMMARY_AI_HOOK:
                return
        except Exception:
            logger.debug("[MAIN ENTRY OPERATOR FIX] watcher iteration failed", exc_info=True)
        time.sleep(0.75)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if _INSTALLED:
        return True
    try:
        _set_env_defaults()
        _patch_summary_ai_hook_once()
        if not _WATCHER_STARTED:
            _WATCHER_STARTED = True
            threading.Thread(target=_watcher, name="main-entry-operator-fix", daemon=True).start()
        _INSTALLED = True
        logger.warning(
            "[MAIN ENTRY OPERATOR FIX] installed version=%s watcher=%s summary_ai_overlay=%s",
            VERSION, _WATCHER_STARTED, _PATCHED_SUMMARY_AI_HOOK,
        )
        return True
    except Exception:
        logger.exception("[MAIN ENTRY OPERATOR FIX] install failed")
        return False


__all__ = ["VERSION", "install"]
