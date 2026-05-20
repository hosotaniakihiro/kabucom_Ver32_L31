# ============================================================
# File   : core/startup/push_bootstrap_fast_restore_patch.py
# Version: Ver01-LIGHTWEIGHT-PUSH-BOOTSTRAP
# ------------------------------------------------------------
# main.py 起動時の pushDB 復元が NAS + SELECT * + 8000行で重い問題を回避する。
#
# 症状:
#   pushDB recent restore query rows=7619 ... elapsed=91.440s
#
# 方針:
#   - main.py の起動復元では、サマリー計算に必要な最小列だけ読む
#   - 板10本・raw_json・IV/Greek等は起動復元から除外
#   - 行数/Lookbackを短縮
#   - 失敗時のみ元の _load_push_db にフォールバック
#
# ENV:
#   PUSH_BOOTSTRAP_FAST_RESTORE=1
#   PUSH_BOOTSTRAP_FAST_MAX_ROWS=3000
#   PUSH_BOOTSTRAP_FAST_LOOKBACK_MINUTES=45
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_LOAD = None

_MIN_COL_CANDIDATES = [
    "symbol", "symbolname", "datetime", "date", "time",
    "price", "current_price", "close", "close_price",
    "volume", "trading_value", "turnover", "turnover_yen",
    "vwap", "previousclose", "opening_price", "high_price", "low_price",
]

_DT_COL_CANDIDATES = ["datetime", "timestamp", "current_price_time", "received_at", "inserted_at", "time"]


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return max(1, int(float(v)))
    except Exception:
        return int(default)


def _table_cols(conn: sqlite3.Connection, table: str = "stream_data") -> list[str]:
    try:
        return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall() if len(r) >= 2]
    except Exception:
        return []


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _select_cols(cols: list[str]) -> list[str]:
    lower_to_real = {c.lower(): c for c in cols}
    out: list[str] = []
    for c in _MIN_COL_CANDIDATES:
        real = lower_to_real.get(c.lower())
        if real and real not in out:
            out.append(real)
    # normalize側が boardを必要とする場面もあるが、起動復元では重くなるため板列は読まない。
    return out


def _dt_col(cols: list[str]) -> str | None:
    lower_to_real = {c.lower(): c for c in cols}
    for c in _DT_COL_CANDIDATES:
        if c.lower() in lower_to_real:
            return lower_to_real[c.lower()]
    return None


def _fast_load_push_db(db_path: str) -> pd.DataFrame:
    if not _env_bool("PUSH_BOOTSTRAP_FAST_RESTORE", True):
        if callable(_ORIG_LOAD):
            return _ORIG_LOAD(db_path)
        return pd.DataFrame()

    started = dt.datetime.now()
    max_rows = _env_int("PUSH_BOOTSTRAP_FAST_MAX_ROWS", 3000)
    lookback_min = _env_int("PUSH_BOOTSTRAP_FAST_LOOKBACK_MINUTES", 45)
    cutoff_dt = started - dt.timedelta(minutes=lookback_min)
    cutoff_text = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    try:
        with sqlite3.connect(db_path, timeout=3.0) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=3000")
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                pass

            cols = _table_cols(conn, "stream_data")
            selected = _select_cols(cols)
            dcol = _dt_col(cols)
            if not selected:
                raise RuntimeError("no selectable columns")

            select_sql = ", ".join(_q(c) for c in selected)
            if dcol and dcol.lower() != "time":
                sql = f"""
                    SELECT {select_sql}
                    FROM stream_data
                    WHERE {_q(dcol)} >= ?
                    ORDER BY rowid DESC
                    LIMIT ?
                """
                params: tuple[Any, ...] = (cutoff_text, max_rows)
                df = pd.read_sql(sql, conn, params=params)
                mode = f"dt_col={dcol} cutoff={cutoff_text}"
            else:
                sql = f"""
                    SELECT {select_sql}
                    FROM stream_data
                    ORDER BY rowid DESC
                    LIMIT ?
                """
                params = (max_rows,)
                df = pd.read_sql(sql, conn, params=params)
                mode = "latest_rowid"

        logger.warning(
            "[PUSH BOOTSTRAP FAST] restore done rows=%s cols=%s max_rows=%s lookback=%s mode=%s elapsed=%.3fs db=%s",
            len(df) if isinstance(df, pd.DataFrame) else 0,
            list(df.columns) if isinstance(df, pd.DataFrame) else [],
            max_rows,
            lookback_min,
            mode,
            (dt.datetime.now() - started).total_seconds(),
            db_path,
        )
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

    except Exception as e:
        logger.warning("[PUSH BOOTSTRAP FAST] failed err=%s -> fallback original", e, exc_info=False)
        if callable(_ORIG_LOAD):
            return _ORIG_LOAD(db_path)
        return pd.DataFrame()


def install() -> bool:
    global _INSTALLED, _ORIG_LOAD
    if _INSTALLED:
        return True
    try:
        import core.startup.push_bootstrap as pb
        cur = getattr(pb, "_load_push_db", None)
        if getattr(cur, "_push_bootstrap_fast_restore_v1", False):
            _INSTALLED = True
            return True
        _ORIG_LOAD = cur
        _fast_load_push_db._push_bootstrap_fast_restore_v1 = True  # type: ignore[attr-defined]
        pb._load_push_db = _fast_load_push_db
        _INSTALLED = True
        logger.warning(
            "[PUSH BOOTSTRAP FAST] installed enabled=%s max_rows=%s lookback=%s",
            _env_bool("PUSH_BOOTSTRAP_FAST_RESTORE", True),
            _env_int("PUSH_BOOTSTRAP_FAST_MAX_ROWS", 3000),
            _env_int("PUSH_BOOTSTRAP_FAST_LOOKBACK_MINUTES", 45),
        )
        return True
    except Exception as e:
        logger.exception("[PUSH BOOTSTRAP FAST] install failed err=%s", e)
        return False

try:
    install()
except Exception as e:
    logger.exception("[PUSH BOOTSTRAP FAST] auto install failed err=%s", e)

__all__ = ["install"]
