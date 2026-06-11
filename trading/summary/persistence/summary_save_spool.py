# ============================================================
# File   : trading/summary/persistence/summary_save_spool.py
# Version: V2-SUMMARY-SAVE-SPOOL-CORRUPT-QUARANTINE
# ------------------------------------------------------------
# 目的:
#   summary DB が database is locked の時でも、計算済みサマリーを失わない。
#
# 仕組み:
#   - main.py 側の補助保存でDBロックになったら jsonl.gz へスプール
#   - main_database.py / summary_database_runner 側で毎分 flush して summary DB へ保存
#   - 壊れた gzip/json spool は .bad へ隔離し、毎回再読込しない
#
# 注意:
#   - スプールはDBではなくファイルなので、SQLiteロックの影響を受けない
#   - flush成功後は .done へrename
#   - DBロック等の一時失敗時は残して次回再試行
#   - 読み取り不能な破損ファイルのみ .bad へ隔離
# ============================================================

from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

try:
    _BAD_GZIP_ERRORS = (EOFError, gzip.BadGzipFile, OSError, UnicodeDecodeError, json.JSONDecodeError)
except AttributeError:  # pragma: no cover - old Python compatibility
    _BAD_GZIP_ERRORS = (EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError)


def _spool_dir() -> Path:
    base = os.getenv(
        "SUMMARY_SAVE_SPOOL_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\runtime\summary_save_spool",
    )
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _summary_db_dir() -> str:
    return os.getenv(
        "SUMMARY_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
    )


def _detect_yyyymmdd(df: pd.DataFrame) -> str:
    try:
        if "datetime" in df.columns:
            s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
            if not s.empty:
                return pd.Timestamp(s.max()).strftime("%Y%m%d")
        if "date" in df.columns:
            s = pd.to_datetime(df["date"], errors="coerce").dropna()
            if not s.empty:
                return pd.Timestamp(s.max()).strftime("%Y%m%d")
    except Exception:
        pass
    return dt.datetime.now().strftime("%Y%m%d")


def _table(interval: int) -> str:
    return f"stock_summary_{int(interval)}min"


def _norm_df(df: pd.DataFrame, *, source: str) -> pd.DataFrame:
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty:
        return out
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.replace(".0", "", regex=False).str.strip()
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
    if "date" in out.columns:
        try:
            s = pd.to_datetime(out["date"], errors="coerce")
            f = s.dt.strftime("%Y-%m-%d")
            out["date"] = f.where(f.notna(), out["date"].astype(str))
        except Exception:
            out["date"] = out["date"].astype(str)
    for c in ("time", "time_range"):
        if c in out.columns:
            out[c] = out[c].astype(str)
    if "source" not in out.columns:
        out["source"] = source
    else:
        out["source"] = out["source"].fillna(source).astype(str)
    if "last_update" not in out.columns:
        out["last_update"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return out


def _sqlite_value(v: Any):
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, dt.date):
        return v.strftime("%Y-%m-%d")
    return v


def _key_cols(work: pd.DataFrame, table_cols: list[str], interval: int) -> list[str]:
    cols = set(work.columns)
    tcols = set(table_cols)
    if {"symbol", "datetime"}.issubset(cols) and {"symbol", "datetime"}.issubset(tcols):
        return ["symbol", "datetime"]
    if int(interval) in (3, 5) and {"symbol", "date", "time_range"}.issubset(cols) and {"symbol", "date", "time_range"}.issubset(tcols):
        return ["symbol", "date", "time_range"]
    if {"symbol", "date", "time"}.issubset(cols) and {"symbol", "date", "time"}.issubset(tcols):
        return ["symbol", "date", "time"]
    return []


def _rename_with_suffix(path: Path, suffix: str) -> Path:
    dst = path.with_suffix(path.suffix + suffix)
    if not dst.exists():
        return dst
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return path.with_name(f"{path.name}.{stamp}{suffix}")


def _quarantine_bad_spool(path: Path, err: BaseException) -> str:
    """Move an unreadable spool file away from *.jsonl.gz retry glob."""
    try:
        bad = _rename_with_suffix(path, ".bad")
        path.rename(bad)
        logger.error("[SUMMARY SAVE SPOOL] corrupt file quarantined path=%s bad=%s err=%r", path, bad, err)
        return str(bad)
    except Exception:
        logger.exception("[SUMMARY SAVE SPOOL] corrupt file quarantine failed path=%s err=%r", path, err)
        return ""


def spool_summary_df(df: pd.DataFrame, *, interval: int, source: str, reason: str = "db_locked") -> str:
    work = _norm_df(df, source=source)
    if work.empty:
        return ""
    ymd = _detect_yyyymmdd(work)
    latest = ""
    try:
        if "datetime" in work.columns:
            latest = str(work["datetime"].max()).replace(" ", "_").replace(":", "")
    except Exception:
        latest = ""
    name = f"summary_spool_{ymd}_{source}_{int(interval)}m_{latest}_{uuid.uuid4().hex[:8]}.jsonl.gz"
    path = _spool_dir() / name
    meta = {
        "interval": int(interval),
        "source": str(source),
        "date_yyyymmdd": ymd,
        "reason": reason,
        "created_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": int(len(work)),
    }
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
        for rec in work.to_dict(orient="records"):
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    logger.warning("[SUMMARY SAVE SPOOL] spooled path=%s rows=%s interval=%s source=%s reason=%s", path, len(work), interval, source, reason)
    return str(path)


def _read_spool(path: Path) -> tuple[dict, pd.DataFrame]:
    meta: dict = {}
    rows: list[dict] = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if i == 0 and isinstance(obj, dict) and "_meta" in obj:
                meta = dict(obj.get("_meta") or {})
            else:
                rows.append(obj)
    return meta, pd.DataFrame(rows)


def _save_direct(df: pd.DataFrame, *, interval: int, source: str, date_yyyymmdd: str) -> int:
    work = _norm_df(df, source=source)
    if work.empty:
        return 0
    db_path = os.path.join(_summary_db_dir(), f"summary{date_yyyymmdd}.db")
    table = _table(interval)
    con = None
    try:
        if not os.path.exists(db_path):
            logger.error("[SUMMARY SAVE SPOOL] target db not found path=%s", db_path)
            return 0
        con = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
        con.execute("PRAGMA busy_timeout = 5000")
        exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists is None:
            logger.error("[SUMMARY SAVE SPOOL] target table not found table=%s path=%s", table, db_path)
            return 0
        table_cols = [str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
        keys = _key_cols(work, table_cols, interval)
        if not keys:
            logger.error("[SUMMARY SAVE SPOOL] no key cols interval=%s table=%s", interval, table)
            return 0
        if "source" in table_cols:
            work["source"] = str(source).lower()
        if "interval" in table_cols and "interval" not in work.columns:
            work["interval"] = int(interval)
        cols = [c for c in table_cols if c in work.columns and c != "id"]
        work = work.dropna(subset=keys).drop_duplicates(subset=keys, keep="last").reset_index(drop=True)
        if work.empty or not cols:
            return 0
        con.execute("BEGIN IMMEDIATE")
        delete_sql = f"DELETE FROM {table} WHERE " + " AND ".join([f"{c}=?" for c in keys])
        delete_params = [tuple(_sqlite_value(row[c]) for c in keys) for _, row in work[keys].iterrows()]
        con.executemany(delete_sql, delete_params)
        insert_sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})"
        records = [tuple(_sqlite_value(row.get(c)) for c in cols) for _, row in work[cols].iterrows()]
        con.executemany(insert_sql, records)
        con.commit()
        return int(len(records))
    except sqlite3.OperationalError as e:
        logger.warning("[SUMMARY SAVE SPOOL] flush locked interval=%s source=%s err=%s", interval, source, e)
        try:
            if con is not None:
                con.rollback()
        except Exception:
            pass
        return 0
    except Exception:
        logger.exception("[SUMMARY SAVE SPOOL] flush failed interval=%s source=%s", interval, source)
        try:
            if con is not None:
                con.rollback()
        except Exception:
            pass
        return 0
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def flush_summary_spool(*, max_files: int = 50) -> dict:
    d = _spool_dir()
    files = sorted(d.glob("summary_spool_*.jsonl.gz"), key=lambda p: p.stat().st_mtime)[:max_files]
    result = {"files": len(files), "flushed_files": 0, "saved_rows": 0, "failed_files": 0, "bad_files": 0}
    for path in files:
        try:
            try:
                meta, df = _read_spool(path)
            except _BAD_GZIP_ERRORS as e:
                result["bad_files"] += 1
                _quarantine_bad_spool(path, e)
                continue
            interval = int(meta.get("interval") or 1)
            source = str(meta.get("source") or "push")
            ymd = str(meta.get("date_yyyymmdd") or _detect_yyyymmdd(df))
            saved = _save_direct(df, interval=interval, source=source, date_yyyymmdd=ymd)
            if saved > 0:
                done = _rename_with_suffix(path, ".done")
                try:
                    path.rename(done)
                except Exception:
                    path.unlink(missing_ok=True)
                result["flushed_files"] += 1
                result["saved_rows"] += int(saved)
                logger.warning("[SUMMARY SAVE SPOOL] flushed path=%s saved=%s", path, saved)
            else:
                result["failed_files"] += 1
        except Exception:
            result["failed_files"] += 1
            logger.exception("[SUMMARY SAVE SPOOL] flush file failed path=%s", path)
    if files:
        logger.warning("[SUMMARY SAVE SPOOL] flush result=%s", result)
    return result


__all__ = ["spool_summary_df", "flush_summary_spool"]