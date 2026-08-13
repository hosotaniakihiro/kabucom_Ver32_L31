# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_parallel_main_scope.py
# Version: V2-MAIN-SUMMARY-SCHEDULER-1M-ONLY
# ------------------------------------------------------------
# 旧 core/startup/summary_parallel_timeout_relief_patch.py の環境変数部分を
# ここへ移設した (パッチではなく通常の設定モジュール)。
#
# 目的:
#   main.py の entry tick を軽く保つ。3m/5m の重い PUSH 処理は
#   main_database.py / raw fallback / cache / MTF enrichment 側に任せ、
#   main.py では 1m PUSH のみを同期実行する。
#
# 旧パッチのうち scheduler の 1m-only 判定は
# scheduler_jobs/summary/scheduler.py の _should_run_3m / _should_run_5m へ
# 本文化した (_main_1m_only_scope)。ここは env の強制だけを担う。
#
# _parent_timeout_sec / _child_timeout_sec は旧パッチが差し替えていたが、
# 下で SUMMARY_PARENT_TICK_TIMEOUT_SEC=30 / SUMMARY_CHILD_JOB_TIMEOUT_SEC=20 を
# 強制するため、scheduler.py 本体の _env_float 実装と結果が一致する。
# よって差し替えは不要になった。
#
# 適用スコープは旧パッチと同じく main.py プロセスのみ (main_database.py は除外)。
# ============================================================
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)
VERSION = "V2-MAIN-SUMMARY-SCHEDULER-1M-ONLY"
_INSTALLED = False

# main.py で強制する値 (旧パッチ install() の第1グループ)。
_FORCED_ENV = {
    "SUMMARY_PUSH_BG_ALL_INTERVALS": "0",
    "SUMMARY_PUSH_BG_LONG_INTERVALS": "0",
    "SUMMARY_PUSH_DISPLAY_ALL_INTERVALS": "0",
    "SUMMARY_PUSH_BG_INTERVAL_WORKERS": "1",
    "SUMMARY_PUSH_FORCE_1_3_5": "0",
    "SUMMARY_PARALLEL_FORCE_1_3_5": "0",
    "SUMMARY_PARALLEL_MAIN_ENTRY_ONLY": "1",
    "SUMMARY_PARALLEL_TIMEOUT_SEC": "20",
    "SUMMARY_PARALLEL_MIN_TIMEOUT_SEC": "20",
    "SUMMARY_PARALLEL_PARENT_TIMEOUT_SEC": "30",
    "SUMMARY_PARENT_TICK_TIMEOUT_SEC": "30",
    "SUMMARY_CHILD_JOB_TIMEOUT_SEC": "20",
    "SUMMARY_RUN_ENTRY_ON_1M_ONLY": "1",
    "SUMMARY_RANKING_PARALLEL_ENABLED": "0",
    "SUMMARY_MAIN_PUSH_1M_ONLY": "1",
}

# cache / raw fallback で MTF を賄うための既定値 (未設定のときだけ)。
_DEFAULT_ENV = {
    "SUMMARY_MTF_PUSH_RAW_FALLBACK_ENABLED": "1",
    "SUMMARY_MTF_DIFF_FROM_1M_ENABLED": "1",
    "SUMMARY_MTF_DIFF_ALLOW_PARTIAL_BAR": "0",
    "SUMMARY_MTF_DIFF_HISTORY_ROWS": "74",
    "SUMMARY_LATEST_PREFER_HEALTH": "1",
}

# scheduler_jobs/summary/parallel_intervals.py 側で上書きする timeout 定数。
_MODULE_TIMEOUT_ATTRS = (
    "SUMMARY_PARALLEL_TIMEOUT_SEC",
    "SUMMARY_PARALLEL_MIN_TIMEOUT_SEC",
    "SUMMARY_CHILD_JOB_TIMEOUT_SEC",
    "SUMMARY_PARENT_TICK_TIMEOUT_SEC",
    "DEFAULT_TIMEOUT_SEC",
    "TIMEOUT_SEC",
    "MIN_TIMEOUT_SEC",
)

# _patch_summary_parallel_module() が強制していた env (install() の第1グループとほぼ同じ。
# SUMMARY_RUN_ENTRY_ON_1M_ONLY / SUMMARY_RANKING_PARALLEL_ENABLED は含まれない)。
_MODULE_FORCED_ENV = {
    k: v
    for k, v in _FORCED_ENV.items()
    if k not in ("SUMMARY_RUN_ENTRY_ON_1M_ONLY", "SUMMARY_RANKING_PARALLEL_ENABLED")
}


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv and "main_database.py" not in argv
    except Exception:
        return False


def _set_default(name: str, value: str) -> bool:
    try:
        old = os.getenv(name)
        if old is None or str(old).strip() == "":
            os.environ[name] = str(value)
            return True
        return False
    except Exception:
        return False


def _force(name: str, value: str) -> tuple[str | None, str]:
    old = os.getenv(name)
    os.environ[name] = str(value)
    return old, str(value)


def _tune_summary_parallel_module() -> bool:
    """scheduler_jobs/summary/parallel_intervals.py の timeout 定数も揃える。"""
    try:
        import scheduler_jobs.summary.parallel_intervals as target
    except Exception:
        return False

    patched = False
    for name, value in _MODULE_FORCED_ENV.items():
        _force(name, value)
        patched = True

    for attr in _MODULE_TIMEOUT_ATTRS:
        try:
            if hasattr(target, attr):
                setattr(target, attr, 30.0 if "PARENT" in attr else 20.0)
                patched = True
        except Exception:
            pass
    return patched


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _is_main_py():
        logger.warning("[SUMMARY PARALLEL TIMEOUT RELIEF] skipped non-main context version=%s", VERSION)
        return False

    try:
        changed: dict[str, tuple[str | None, str]] = {}
        for name, value in _FORCED_ENV.items():
            old, new = _force(name, value)
            if old != new:
                changed[name] = (old, new)
        for name, value in _DEFAULT_ENV.items():
            if _set_default(name, value):
                changed[name] = (None, value)

        module_patched = _tune_summary_parallel_module()
        _INSTALLED = True
        logger.warning(
            "[SUMMARY PARALLEL TIMEOUT RELIEF] installed version=%s main_py=True module_patched=%s changed=%s",
            VERSION,
            module_patched,
            {k: v[1] for k, v in changed.items()},
        )
        return True
    except Exception:
        logger.exception("[SUMMARY PARALLEL TIMEOUT RELIEF] install failed")
        return False


__all__ = ["VERSION", "install"]
