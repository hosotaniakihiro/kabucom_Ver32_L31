# ============================================================
# File   : scripts/night_yahoo_daily_stage_log_patch.py
# Version: V1-NIGHT-YAHOO-DAILY-STAGE-LOG
# ------------------------------------------------------------
# 夜間Yahoo日足バッチで、銘柄ごとの fetch / compute / save の
# どこで待っているか分かるようにするログ補強パッチ。
# ============================================================

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

import pandas as pd

LOG = logging.getLogger("night_yahoo_daily_stage_log_patch")
VERSION = "V1-NIGHT-YAHOO-DAILY-STAGE-LOG"
_INSTALLED = False


def _sym_from_price_df(df: pd.DataFrame) -> str:
    try:
        if df is not None and not df.empty and "symbol" in df.columns:
            return str(df["symbol"].dropna().iloc[0])
    except Exception:
        pass
    return "?"


def install(daily_mod: Any) -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if daily_mod is None:
        return False
    if getattr(daily_mod, "_NIGHT_YAHOO_DAILY_STAGE_LOG_PATCHED", False):
        _INSTALLED = True
        return True

    original_fetch = getattr(daily_mod, "_fetch_daily_one", None)
    original_pipeline = getattr(daily_mod, "_run_indicator_pipeline", None)
    original_save = getattr(daily_mod, "_save_symbol_df", None)
    if not callable(original_fetch) or not callable(original_pipeline) or not callable(original_save):
        LOG.warning("[NIGHT YAHOO DAILY STAGE] install failed: required functions missing")
        return False

    def fetch_logged(symbol: str, *, period: str, start: Optional[str]):
        t0 = time.time()
        LOG.info("[NIGHT YAHOO DAILY STAGE] fetch begin symbol=%s start=%s period=%s", symbol, start or "-", period)
        out = original_fetch(symbol, period=period, start=start)
        LOG.info("[NIGHT YAHOO DAILY STAGE] fetch done symbol=%s rows=%s elapsed=%.1fs", symbol, 0 if out is None else len(out), time.time() - t0)
        return out

    def pipeline_logged(price_df: pd.DataFrame, rec: dict[str, str]):
        symbol = str((rec or {}).get("symbol") or _sym_from_price_df(price_df))
        t0 = time.time()
        LOG.info("[NIGHT YAHOO DAILY STAGE] compute begin symbol=%s rows=%s", symbol, 0 if price_df is None else len(price_df))
        out = original_pipeline(price_df, rec)
        LOG.info("[NIGHT YAHOO DAILY STAGE] compute done symbol=%s rows=%s cols=%s elapsed=%.1fs", symbol, 0 if out is None else len(out), 0 if out is None else len(out.columns), time.time() - t0)
        return out

    def save_logged(db_path: Path, df: pd.DataFrame):
        symbol = _sym_from_price_df(df)
        try:
            if df is not None and not df.empty and "stock_code" in df.columns:
                symbol = str(df["stock_code"].dropna().iloc[0])
        except Exception:
            pass
        t0 = time.time()
        LOG.info("[NIGHT YAHOO DAILY STAGE] save begin symbol=%s rows=%s cols=%s db=%s", symbol, 0 if df is None else len(df), 0 if df is None else len(df.columns), db_path)
        out = original_save(db_path, df)
        LOG.info("[NIGHT YAHOO DAILY STAGE] save done symbol=%s result=%s elapsed=%.1fs", symbol, out, time.time() - t0)
        return out

    daily_mod._fetch_daily_one = fetch_logged
    daily_mod._run_indicator_pipeline = pipeline_logged
    daily_mod._save_symbol_df = save_logged
    daily_mod._NIGHT_YAHOO_DAILY_STAGE_LOG_PATCHED = True
    _INSTALLED = True
    LOG.warning("[NIGHT YAHOO DAILY STAGE] installed version=%s", VERSION)
    return True
