# ============================================================
# File   : database/upsert/ranking_summary_upsert.py
# Version: PRODUCTION-STABLE-REV1.1-RANKING-SUMMARY-UPSERT
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import time as _rs_time
import threading
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
# スキーマ保守を save path から外す (2026-09-03)
# ------------------------------------------------------------
# 旧実装は 1 回の save ごとに BEGIN IMMEDIATE の内側で
#     ensure_ranking_summary_table
#     delete_null_key_rows        (COUNT + DELETE + COUNT)
#     dedupe_ranking_summary_table(COUNT + GROUP BY 全表 + DELETE)
# を実行していた。実測 (ranking20260903.db 282.8MB, アイドル NAS):
#     1min 75,099 行 -> COUNT 2.76s + null-key 8.97s + dedupe 8.49s = 約 23 秒
#     しかも null-key 該当 0 行 / 重複 0 件。毎分、空振りの掃除に 23 秒。
# 書き込みトランザクション内で他 writer と競合すると persist=139.863s になる。
#
# 保守は「プロセス内で db_path + table ごとに 1 回」に限定する。
# db_path には日付が入る (ranking20260903.db) ので、日付が変われば
# 別キーになり自動的に再実行される = 起動時 / 日付変更時のみ、を満たす。
# db_prepare も起動時に同じ ensure を行うので二重に安全。
#
# RANKING_SUMMARY_SAVE_MAINTENANCE=always で従来動作 (毎回実行)、
# never で完全に無効化できる。既定は once。
# ============================================================
_MAINTAINED_KEYS: set = set()
_MAINTAIN_LOCK = threading.Lock()


def _maintenance_mode() -> str:
    v = str(os.environ.get("RANKING_SUMMARY_SAVE_MAINTENANCE", "once")).strip().lower()
    return v if v in {"once", "always", "never"} else "once"


def _run_schema_maintenance(con: sqlite3.Connection, *, interval: int, table: str,
                            path: str, force: bool = False) -> bool:
    """ensure + cleanup。実行したら True。

    force=True はテーブル不存在の fallback 用。
    """
    mode = _maintenance_mode()
    key = (os.path.normcase(str(path)), table)

    if not force:
        if mode == "never":
            return False
        if mode == "once":
            with _MAINTAIN_LOCK:
                if key in _MAINTAINED_KEYS:
                    return False

    t0 = _rs_time.perf_counter()
    ensure_ranking_summary_table(con, interval=interval)
    t1 = _rs_time.perf_counter()
    delete_null_key_rows(con, table)
    t2 = _rs_time.perf_counter()
    dedupe_ranking_summary_table(con, table)
    t3 = _rs_time.perf_counter()

    with _MAINTAIN_LOCK:
        _MAINTAINED_KEYS.add(key)

    logger.warning(
        "[RANKING SUMMARY SAVE] schema maintenance done table=%s force=%s mode=%s "
        "ensure=%.3fs null_key=%.3fs dedupe=%.3fs total=%.3fs "
        "(以降この db+table では save ごとに実行しない)",
        table, force, mode, t1 - t0, t2 - t1, t3 - t2, t3 - t0,
    )
    return True


def _rs_should_stop(label: str) -> bool:
    """協調キャンセルの降り場所。

    worker context の外 (単体呼び出し) では常に False なので通常経路は不変。
    """
    try:
        from scheduler_jobs.summary.stage_tracker import should_stop

        return bool(should_stop(label))
    except Exception:
        return False


def _install_cancel_progress_handler(con: sqlite3.Connection) -> None:
    """長い statement を途中で中断できるようにする。

    SQLite の progress handler から例外を投げると、実行中の statement が
    sqlite3.OperationalError('interrupted') で落ちる。
    cancel 済み worker が NAS 上の巨大 DELETE/GROUP BY を最後まで
    走らせないための保険。
    """
    try:
        n = int(os.environ.get("RANKING_SUMMARY_SAVE_PROGRESS_OPS", "100000"))
    except Exception:
        n = 100000
    if n <= 0:
        return

    def _cb() -> int:
        # 0 以外を返すと SQLite が statement を中断する
        return 1 if _rs_should_stop("ranking_summary_save.progress") else 0

    try:
        con.set_progress_handler(_cb, n)
    except Exception:
        logger.debug("[RANKING SUMMARY SAVE] progress handler 設定に失敗", exc_info=True)



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
    # 2026-09-11: 既定を NAS 固定から「現在の既定」(ローカル) へ。
    #   DEFAULT_RANKING_DIR は NAS のパスで import 時に固定される。
    #   ranking DB は 2026-09-07 にローカルへ移っており、ここだけ NAS を
    #   指していたため ranking_summary の保存が 1 回 53 秒 (upsert 24s +
    #   commit 28s) かかり、ranking_job_all が平均 111 秒に膨らんでいた。
    #   その結果 bar_age が 180 秒上限を超えて候補が全滅していた。
    #   書き手・読み手・スキーマを同時に揃えること。片側だけ変えると
    #   「書いた場所と読む場所が違う」状態になる (2026-09-11 に実際に発生)。
    ranking_dir: Optional[str] = None,
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

    # 最初の降り場所。normalize_for_save は 675 銘柄 x 97 列で 2.4 秒かかるので、
    # cancel 済みならそこへ入る前に降りる (cancel SLA 5 秒を守るため)。
    if _rs_should_stop("ranking_summary_save.entry"):
        logger.warning(
            "[RANKING SUMMARY SAVE] cancelled at entry -> normalize もしない interval=%s rows=%s",
            interval, len(df),
        )
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

    # cancel 済み worker はそもそも保存しない (古い generation の結果を残さない)
    if _rs_should_stop("ranking_summary_save.before"):
        logger.warning(
            "[RANKING SUMMARY SAVE] cancelled before save -> 保存しない table=%s rows=%s",
            table, len(save_df),
        )
        return 0

    try:
        chunk_size = max(200, int(os.environ.get("RANKING_SUMMARY_SAVE_CHUNK_ROWS", "2000")))
    except Exception:
        chunk_size = 2000

    force_maintenance = False

    for attempt in range(1, MAX_SAVE_RETRY + 1):
        con: Optional[sqlite3.Connection] = None

        try:
            with RANKING_SUMMARY_DB_LOCK:
                _t_lock = _rs_time.perf_counter()
                con = _connect(path)
                _install_cancel_progress_handler(con)
                _t_conn = _rs_time.perf_counter()

                _begin_immediate(con)
                _t_begin = _rs_time.perf_counter()

                if _rs_should_stop("ranking_summary_save.before_schema"):
                    _rollback_quietly(con)
                    _close_quietly(con)
                    con = None
                    logger.warning(
                        "[RANKING SUMMARY SAVE] cancelled before schema -> rollback table=%s", table
                    )
                    return 0

                maintained = _run_schema_maintenance(
                    con, interval=interval, table=table, path=path, force=force_maintenance
                )
                _t_schema = _rs_time.perf_counter()

                # chunk ごとに降りられるようにする。
                # 1 chunk = executemany 1 回。間で cancel を見る。
                written = 0
                for i in range(0, len(rows), chunk_size):
                    if _rs_should_stop("ranking_summary_save.chunk"):
                        _rollback_quietly(con)
                        _close_quietly(con)
                        con = None
                        logger.warning(
                            "[RANKING SUMMARY SAVE] cancelled during upsert -> rollback "
                            "table=%s written=%s/%s",
                            table, written, len(rows),
                        )
                        return 0
                    con.executemany(sql, rows[i:i + chunk_size])
                    written += len(rows[i:i + chunk_size])
                _t_upsert = _rs_time.perf_counter()

                if _rs_should_stop("ranking_summary_save.before_commit"):
                    _rollback_quietly(con)
                    _close_quietly(con)
                    con = None
                    logger.warning(
                        "[RANKING SUMMARY SAVE] cancelled before commit -> rollback table=%s", table
                    )
                    return 0

                _commit(con)
                _t_commit = _rs_time.perf_counter()

                _close_quietly(con)
                con = None

            _total = _t_commit - _t_lock
            _log = logger.warning if _total >= 3.0 else logger.info
            _log(
                "[RANKING SUMMARY SAVE] saved table=%s rows=%s symbols=%s dt_min=%s dt_max=%s "
                "attempt=%s/%s cols=%s | connect=%.3fs begin=%.3fs schema=%.3fs(ran=%s) "
                "upsert=%.3fs(chunks=%s) commit=%.3fs total=%.3fs",
                table,
                len(save_df),
                save_df["symbol"].nunique() if "symbol" in save_df.columns else 0,
                save_df["datetime"].min() if "datetime" in save_df.columns else None,
                save_df["datetime"].max() if "datetime" in save_df.columns else None,
                attempt,
                MAX_SAVE_RETRY,
                len(cols),
                _t_conn - _t_lock, _t_begin - _t_conn, _t_schema - _t_begin, maintained,
                _t_upsert - _t_schema, (len(rows) + chunk_size - 1) // chunk_size,
                _t_commit - _t_upsert, _total,
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

            # テーブルが無い / 列が足りない場合だけ、次の attempt で
            # スキーマ保守を強制する (毎回 ensure しない代わりの安全弁)。
            _msg = str(e).lower()
            if ("no such table" in _msg or "no such column" in _msg
                    or "has no column named" in _msg):
                force_maintenance = True
                logger.warning(
                    "[RANKING SUMMARY SAVE] schema mismatch -> 次の attempt で ensure を強制する "
                    "table=%s attempt=%s/%s err=%s",
                    table, attempt, MAX_SAVE_RETRY, e,
                )
                if attempt < MAX_SAVE_RETRY:
                    continue

            if "interrupted" in _msg:
                logger.warning(
                    "[RANKING SUMMARY SAVE] cancelled (sqlite interrupted) -> 保存しない table=%s", table
                )
                return 0

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