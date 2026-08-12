# -*- coding: utf-8 -*-
"""
RANKING pre-filter シグナル版の本体は
trading/entry/summary_ai/runner.py._apply_ranking_pre_filter へ本文化済み。

このモジュールに残っているのは、旧 V4 の install() が併せて行っていた
2件の連鎖install (ranking_summary_fast_entry / exit_stagnation) とその
リトライ watcher のみ。両者を本文化した時点でこのファイルごと削除する。
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time

logger = logging.getLogger(__name__)
VERSION = "V5-RANKING-PREFILTER-SIGNAL-CHAIN-ONLY"
_INSTALLED = False
_WATCHER_STARTED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _install_fast_ranking_summary() -> bool:
    try:
        from core.startup.ranking_summary_fast_entry_patch import install as _install_fast
        ok = bool(_install_fast())
        logger.warning("[SUMMARY AI RANKING PREFILTER SIGNAL] fast ranking summary patch ok=%s version=%s", ok, VERSION)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI RANKING PREFILTER SIGNAL] fast ranking summary patch failed version=%s", VERSION)
        return False


def _install_exit_stagnation() -> bool:
    try:
        from core.startup.exit_stagnation_favorable_then_market_patch import install as _install_exit_stagnation
        ok = bool(_install_exit_stagnation())
        logger.warning("[SUMMARY AI RANKING PREFILTER SIGNAL] exit stagnation patch ok=%s version=%s", ok, VERSION)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI RANKING PREFILTER SIGNAL] exit stagnation patch failed version=%s", VERSION)
        return False


def _watcher() -> None:
    loops = max(1, _env_int("SUMMARY_AI_RANKING_PREFILTER_WATCHER_LOOPS", 120))
    sleep_sec = max(0.5, _env_float("SUMMARY_AI_RANKING_PREFILTER_WATCHER_SLEEP", 1.0))
    for i in range(loops):
        try:
            _install_fast_ranking_summary()
            _install_exit_stagnation()
        except Exception:
            logger.debug("[SUMMARY AI RANKING PREFILTER SIGNAL] watcher loop failed", exc_info=True)
        time.sleep(sleep_sec)
    logger.warning("[SUMMARY AI RANKING PREFILTER SIGNAL] watcher done loops=%s version=%s", loops, VERSION)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    fast_ok = _install_fast_ranking_summary()
    exit_ok = _install_exit_stagnation()
    _INSTALLED = bool(fast_ok or exit_ok)
    if (fast_ok or exit_ok) and not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        try:
            threading.Thread(target=_watcher, name="summary-ai-ranking-prefilter-signal-watch", daemon=True).start()
            logger.warning("[SUMMARY AI RANKING PREFILTER SIGNAL] watcher started version=%s", VERSION)
        except Exception:
            logger.exception("[SUMMARY AI RANKING PREFILTER SIGNAL] watcher start failed version=%s", VERSION)
    return bool(fast_ok or exit_ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI RANKING PREFILTER SIGNAL] auto install failed")
