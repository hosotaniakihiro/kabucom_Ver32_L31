# ============================================================
# File   : scripts/night_yahoo_daily_save_chunk_patch.py
# Version: V1-NIGHT-YAHOO-DAILY-CHUNKED-SQLITE-SAVE
# ------------------------------------------------------------
# NAS上のSQLiteへ 1銘柄数千行 x 多列 を一括upsertすると詰まるため、
# history保存をchunk分割し、chunkごとにcommitする。
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pandas as pd

LOG = logging.getLogger("night_yahoo_daily_save_chunk_patch")
VERSION = "V1-NIGHT-YAHOO-DAILY-CHUNKED-SQLITE-SAVE"
_INSTALLED = False


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return float(default)


def install(daily_mod: Any) -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if daily_mod is None:
        return False
    if getattr(daily_mod, "_NIGHT_YAHOO_DAILY_CHUNK_SAVE_PATCHED", False):
        _INSTALLED = True
        return True

    upsert_df = getattr(daily_mod, "_upsert_df", None)
    if not callable(upsert_df):
        LOG.warning("[NIGHT YAHOO DAILY SAVE CHUNK] install failed: _upsert_df missing")
        return False

    history_table = getattr(daily_mod, "DB_TABLE_HISTORY", "stock_analysis_history")
    latest_table = getattr(daily_mod, "DB_TABLE_LATEST", "stock_analysis_latest")

    def save_symbol_df_chunked(db_path: Path, df: pd.DataFrame):
        if df is None or df.empty:
            return 0, 0
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_size = max(50, _env_int("NIGHT_YAHOO_DAILY_SAVE_CHUNK_SIZE", 500))
        timeout = _env_float("NIGHT_YAHOO_DAILY_SQLITE_TIMEOUT", 10.0)
        busy_ms = max(1000, _env_int("NIGHT_YAHOO_DAILY_SQLITE_BUSY_TIMEOUT_MS", 10000))
        t0 = time.time()
        LOG.info(
            "[NIGHT YAHOO DAILY SAVE CHUNK] start rows=%s cols=%s chunk=%s db=%s",
            len(df), len(df.columns), chunk_size, db_path,
        )
        conn = sqlite3.connect(str(db_path), timeout=timeout)
        hist_total = 0
        lat = 0
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            conn.execute(f"PRAGMA busy_timeout={busy_ms}")
            conn.execute("PRAGMA synchronous=NORMAL")

            n = len(df)
            for start in range(0, n, chunk_size):
                part = df.iloc[start:start + chunk_size].copy()
                hist_total += int(upsert_df(conn, history_table, part, ["stock_code", "date"]) or 0)
                conn.commit()
                LOG.info(
                    "[NIGHT YAHOO DAILY SAVE CHUNK] history chunk %s-%s/%s committed elapsed=%.1fs",
                    start + 1, min(start + chunk_size, n), n, time.time() - t0,
                )

            latest = df.copy()
            latest["date"] = pd.to_datetime(latest["date"], errors="coerce")
            latest = latest.dropna(subset=["date"]).sort_values("date").tail(1)
            latest["date"] = latest["date"].dt.strftime("%Y-%m-%d")
            lat = int(upsert_df(conn, latest_table, latest, ["stock_code"]) or 0)
            conn.commit()
            LOG.info(
                "[NIGHT YAHOO DAILY SAVE CHUNK] done history=%s latest=%s elapsed=%.1fs",
                hist_total, lat, time.time() - t0,
            )
            return hist_total, lat
        finally:
            try:
                conn.close()
            except Exception:
                pass

    daily_mod._save_symbol_df = save_symbol_df_chunked
    daily_mod._NIGHT_YAHOO_DAILY_CHUNK_SAVE_PATCHED = True
    _INSTALLED = True
    LOG.warning(
        "[NIGHT YAHOO DAILY SAVE CHUNK] installed version=%s chunk=%s timeout=%s busy_ms=%s",
        VERSION,
        os.environ.get("NIGHT_YAHOO_DAILY_SAVE_CHUNK_SIZE", "500"),
        os.environ.get("NIGHT_YAHOO_DAILY_SQLITE_TIMEOUT", "10"),
        os.environ.get("NIGHT_YAHOO_DAILY_SQLITE_BUSY_TIMEOUT_MS", "10000"),
    )
    return True
