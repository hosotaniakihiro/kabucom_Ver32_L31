# ============================================================
# File   : core/startup/summary_db_date_guard_patch.py
# Version: V1-SUMMARY-DB-DATE-GUARD
# ------------------------------------------------------------
# 目的:
#   summaryYYYYMMDD.db に別日 datetime の行が混入するのを防ぐ。
#
# 対策:
#   1. bulk_upsert_summary() 保存直前に、接続先 summary DB の
#      ファイル名 summaryYYYYMMDD.db から対象日を取得する。
#   2. df['datetime'] の日付が対象日と違う行は保存しない。
#   3. 起動時/必要時に stock_summary_1min/3min/5min から
#      既存の別日データを削除する。
#
# 重要:
#   - recovery / backfill / startup 系であっても、日次DBへ別日行は保存しない。
#   - 前日データを使う場合は計算用に読むだけ。保存先は当日DBの日付に限定。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_BULK_UPSERT = None
_CLEANUP_DONE: set[str] = set()
_CLEANUP_LOCK = threading.RLock()

_TABLES = ("stock_summary_1min", "stock_summary_3min", "stock_summary_5min")


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _safe_datetime_series(s: Any) -> pd.Series:
    try:
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0] if s.shape[1] else pd.Series(dtype="object")
        if not isinstance(s, pd.Series):
            s = pd.Series(s)
        out = pd.to_datetime(s, errors="coerce")
        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass
        return out
    except Exception:
        return pd.Series(dtype="datetime64[ns]")


def _engine_url_to_path(engine: Any) -> Optional[str]:
    try:
        url = getattr(engine, "url", None)
        if url is None:
            return None
        database = getattr(url, "database", None)
        if database:
            return str(database)
        s = str(url)
        if s.startswith("sqlite:///"):
            return s.replace("sqlite:///", "", 1).replace("/", "\\")
    except Exception:
        pass
    return None


def _resolve_summary_engine() -> Any:
    candidates = []
    try:
        from database.session import get_summary_engine
        candidates.append(get_summary_engine)
    except Exception:
        pass
    try:
        from database.session import summary_engine
        candidates.append(lambda: summary_engine)
    except Exception:
        pass
    try:
        from database.session import Session_summary
        candidates.append(lambda: getattr(Session_summary, "bind", None))
    except Exception:
        pass
    try:
        from trading.summary.persistence.core import upsert_engine as ue
        candidates.append(lambda: getattr(ue, "summary_engine", None))
        candidates.append(lambda: getattr(ue, "engine", None))
    except Exception:
        pass

    for fn in candidates:
        try:
            engine = fn()
            if engine is not None:
                return engine
        except Exception:
            continue
    return None


def _resolve_summary_db_path() -> Optional[str]:
    # まず現在の summary engine から取得
    try:
        engine = _resolve_summary_engine()
        p = _engine_url_to_path(engine)
        if p:
            return p
    except Exception:
        pass

    # フォールバック: よく使う環境変数
    for key in ("SUMMARY_DB_PATH", "AUTOSTOCK_SUMMARY_DB_PATH", "KABU_SUMMARY_DB_PATH"):
        try:
            v = os.environ.get(key, "").strip()
            if v:
                return v
        except Exception:
            pass
    return None


def _target_date_from_path(path: Optional[str]) -> Optional[dt.date]:
    if not path:
        return None
    m = re.search(r"summary(\d{8})\.db", str(path), flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except Exception:
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
        return bool(row)
    except Exception:
        return False


def _cleanup_wrong_date_rows(db_path: str, target_date: dt.date) -> None:
    if not _env_bool("SUMMARY_DB_DATE_GUARD_CLEANUP_ENABLED", True):
        return
    key = f"{db_path}|{target_date.isoformat()}"
    with _CLEANUP_LOCK:
        if key in _CLEANUP_DONE:
            return
        _CLEANUP_DONE.add(key)

    if not db_path or not os.path.exists(db_path):
        return

    target = target_date.isoformat()
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        total_deleted = 0
        for table in _TABLES:
            if not _table_exists(conn, table):
                continue
            try:
                before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                wrong = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE datetime IS NOT NULL AND substr(CAST(datetime AS TEXT),1,10) <> ?',
                    (target,),
                ).fetchone()[0]
                if int(wrong) > 0:
                    conn.execute(
                        f'DELETE FROM "{table}" WHERE datetime IS NOT NULL AND substr(CAST(datetime AS TEXT),1,10) <> ?',
                        (target,),
                    )
                    after = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    deleted = int(before) - int(after)
                    total_deleted += deleted
                    logger.warning(
                        "[SUMMARY DB DATE GUARD] cleanup deleted table=%s db=%s target_date=%s before=%s wrong=%s after=%s deleted=%s",
                        table,
                        db_path,
                        target,
                        before,
                        wrong,
                        after,
                        deleted,
                    )
                else:
                    logger.info(
                        "[SUMMARY DB DATE GUARD] cleanup ok table=%s db=%s target_date=%s rows=%s wrong=0",
                        table,
                        db_path,
                        target,
                        before,
                    )
            except Exception:
                logger.exception("[SUMMARY DB DATE GUARD] cleanup failed table=%s db=%s", table, db_path)
        conn.commit()
        if total_deleted:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
    except Exception:
        logger.exception("[SUMMARY DB DATE GUARD] cleanup db failed db=%s target_date=%s", db_path, target)
    finally:
        try:
            conn.close()  # type: ignore[name-defined]
        except Exception:
            pass


def _filter_df_to_summary_db_date(df: pd.DataFrame, *, interval: int, save_reason: str) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return df
    if "datetime" not in df.columns:
        logger.warning(
            "[SUMMARY DB DATE GUARD] datetime column missing -> cannot date-filter interval=%s rows=%s save_reason=%s",
            interval,
            len(df),
            save_reason,
        )
        return df

    db_path = _resolve_summary_db_path()
    target_date = _target_date_from_path(db_path)
    if target_date is None:
        logger.warning(
            "[SUMMARY DB DATE GUARD] target date unresolved -> skip date filter interval=%s db_path=%s rows=%s save_reason=%s",
            interval,
            db_path,
            len(df),
            save_reason,
        )
        return df

    try:
        _cleanup_wrong_date_rows(str(db_path), target_date)
    except Exception:
        logger.debug("[SUMMARY DB DATE GUARD] cleanup call failed", exc_info=True)

    out = df.copy()
    dt_s = _safe_datetime_series(out["datetime"])
    mask_valid = dt_s.notna()
    mask_target = mask_valid & (dt_s.dt.date == target_date)
    before = len(out)
    wrong = int((mask_valid & ~mask_target).sum())
    invalid = int((~mask_valid).sum())

    if wrong or invalid:
        sample = []
        try:
            sample = (
                out.loc[mask_valid & ~mask_target, [c for c in ("symbol", "datetime") if c in out.columns]]
                .head(10)
                .to_dict("records")
            )
        except Exception:
            pass
        logger.warning(
            "[SUMMARY DB DATE GUARD] drop wrong-date rows interval=%s db=%s target_date=%s rows_before=%s wrong_date=%s invalid_dt=%s sample=%s save_reason=%s",
            interval,
            db_path,
            target_date.isoformat(),
            before,
            wrong,
            invalid,
            sample,
            save_reason,
        )
    out = out.loc[mask_target].copy()
    after = len(out)
    if after != before:
        logger.warning(
            "[SUMMARY DB DATE GUARD] filtered interval=%s rows_before=%s rows_after=%s target_date=%s save_reason=%s",
            interval,
            before,
            after,
            target_date.isoformat(),
            save_reason,
        )
    return out


def install() -> bool:
    global _INSTALLED, _ORIG_BULK_UPSERT
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_DB_DATE_GUARD_ENABLED", True):
        logger.warning("[SUMMARY DB DATE GUARD] disabled by env")
        return False
    try:
        import trading.summary.persistence.summary_saver_bulk as ssb
    except Exception:
        logger.exception("[SUMMARY DB DATE GUARD] import summary_saver_bulk failed")
        return False

    old = getattr(ssb, "bulk_upsert_summary", None)
    if getattr(old, "_summary_db_date_guard_patch", False):
        _INSTALLED = True
        return True
    if not callable(old):
        logger.error("[SUMMARY DB DATE GUARD] bulk_upsert_summary not callable")
        return False

    _ORIG_BULK_UPSERT = old

    def _patched_bulk_upsert_summary(df, interval: int, *args, **kwargs):
        save_reason = str(kwargs.get("save_reason", "") or "")
        filtered = _filter_df_to_summary_db_date(df, interval=int(interval), save_reason=save_reason)
        if filtered is None or getattr(filtered, "empty", False):
            try:
                rows = 0 if df is None else len(df)
            except Exception:
                rows = -1
            logger.warning(
                "[SUMMARY DB DATE GUARD] all rows removed -> skip upsert interval=%s original_rows=%s save_reason=%s",
                interval,
                rows,
                save_reason,
            )
            return 0
        return _ORIG_BULK_UPSERT(filtered, interval, *args, **kwargs)

    _patched_bulk_upsert_summary._summary_db_date_guard_patch = True  # type: ignore[attr-defined]
    _patched_bulk_upsert_summary._summary_db_date_guard_original = old  # type: ignore[attr-defined]

    ssb.bulk_upsert_summary = _patched_bulk_upsert_summary
    try:
        ssb.save_summary_bulk = lambda df, interval, lock_timeout_sec=1.0, skip_if_busy=False, latest_only=False, save_reason="": _patched_bulk_upsert_summary(
            df,
            interval=interval,
            lock_timeout_sec=lock_timeout_sec,
            skip_if_busy=skip_if_busy,
            latest_only=latest_only,
            save_reason=save_reason,
        )
        ssb.save_summary_df = ssb.save_summary_bulk
    except Exception:
        pass

    # 起動時にも一度だけ既存DBを掃除する
    try:
        db_path = _resolve_summary_db_path()
        target_date = _target_date_from_path(db_path)
        if db_path and target_date:
            _cleanup_wrong_date_rows(str(db_path), target_date)
    except Exception:
        logger.debug("[SUMMARY DB DATE GUARD] startup cleanup failed", exc_info=True)

    _INSTALLED = True
    logger.warning("[SUMMARY DB DATE GUARD] installed enabled=True cleanup=True")
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY DB DATE GUARD] auto install failed")


__all__ = ["install"]
