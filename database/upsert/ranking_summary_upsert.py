# ============================================================
# File   : database/upsert/ranking_summary_upsert.py
# Version: PRODUCTION-STABLE-REV1.1-RANKING-SUMMARY-UPSERT
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Optional

import pandas as pd

from database.paths.ranking_paths import DEFAULT_RANKING_DIR, get_ranking_db_path
from database.schema.ranking_summary_schema import (
    RANKING_SUMMARY_DB_LOCK,
    delete_null_key_rows,
    dedupe_ranking_summary_table,
    ensure_ranking_summary_table,
    table_name,
)
from database.sqlite import (
    DEFAULT_BUSY_TIMEOUT_MS,
    is_lock_error,
    lock_sleep_seconds,
    prepare_sqlite_connection,
    quote_ident,
)

try:
    from trading.ranking.summary.features.normalizer import normalize_for_save
except Exception:
    normalize_for_save = None

logger = logging.getLogger(__name__)

MAX_SAVE_RETRY = 6


# ============================================================
# SQLite helpers
# ============================================================

def _connect(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(str(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    con = sqlite3.connect(
        str(path),
        timeout=max(10.0, float(DEFAULT_BUSY_TIMEOUT_MS) / 1000.0),
        check_same_thread=False,
        isolation_level=None,
    )
    prepare_sqlite_connection(con)
    return con


def _begin_immediate(con: sqlite3.Connection) -> None:
    con.execute("BEGIN IMMEDIATE")


def _commit(con: sqlite3.Connection) -> None:
    con.execute("COMMIT")


def _rollback_quietly(con: sqlite3.Connection | None) -> None:
    if con is None:
        return
    try:
        con.execute("ROLLBACK")
    except Exception:
        pass


def _close_quietly(con: sqlite3.Connection | None) -> None:
    if con is None:
        return
    try:
        con.close()
    except Exception:
        pass


# ============================================================
# Save
# ============================================================

def save_ranking_summary(
    df: pd.DataFrame,
    *,
    interval: int,
    trade_date=None,
    db_path: Optional[str] = None,
    ranking_dir: str = DEFAULT_RANKING_DIR,
) -> int:
    """
    ranking_summary_{interval}min に UPSERT 保存する。

    特徴:
      - normalize_for_save で保存前整形
      - schema ensure / cleanup / upsert を同一 transaction 内で実行
      - COMMIT失敗を成功扱いしない
      - SQLite lock は retry
    """
    if df is None or df.empty:
        logger.info("[RANKING SUMMARY SAVE] skip empty interval=%s", interval)
        return 0

    if normalize_for_save is None:
        logger.error("[RANKING SUMMARY SAVE] normalizer unavailable")
        return 0

    interval = int(interval)
    table = table_name(interval)
    path = db_path or get_ranking_db_path(trade_date, ranking_dir=ranking_dir)

    save_df = normalize_for_save(df, interval=interval)
    if save_df.empty:
        logger.info("[RANKING SUMMARY SAVE] normalized empty interval=%s", interval)
        return 0

    cols = list(save_df.columns)
    placeholders = ",".join(["?"] * len(cols))
    col_sql = ",".join([quote_ident(c) for c in cols])

    update_cols = [c for c in cols if c not in ("symbol", "datetime")]
    update_sql = ",".join(
        [f"{quote_ident(c)}=excluded.{quote_ident(c)}" for c in update_cols]
    )

    sql = f"""
        INSERT INTO {quote_ident(table)} ({col_sql})
        VALUES ({placeholders})
        ON CONFLICT(symbol, datetime)
        DO UPDATE SET {update_sql}
    """

    rows = [tuple(row) for row in save_df.itertuples(index=False, name=None)]
    last_err: BaseException | None = None

    for attempt in range(1, MAX_SAVE_RETRY + 1):
        con: Optional[sqlite3.Connection] = None

        try:
            with RANKING_SUMMARY_DB_LOCK:
                con = _connect(path)
                _begin_immediate(con)

                ensure_ranking_summary_table(con, interval=interval)
                delete_null_key_rows(con, table)
                dedupe_ranking_summary_table(con, table)

                con.executemany(sql, rows)

                _commit(con)

                _close_quietly(con)
                con = None

            logger.info(
                "[RANKING SUMMARY SAVE] saved table=%s rows=%s symbols=%s dt_min=%s dt_max=%s attempt=%s/%s cols=%s",
                table,
                len(save_df),
                save_df["symbol"].nunique() if "symbol" in save_df.columns else 0,
                save_df["datetime"].min() if "datetime" in save_df.columns else None,
                save_df["datetime"].max() if "datetime" in save_df.columns else None,
                attempt,
                MAX_SAVE_RETRY,
                len(cols),
            )
            return len(save_df)

        except sqlite3.IntegrityError as e:
            last_err = e
            _rollback_quietly(con)

            logger.warning(
                "[RANKING SUMMARY SAVE] sqlite integrity error table=%s rows=%s attempt=%s/%s err=%s",
                table,
                len(save_df),
                attempt,
                MAX_SAVE_RETRY,
                e,
            )

            if attempt < MAX_SAVE_RETRY:
                lock_sleep_seconds(attempt)
                continue

        except sqlite3.OperationalError as e:
            last_err = e
            _rollback_quietly(con)

            if is_lock_error(e) and attempt < MAX_SAVE_RETRY:
                slept = lock_sleep_seconds(attempt)
                logger.warning(
                    "[RANKING SUMMARY SAVE] sqlite locked retry table=%s rows=%s attempt=%s/%s sleep=%.2fs err=%s",
                    table,
                    len(save_df),
                    attempt,
                    MAX_SAVE_RETRY,
                    slept,
                    e,
                )
                continue

            logger.warning(
                "[RANKING SUMMARY SAVE] sqlite operational error table=%s rows=%s attempt=%s/%s err=%s",
                table,
                len(save_df),
                attempt,
                MAX_SAVE_RETRY,
                e,
            )

            if attempt < MAX_SAVE_RETRY:
                lock_sleep_seconds(attempt)
                continue

        except Exception as e:
            last_err = e
            _rollback_quietly(con)

            if is_lock_error(e) and attempt < MAX_SAVE_RETRY:
                slept = lock_sleep_seconds(attempt)
                logger.warning(
                    "[RANKING SUMMARY SAVE] locked retry table=%s rows=%s attempt=%s/%s sleep=%.2fs err=%s",
                    table,
                    len(save_df),
                    attempt,
                    MAX_SAVE_RETRY,
                    slept,
                    e,
                )
                continue

            logger.exception(
                "[RANKING SUMMARY SAVE] failed table=%s rows=%s attempt=%s/%s",
                table,
                len(save_df),
                attempt,
                MAX_SAVE_RETRY,
            )
            break

        finally:
            _close_quietly(con)

    logger.error(
        "[RANKING SUMMARY SAVE] failed after retries table=%s rows=%s last_err=%s",
        table,
        len(save_df),
        str(last_err)[:500] if last_err else None,
    )
    return 0


__all__ = [
    "save_ranking_summary",
]