# ============================================================
# File   : scripts/night_yahoo_daily_timeout_patch.py
# Version: V1-NIGHT-YAHOO-DAILY-SYMBOL-TIMEOUT
# ------------------------------------------------------------
# 夜間Yahoo日足更新で、1銘柄の取得/計算/保存が詰まった場合に
# 指定秒数でスキップし、次の銘柄へ進めるためのパッチ。
#
# 注意:
#   Python のスレッドは強制終了できないため、詰まった処理はdaemon thread
#   として残る可能性がある。ただしバッチ本体は次銘柄へ進める。
# ============================================================

from __future__ import annotations

import concurrent.futures
import logging
import os
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("night_yahoo_daily_timeout_patch")
VERSION = "V1-NIGHT-YAHOO-DAILY-SYMBOL-TIMEOUT"
_INSTALLED = False
_EXECUTOR: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _timeout_sec() -> float:
    try:
        return float(os.environ.get("NIGHT_YAHOO_DAILY_SYMBOL_TIMEOUT_SEC", "90"))
    except Exception:
        return 90.0


def install(daily_mod: Any) -> bool:
    global _INSTALLED, _EXECUTOR
    if _INSTALLED:
        return True
    if daily_mod is None:
        return False
    if getattr(daily_mod, "_NIGHT_YAHOO_DAILY_TIMEOUT_PATCHED", False):
        _INSTALLED = True
        return True

    original_process = getattr(daily_mod, "process_symbol", None)
    if not callable(original_process):
        LOG.warning("[NIGHT YAHOO DAILY TIMEOUT] install failed: process_symbol missing")
        return False

    workers = int(float(os.environ.get("NIGHT_YAHOO_DAILY_TIMEOUT_WORKERS", "4")))
    workers = max(1, min(workers, 16))
    _EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="night-yahoo-daily")

    def process_symbol_timeout(rec: dict[str, str], *, period: str, start: Optional[str], db_path: Path):
        symbol = str((rec or {}).get("symbol", ""))
        timeout = _timeout_sec()
        fut = _EXECUTOR.submit(original_process, rec, period=period, start=start, db_path=db_path)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            try:
                fut.cancel()
            except Exception:
                pass
            LOG.warning(
                "[NIGHT YAHOO DAILY TIMEOUT] symbol timeout symbol=%s timeout=%.1fs -> skip",
                symbol,
                timeout,
            )
            return symbol, False, f"symbol timeout {timeout:.1f}s", 0

    daily_mod.process_symbol = process_symbol_timeout
    daily_mod._NIGHT_YAHOO_DAILY_TIMEOUT_PATCHED = True
    _INSTALLED = True
    LOG.warning(
        "[NIGHT YAHOO DAILY TIMEOUT] installed version=%s symbol_timeout=%ss workers=%s",
        VERSION,
        os.environ.get("NIGHT_YAHOO_DAILY_SYMBOL_TIMEOUT_SEC", "90"),
        workers,
    )
    return True
