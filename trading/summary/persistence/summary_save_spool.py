# ============================================================
# File   : trading/summary/persistence/summary_save_spool.py
# Version: V3-SUMMARY-SAVE-SPOOL-ROBUST-FLUSH
# ------------------------------------------------------------
# 目的:
#   summary DB が database is locked の時でも、計算済みサマリーを失わない。
#
# 仕組み:
#   - main.py 側の補助保存でDBロックになったら jsonl.gz へスプール
#   - main_database.py / summary_database_runner 側で毎分 flush して summary DB へ保存
#   - 壊れた gzip/json spool は .bad へ隔離し、毎回再読込しない
#
# V3:
#   - SQLiteに直接bindできない dict/list/tuple/numpy scalar を安全に変換する。
#   - datetime/date/time/source/interval を補完し、キー列不足で failed_files が残り続ける
#     ケースを減らす。
#   - 失敗時に path / rows / cols / keys / db / table / reason をログに出し、次の原因特定を
#     可能にする。
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

    # datetime が無い spool でも date/time/time_range から復元できる範囲で補完する。
    if "datetime" in out.columns:
        dt_s = pd.to_datetime(out["datetime"], errors="coerce")
        out["datetime"] = dt_s.dt.strftime("%Y-%m-%d %H:%M:%S")
        if "date" not in out.columns:
            out["date"] = dt_s.dt.strftime("%Y-%m-%d")
        if "time" not in out.columns:
            out["time"] = dt_s.dt.strftime("%H:%M:%S")
    elif "date" in out.columns and "time" in out.columns:
        try:
            dt_s = pd.to_datetime(out["date"].astype(str) + " " + out["time"].astype(str), errors="coerce")
            out["datetime"] = dt_s.dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    elif "date" in out.columns and "time_range" in out.columns:
        try:
            t = out["time_range"].astype(str).str.extract(r"(\d{1,2}:\d{2}(?::\d{2})?)", expand=False)
            dt_s = pd.to_datetime(out["date"].astype(str) + " " + t.fillna(""), errors="coerce")
            out["datetime"] = dt_s.dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

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

    if "interval" not in out.columns:
        try:
            out["interval"] = int(float(str(os.getenv("SUMMARY_SAVE_SPOOL_INTERVAL_FALLBACK", "1"))))
        except Exception:
            out["interval"] = 1

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
    # numpy scalar / pandas scalar をPython標準型へ変換
    try:
        item = getattr(v, "item", None)
        if callable(item) and not isinstance(v, (str, bytes, bytearray)):
            return item()
    except Exception:
        pass
    # list/dict/tuple/set はSQLiteへ直接bindできないためJSON文字列化
    if isinstance(v, (dict, list, tuple, set)):
        try:
            return json.dumps(v, ensure_ascii=False, default=str)
        except Exception:
            return str(v)
    if isinstance(v, bool):
        return int(v)
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
        logger.warning("[SUMMARY SAVE SPOOL] empty dataframe interval=%s source=%s date=%s", interval, source, date_yyyymmdd)
        return 0
    db_path = os.path.join(_summary_db_dir(), f"summary{date_yyyymmdd}.db")
    table = _table(interval)
    con = None
    try:
        if not os.path.exists(db_path):
            logger.error("[SUMMARY SAVE SPOOL] target db not found path=%s rows=%s cols=%s", db_path, len(work), list(work.columns)[:50])
            return 0
        con = sqlite3.connect(db_path, timeout=8.0, isolation_level=None)
        con.execute("PRAGMA busy_timeout = 8000")
        exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists is None:
            logger.error("[SUMMARY SAVE SPOOL] target table not found table=%s path=%s rows=%s", table, db_path, len(work))
            return 0
        table_cols = [str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
        keys = _key_cols(work, table_cols, interval)
        if not keys:
            logger.error(
                "[SUMMARY SAVE SPOOL] no key cols interval=%s table=%s rows=%s work_cols=%s table_cols=%s",
                interval,
                table,
                len(work),
                list(work.columns)[:80],
                table_cols[:80],
            )
            return 0
        if "source" in table_cols:
            work["source"] = str(source).lower()
        if "interval" in table_cols:
            work["interval"] = int(interval)
        cols = [c for c in table_cols if c in work.columns and c != "id"]
        before = len(work)
        work = work.dropna(subset=keys).drop_duplicates(subset=keys, keep="last").reset_index(drop=True)
        if work.empty or not cols:
            logger.warning(
                "[SUMMARY SAVE SPOOL] no rows after key cleanup interval=%s source=%s table=%s before=%s keys=%s cols=%s",
                interval,
                source,
                table,
                before,
                keys,
                cols[:50],
            )
            return 0
        con.execute("BEGIN IMMEDIATE")
        delete_sql = f"DELETE FROM {table} WHERE " + " AND ".join([f"{c}=?" for c in keys])
        delete_params = [tuple(_sqlite_value(row[c]) for c in keys) for _, row in work[keys].iterrows()]
        con.executemany(delete_sql, delete_params)
        insert_sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})"
        records = [tuple(_sqlite_value(row.get(c)) for c in cols) for _, row in work[cols].iterrows()]
        con.executemany(insert_sql, records)
        con.commit()
        logger.warning(
            "[SUMMARY SAVE SPOOL] direct flush ok interval=%s source=%s table=%s rows=%s keys=%s cols=%s",
            interval,
            source,
            table,
            len(records),
            keys,
            len(cols),
        )
        return int(len(records))
    except sqlite3.OperationalError as e:
        logger.warning("[SUMMARY SAVE SPOOL] flush locked interval=%s source=%s db=%s table=%s rows=%s err=%s", interval, source, db_path, table, len(work), e)
        try:
            if con is not None:
                con.rollback()
        except Exception:
            pass
        return 0
    except Exception:
        logger.exception(
            "[SUMMARY SAVE SPOOL] flush failed interval=%s source=%s db=%s table=%s rows=%s cols=%s",
            interval,
            source,
            db_path,
            table,
            len(work),
            list(work.columns)[:80],
        )
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


def flush_summary_spool_core(*, max_files: int = 50) -> dict:
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
            logger.warning(
                "[SUMMARY SAVE SPOOL] flush file start path=%s rows=%s interval=%s source=%s date=%s cols=%s",
                path,
                len(df),
                interval,
                source,
                ymd,
                list(df.columns)[:40],
            )
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
                logger.warning(
                    "[SUMMARY SAVE SPOOL] flush file kept for retry path=%s rows=%s interval=%s source=%s date=%s",
                    path,
                    len(df),
                    interval,
                    source,
                    ymd,
                )
        except Exception:
            result["failed_files"] += 1
            logger.exception("[SUMMARY SAVE SPOOL] flush file failed path=%s", path)
    if files:
        logger.warning("[SUMMARY SAVE SPOOL] flush result=%s", result)
    return result


# --- summary_spool_quarantine_patch (native化, 元 core/startup/summary_spool_quarantine_patch.py) ---
# rows=0 の空spoolや古すぎるspoolを毎回リトライし続けて failed_files が減らない問題への対処。
# flush_summary_spool_core を呼ぶ前に、読めない/空/古いスプールファイルを
# summary_spool_*.jsonl.gz の glob 対象から先に退避しておく。
_SUMMARY_SPOOL_QUARANTINE_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _summary_spool_quarantine_env_bool(name: str, default: bool = True) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if raw in _SUMMARY_SPOOL_QUARANTINE_TRUE:
        return True
    if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return bool(default)


def _summary_spool_quarantine_env_int(name: str, default: int) -> int:
    try:
        return int(float(str(os.getenv(name, str(default))).strip()))
    except Exception:
        return int(default)


def _summary_spool_quarantine_safe_rename_with_suffix(path: Path, suffix: str) -> Path | None:
    try:
        dst = path.with_suffix(path.suffix + suffix)
        if dst.exists():
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            dst = path.with_name(f"{path.name}.{stamp}{suffix}")
        path.rename(dst)
        return dst
    except Exception:
        logger.exception("[SUMMARY SPOOL QUARANTINE] rename failed path=%s suffix=%s", path, suffix)
        return None


def _summary_spool_quarantine_ymd_from_name_or_meta(path: Path, meta: dict[str, Any]) -> str:
    try:
        ymd = str(meta.get("date_yyyymmdd") or "").strip()
        if len(ymd) == 8 and ymd.isdigit():
            return ymd
    except Exception:
        pass
    try:
        parts = path.name.split("_")
        for part in parts:
            if len(part) == 8 and part.isdigit():
                return part
    except Exception:
        pass
    return ""


def _summary_spool_quarantine_is_old_ymd(ymd: str, max_age_days: int) -> bool:
    if max_age_days < 0:
        return False
    try:
        d = dt.datetime.strptime(str(ymd), "%Y%m%d").date()
        return (dt.date.today() - d).days > int(max_age_days)
    except Exception:
        return False


def flush_summary_spool(*, max_files: int = 50) -> dict:
    if not _summary_spool_quarantine_env_bool("SUMMARY_SPOOL_QUARANTINE_ENABLED", True):
        return flush_summary_spool_core(max_files=max_files)

    max_age_days = _summary_spool_quarantine_env_int("SUMMARY_SPOOL_QUARANTINE_MAX_AGE_DAYS", 2)
    quarantine_empty = _summary_spool_quarantine_env_bool("SUMMARY_SPOOL_QUARANTINE_EMPTY", True)
    quarantine_old = _summary_spool_quarantine_env_bool("SUMMARY_SPOOL_QUARANTINE_OLD", True)

    quarantined_empty = 0
    quarantined_old = 0
    quarantined_bad = 0
    try:
        d = _spool_dir()
        files = sorted(d.glob("summary_spool_*.jsonl.gz"), key=lambda p: p.stat().st_mtime)[:max_files]
        for path in files:
            try:
                meta, df = _read_spool(path)
            except _BAD_GZIP_ERRORS as e:
                dst = _summary_spool_quarantine_safe_rename_with_suffix(path, ".bad")
                quarantined_bad += 1 if dst is not None else 0
                logger.error("[SUMMARY SPOOL QUARANTINE] bad spool quarantined path=%s dst=%s err=%r", path, dst, e)
                continue
            except Exception:
                # Unknown read error: leave for original flusher to log/handle.
                continue

            ymd = _summary_spool_quarantine_ymd_from_name_or_meta(path, meta if isinstance(meta, dict) else {})
            rows = len(df) if hasattr(df, "__len__") else 0
            if quarantine_empty and int(rows or 0) <= 0:
                dst = _summary_spool_quarantine_safe_rename_with_suffix(path, ".empty")
                quarantined_empty += 1 if dst is not None else 0
                logger.warning(
                    "[SUMMARY SPOOL QUARANTINE] empty spool quarantined path=%s dst=%s ymd=%s rows=%s",
                    path,
                    dst,
                    ymd,
                    rows,
                )
                continue
            if quarantine_old and ymd and _summary_spool_quarantine_is_old_ymd(ymd, max_age_days):
                dst = _summary_spool_quarantine_safe_rename_with_suffix(path, ".old")
                quarantined_old += 1 if dst is not None else 0
                logger.warning(
                    "[SUMMARY SPOOL QUARANTINE] old spool quarantined path=%s dst=%s ymd=%s max_age_days=%s rows=%s",
                    path,
                    dst,
                    ymd,
                    max_age_days,
                    rows,
                )
                continue
    except Exception:
        logger.exception("[SUMMARY SPOOL QUARANTINE] pre-scan failed")

    result = flush_summary_spool_core(max_files=max_files)
    try:
        if isinstance(result, dict):
            result["quarantined_empty"] = int(quarantined_empty)
            result["quarantined_old"] = int(quarantined_old)
            result["quarantined_bad"] = int(quarantined_bad)
    except Exception:
        pass
    return result


__all__ = ["spool_summary_df", "flush_summary_spool"]
