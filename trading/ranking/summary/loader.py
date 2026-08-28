# ============================================================
# File   : trading/ranking/summary/loader.py
# Ver    : PRODUCTION-STABLE-REV1.0-RANKING-SUMMARY-LOADER
# ------------------------------------------------------------
# 【概要】
#   ranking_snapshot_1min の読み込み専用モジュール
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time as _time
from typing import Any, Iterable, Optional

import pandas as pd

from trading.ranking.summary.constants import (
    DATETIME_CANDIDATES,
    RANKING_SNAPSHOT_TABLE,
)
from trading.ranking.summary import incremental_cache as _inc
from trading.ranking.summary.normalize import (
    filter_lookback,
    filter_symbols,
    filter_trade_date_if_possible,
    normalize_ranking_snapshot_df,
)
from trading.ranking.summary.utils import (
    default_ranking_db_path,
    normalize_symbols,
    normalize_trade_date,
    path_exists,
)

logger = logging.getLogger(__name__)


# ============================================================
# Ranking DB read: cancel 可能な chunk 読み
# ------------------------------------------------------------
# 2026-08-28 14:45 の cancel SLA breach スタック:
#   ranking_summary_jobs.job_ranking_summary_1m
#     -> runner._run_interval
#     -> resample.build_ranking_summary_base_df
#     -> loader.load_ranking_snapshot_1min
#     -> loader.read_sqlite_table
#     -> pd.read_sql_query -> pandas_sql.read_query
#     -> cursor.fetchall()          <-- ここで停止
#   stage_elapsed=1117.9s / cancel waited=849.4s / SLA=5s
#
# 単なる Python ループではなく **SQLite の fetchall 中**なので、
# should_stop() を挟む場所が無く cancel が効かなかった。
#
# 対策:
#   1. fetchall をやめて fetchmany で chunk 読みする
#      -> chunk 境界ごとに should_stop() / deadline を見る
#   2. conn.set_progress_handler() で **クエリ実行中**も中断できるようにする
#      -> callback が非 0 を返すと SQLite が interrupt する
#   3. 行数上限 (LIMIT) を必ず持たせる
#   4. [RANKING DB READ STAGE] で connect/execute/fetch を分けて出す
#
# timeout は伸ばさない。NAS SQLite の安全性 (query_only / busy_timeout) は維持。
# ============================================================

_RANKING_READ_CHUNK = 5000
# NAS I/O でブロックしている間は VM 命令が進まないため、
# 粒度を細かくしても中断は I/O 完了待ちになる。
# それでも CPU 側 (ORDER BY のソート等) は細かく刻めるので小さくする。
_RANKING_PROGRESS_OPS = 1000


class RankingReadCancelled(Exception):
    """cancel / deadline により DB 読みを打ち切った。"""


def _ranking_read_chunk_rows() -> int:
    try:
        v = os.getenv("RANKING_DB_READ_CHUNK_ROWS")
        if v and str(v).strip():
            return max(500, int(float(str(v).strip())))
    except Exception:
        pass
    return _RANKING_READ_CHUNK


def _ranking_read_max_rows() -> int:
    """1 回の realtime 読み取りで取る行数の上限。0 以下で無制限 (非推奨)。"""
    try:
        v = os.getenv("RANKING_DB_READ_MAX_ROWS")
        if v and str(v).strip():
            return int(float(str(v).strip()))
    except Exception:
        pass
    return 60000


def _ranking_read_deadline_sec() -> float:
    try:
        v = os.getenv("RANKING_DB_READ_DEADLINE_SEC")
        if v and str(v).strip():
            return max(1.0, float(str(v).strip()))
    except Exception:
        pass
    return 20.0


def _stage_should_stop(label: str) -> bool:
    try:
        from scheduler_jobs.summary import stage_tracker as _st

        return bool(_st.should_stop(label))
    except Exception:
        return False


def read_sqlite_table(
    *,
    db_path: str,
    sql: str,
    params: Optional[dict[str, Any]] = None,
    deadline_sec: Optional[float] = None,
    cooperative_stop: bool = True,
) -> pd.DataFrame:
    """ranking DB を chunk 読みする。

    deadline_sec:
        既定は RANKING_DB_READ_DEADLINE_SEC (20 秒)。
        前景の tick を止めないための締切なので、
        **前景を止めない背景処理**からはより長い値を渡す。
        背景再同期は前景 tick と NAS を取り合うため 20 秒では足りず、
        rows_fetched=0 / cancelled=True で黙って空を返していた
        (2026-08-28 実測 total_sec=20.0)。

    cooperative_stop:
        False にすると stage_tracker の停止要求を見ない。
        背景スレッドは前景 worker の stage には属さないため、
        前景の stage 終了で巻き込み停止されないようにする。
    """
    if not path_exists(db_path):
        logger.warning(
            "[RANKING SUMMARY RUNNER] ranking db not found path=%s",
            db_path,
        )
        return pd.DataFrame()

    conn: Optional[sqlite3.Connection] = None
    t_all = _time.perf_counter()
    t_connect = t_execute = t_fetch = 0.0
    rows_fetched = 0
    cancelled = False
    deadline = t_all + (
        float(deadline_sec) if deadline_sec and deadline_sec > 0
        else _ranking_read_deadline_sec()
    )
    max_rows = _ranking_read_max_rows()
    chunk = _ranking_read_chunk_rows()

    try:
        t0 = _time.perf_counter()
        conn = sqlite3.connect(
            db_path,
            timeout=30.0,
            isolation_level=None,
        )
        t_connect = _time.perf_counter() - t0

        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA query_only=ON")
        except Exception:
            logger.debug("[RANKING SUMMARY RUNNER] pragma skipped", exc_info=True)

        # クエリ実行中でも中断できるようにする。
        # 非 0 を返すと SQLite が OperationalError('interrupted') を上げる。
        def _progress() -> int:
            if _time.perf_counter() > deadline:
                return 1
            if cooperative_stop and _stage_should_stop("ranking_db_read"):
                return 1
            return 0

        try:
            conn.set_progress_handler(_progress, _RANKING_PROGRESS_OPS)
        except Exception:
            logger.debug("[RANKING SUMMARY RUNNER] progress handler unavailable", exc_info=True)

        t0 = _time.perf_counter()
        cur = conn.execute(sql, params or {})
        t_execute = _time.perf_counter() - t0

        cols = [d[0] for d in (cur.description or [])]

        # fetchall をやめ、chunk 境界ごとに cancel / deadline / 上限を見る。
        t0 = _time.perf_counter()
        buf: list = []
        while True:
            part = cur.fetchmany(chunk)
            if not part:
                break
            buf.extend(part)
            rows_fetched = len(buf)
            if _time.perf_counter() > deadline:
                cancelled = True
                break
            if _stage_should_stop("ranking_db_read_fetch"):
                cancelled = True
                break
            if max_rows > 0 and rows_fetched >= max_rows:
                cancelled = True
                logger.warning(
                    "[RANKING DB READ STAGE] 行数上限で打ち切り rows=%d max_rows=%d "
                    "(RANKING_DB_READ_MAX_ROWS で調整可)",
                    rows_fetched, max_rows,
                )
                break
        t_fetch = _time.perf_counter() - t0

        try:
            cur.close()
        except Exception:
            logger.debug("[RANKING SUMMARY RUNNER] cursor close failed", exc_info=True)

        out = pd.DataFrame(buf, columns=cols) if cols else pd.DataFrame(buf)
        return out

    except sqlite3.OperationalError as e:
        if "interrupt" in str(e).lower():
            cancelled = True
            logger.warning(
                "[RANKING DB READ STAGE] progress handler により中断 path=%s elapsed=%.3fs "
                "(cancel / deadline。timeout は伸ばさない)",
                db_path, _time.perf_counter() - t_all,
            )
            return pd.DataFrame()
        logger.exception(
            "[RANKING SUMMARY RUNNER] sqlite read failed path=%s",
            db_path,
        )
        return pd.DataFrame()
    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] sqlite read failed path=%s",
            db_path,
        )
        return pd.DataFrame()

    finally:
        if conn is not None:
            try:
                conn.set_progress_handler(None, 0)
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                logger.debug("[RANKING SUMMARY RUNNER] close failed", exc_info=True)
        total = _time.perf_counter() - t_all
        log = logger.warning if (total >= 1.0 or cancelled or t_fetch >= 1.0) else logger.info
        try:
            log(
                "[RANKING DB READ STAGE] table=%s sql_type=%s rows_fetched=%s "
                "connect_sec=%.3f execute_sec=%.3f fetch_sec=%.3f total_sec=%.3f "
                "cancelled=%s deadline_remaining=%.3f chunk=%s max_rows=%s db=%s",
                RANKING_SNAPSHOT_TABLE,
                "SELECT",
                rows_fetched,
                t_connect, t_execute, t_fetch, total,
                cancelled, deadline - _time.perf_counter(), chunk, max_rows,
                os.path.basename(str(db_path)),
            )
        except Exception:
            pass


def read_via_sqlalchemy_engine(
    *,
    sql: str,
    params: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    try:
        from sqlalchemy import text
        from database.session import get_ranking_engine

        engine = get_ranking_engine()
        if engine is None:
            logger.warning("[RANKING SUMMARY RUNNER] get_ranking_engine returned None")
            return pd.DataFrame()

        with engine.connect() as conn:
            return pd.read_sql_query(text(sql), conn, params=params or {})

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] sqlalchemy read failed"
        )
        return pd.DataFrame()


def table_exists_sqlite(db_path: str, table_name: str) -> bool:
    if not path_exists(db_path):
        return False

    conn: Optional[sqlite3.Connection] = None

    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cur.fetchone() is not None

    except Exception:
        return False

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_sqlite_columns(
    *,
    db_path: str,
    table_name: str,
) -> list[str]:
    if not path_exists(db_path):
        return []

    conn: Optional[sqlite3.Connection] = None

    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        cur = conn.execute(f'PRAGMA table_info("{table_name}")')
        rows = cur.fetchall()
        return [str(r[1]) for r in rows if len(r) >= 2]

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RUNNER] failed to get table columns db=%s table=%s",
            db_path,
            table_name,
        )
        return []

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def resolve_sql_order_column(
    *,
    db_path: str,
    table_name: str,
) -> Optional[str]:
    cols = get_sqlite_columns(
        db_path=db_path,
        table_name=table_name,
    )

    if not cols:
        return None

    for c in DATETIME_CANDIDATES:
        if c in cols:
            return c

    return None


def _sql_lookback_enabled() -> bool:
    import os
    v = os.getenv("RANKING_SNAPSHOT_SQL_LOOKBACK")
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _sql_lookback_cutoff(db_path: str, table_name: str, order_col: str,
                         where: list, params: dict, lookback_minutes: int):
    """SQL 側で使う下限時刻を求める。

    pandas 側 (normalize.py) は `latest_dt - lookback` で絞る。
    latest_dt は **データの最大値** なので、ここでも MAX を取って合わせる。
    symbol 条件があれば MAX にも同じ条件を掛ける (latest_dt を一致させるため)。
    """
    try:
        lb = int(lookback_minutes or 0)
        if lb <= 0:
            return None
        sql = f'SELECT MAX("{order_col}") FROM {table_name}'
        if where:
            sql += "\nWHERE " + " AND ".join(where)
        conn = None
        try:
            conn = sqlite3.connect(db_path, timeout=10.0)
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            cur = conn.execute(sql, params or {})
            row = cur.fetchone()
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.debug("[RANKING SUMMARY RUNNER] max dt conn close failed", exc_info=True)
        if not row or row[0] is None:
            return None
        latest = pd.to_datetime(row[0], errors="coerce")
        if latest is None or pd.isna(latest):
            return None
        start = latest - pd.Timedelta(minutes=lb)
        return start.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # 下限が求まらなければ従来どおり全件読みに落とす (結果は変わらない)。
        logger.debug("[RANKING SUMMARY RUNNER] sql lookback cutoff failed", exc_info=True)
        return None


# ============================================================
# P0-10 / P0-11: 1 回の BG 実行で同じ snapshot を何度も読まない
# ------------------------------------------------------------
# run_ranking_summaries_all() は 1m / 3m / 5m を順に回し、
# そのたびに build_ranking_summary_base_df() ->
# load_ranking_snapshot_1min() が **同じ 20,000 行超を NAS から
# 読み直していた**。
#
# 2026-08-28 の実測:
#   interval=1 base_df=14.040s rows=20693
#   interval=1 base_df=20.535s rows=21764  (次 generation)
#   interval=3 base_df=55.065s rows=7953
#   interval=5 budget exhausted before base_df (着手すらできない)
#   ranking_job_all = 151.4s / 155.8s
#
# 同一 (trade_date, lookback, symbols, db_path) の読み込みは
# 短い TTL の間だけ共有する。DB は永続保存用であって、
# 1 回の tick の中で 3 回読む必要はない。
# 判定・閾値・返す内容は一切変えない (同じ DataFrame)。
#
# RANKING_SNAPSHOT_CACHE_TTL_SEC=0 で従来動作。
# ============================================================

_snapshot_cache_lock = threading.Lock()
_snapshot_cache: dict = {}
_snapshot_cache_hits = 0
_snapshot_cache_misses = 0


def _snapshot_cache_ttl() -> float:
    try:
        v = os.getenv("RANKING_SNAPSHOT_CACHE_TTL_SEC")
        if v is None or str(v).strip() == "":
            return 45.0
        return max(0.0, float(str(v).strip()))
    except Exception:
        return 45.0


def ranking_snapshot_cache_stats() -> dict:
    with _snapshot_cache_lock:
        total = _snapshot_cache_hits + _snapshot_cache_misses
        return {
            "hits": _snapshot_cache_hits,
            "misses": _snapshot_cache_misses,
            "entries": len(_snapshot_cache),
            "hit_rate": (_snapshot_cache_hits / total) if total else None,
            "ttl_sec": _snapshot_cache_ttl(),
        }


def reset_ranking_snapshot_cache() -> None:
    global _snapshot_cache_hits, _snapshot_cache_misses
    with _snapshot_cache_lock:
        _snapshot_cache.clear()
        _snapshot_cache_hits = 0
        _snapshot_cache_misses = 0


def load_ranking_snapshot_1min(
    *,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    lookback_minutes: int = 240,
    symbols: Optional[Iterable[str]] = None,
    ranking_db_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    ranking_snapshot_1min からランキング由来 summary の元データを読む。
    """
    global _snapshot_cache_hits, _snapshot_cache_misses

    d = normalize_trade_date(trade_date)
    db_path = ranking_db_path or default_ranking_db_path(d)

    symbol_list = normalize_symbols(symbols)

    ttl = _snapshot_cache_ttl()
    cache_key = (str(d), int(lookback_minutes), str(db_path),
                 tuple(symbol_list) if symbol_list else None)
    if ttl > 0.0:
        now_m = _time.monotonic()
        with _snapshot_cache_lock:
            hit = _snapshot_cache.get(cache_key)
            if hit is not None and (now_m - hit[0]) < ttl:
                _snapshot_cache_hits += 1
                cached_df, age = hit[1], now_m - hit[0]
            else:
                cached_df, age = None, None
                if hit is not None:
                    _snapshot_cache.pop(cache_key, None)
        if cached_df is not None:
            logger.warning(
                "[RANKING SNAPSHOT CACHE] hit rows=%s age=%.1fs ttl=%.0fs "
                "lookback=%s db=%s (1 tick 内で ranking DB を読み直さない)",
                len(cached_df), age, ttl, lookback_minutes, os.path.basename(str(db_path)),
            )
            return cached_df
        with _snapshot_cache_lock:
            _snapshot_cache_misses += 1

    where: list[str] = []
    params: dict[str, Any] = {}

    sql = f"""
        SELECT *
        FROM {RANKING_SNAPSHOT_TABLE}
    """

    if symbol_list:
        placeholders = []

        for i, sym in enumerate(symbol_list):
            key = f"sym_{i}"
            placeholders.append(f":{key}")
            params[key] = sym

        where.append(f"CAST(symbol AS TEXT) IN ({','.join(placeholders)})")

    if where:
        sql += "\nWHERE " + " AND ".join(where)

    raw = pd.DataFrame()

    if ranking_db_path or path_exists(db_path):
        if not table_exists_sqlite(db_path, RANKING_SNAPSHOT_TABLE):
            logger.warning(
                "[RANKING SUMMARY RUNNER] table not found db=%s table=%s",
                db_path,
                RANKING_SNAPSHOT_TABLE,
            )
            return pd.DataFrame()

        order_col = resolve_sql_order_column(
            db_path=db_path,
            table_name=RANKING_SNAPSHOT_TABLE,
        )

        sql_with_order = sql

        # 全件読み + 全件ソートをやめ、SQL 側で lookback を絞る。
        # ranking_snapshot_1min は場中ずっと増えるので、全件読みは時間とともに悪化する。
        # pandas 側の絞り込み (latest - lookback) と等価になるよう MAX から下限を作る。
        if order_col and _sql_lookback_enabled():
            _cut = _sql_lookback_cutoff(
                db_path, RANKING_SNAPSHOT_TABLE, order_col, where, params, lookback_minutes,
            )
            if _cut:
                sql_with_order += ("\nAND " if where else "\nWHERE ") + f'"{order_col}" >= :dt_from'
                params = dict(params)
                params["dt_from"] = _cut
                logger.info(
                    "[RANKING SUMMARY RUNNER] sql lookback applied col=%s from=%s lookback=%s",
                    order_col, _cut, lookback_minutes,
                )

        # ------------------------------------------------------------
        # 打ち切りは「新しい側」を残す。
        #
        # ranking_snapshot_1min は場中 82,000〜84,000 行まで増えるのに対し
        # RANKING_DB_READ_MAX_ROWS は既定 60,000。
        # ORDER BY ... ASC + LIMIT だと **古い側 60,000 行** を残して
        # 直近 20,000 行超を黙って捨てることになり、
        # ranking summary が古い bar で計算されてしまう。
        # 上限で切る可能性がある場合は DESC で取り、後段で昇順へ戻す。
        # (bootstrap_loader と同じ方針)
        # ------------------------------------------------------------
        _lim = _ranking_read_max_rows()
        _desc_tail = bool(order_col) and _lim > 0
        if order_col:
            sql_with_order += f'\nORDER BY "{order_col}" {"DESC" if _desc_tail else "ASC"}'
        else:
            logger.warning(
                "[RANKING SUMMARY RUNNER] no sql order column found table=%s db=%s",
                RANKING_SNAPSHOT_TABLE,
                db_path,
            )

        # SQL 側にも上限を持たせる。
        # ranking_snapshot_1min は場中ずっと増え、2026-08-28 実測で
        # lookback=240 でも 82,000〜84,000 行になっていた。
        # 全件 fetchall が cancel 不能な 1117 秒ブロックの原因だったので、
        # SQL の時点で行数を切る。indicator に必要なのは
        # 銘柄あたり最大 105 bars 程度で、この上限はそれを十分に上回る。
        if _lim > 0 and "LIMIT" not in sql_with_order.upper():
            sql_with_order += f"\nLIMIT {int(_lim)}"

        # ------------------------------------------------------------
        # R16-2: incremental bounded cache
        #
        # ここまでの sql_with_order は「毎回 80,000 行級を NAS から読む」形。
        # warm state があるなら watermark より新しい行だけ読み、
        # 銘柄あたり max_bars 本の ring buffer へ merge して返す。
        # 使えない場合のみ従来どおり full read する。
        # 判定条件・閾値・freshness は一切変えない。
        # ------------------------------------------------------------
        _inc_df = _inc.try_delta(
            key=cache_key,
            db_path=db_path,
            order_col=order_col,
            base_sql=sql,
            has_where=bool(where),
            params=params,
            reader=read_sqlite_table,
            normalizer=normalize_ranking_snapshot_df,
        )
        if _inc_df is not None and not _inc_df.empty:
            _d2 = filter_trade_date_if_possible(_inc_df, trade_date=d)
            _d2 = filter_symbols(_d2, symbol_list, fallback_if_empty=True)
            _d2 = filter_lookback(_d2, lookback_minutes=lookback_minutes)
            _out = _d2.reset_index(drop=True)
            if ttl > 0.0:
                with _snapshot_cache_lock:
                    _snapshot_cache[cache_key] = (_time.monotonic(), _out)
            return _out

        raw = read_sqlite_table(
            db_path=db_path,
            sql=sql_with_order,
            params=params,
        )

        # DESC で取った場合は昇順へ戻す (後段は時系列昇順を前提にしている)。
        if _desc_tail and raw is not None and not raw.empty and order_col in raw.columns:
            raw = raw.sort_values(order_col, kind="stable").reset_index(drop=True)
            logger.info(
                "[RANKING SUMMARY RUNNER] desc-tail read rows=%d -> 昇順へ整列 "
                "(上限で切られても直近側を残す)",
                len(raw),
            )

        if raw.empty and symbol_list:
            logger.warning(
                "[RANKING SUMMARY RUNNER] SQL symbol filter returned empty -> retry without symbol filter db=%s symbols=%s",
                db_path,
                len(symbol_list),
            )

            sql_retry = f"""
                SELECT *
                FROM {RANKING_SNAPSHOT_TABLE}
            """

            if order_col:
                sql_retry += f'\nORDER BY "{order_col}" ASC'

            raw = read_sqlite_table(
                db_path=db_path,
                sql=sql_retry,
                params={},
            )

    else:
        raw = read_via_sqlalchemy_engine(
            sql=sql,
            params=params,
        )

        if raw.empty and symbol_list:
            logger.warning(
                "[RANKING SUMMARY RUNNER] SQLAlchemy symbol filter returned empty -> retry without symbol filter"
            )
            raw = read_via_sqlalchemy_engine(
                sql=f"""
                    SELECT *
                    FROM {RANKING_SNAPSHOT_TABLE}
                """,
                params={},
            )

    if raw is None or raw.empty:
        logger.warning(
            "[RANKING SUMMARY RUNNER] ranking snapshot empty trade_date=%s db=%s",
            d,
            db_path,
        )
        return pd.DataFrame()

    logger.info(
        "[RANKING SUMMARY RUNNER] snapshot loaded rows=%s cols=%s db=%s",
        len(raw),
        list(raw.columns),
        db_path,
    )

    df = normalize_ranking_snapshot_df(raw)
    # full read の結果を incremental state として保持し、
    # 次回以降は delta read だけで済ませる。
    df = _inc.store_full(cache_key, df)

    if df.empty:
        logger.warning(
            "[RANKING SUMMARY RUNNER] snapshot normalized empty rows_raw=%s cols=%s",
            len(raw),
            list(raw.columns),
        )
        return df

    df = filter_trade_date_if_possible(df, trade_date=d)
    df = filter_symbols(df, symbol_list, fallback_if_empty=True)
    df = filter_lookback(df, lookback_minutes=lookback_minutes)

    if df.empty:
        logger.warning(
            "[RANKING SUMMARY RUNNER] snapshot prepared empty after filters "
            "trade_date=%s lookback=%s symbols=%s",
            d,
            lookback_minutes,
            len(symbol_list) if symbol_list else 0,
        )
        return df

    logger.info(
        "[RANKING SUMMARY RUNNER] snapshot prepared rows=%s symbols=%s "
        "dt_min=%s dt_max=%s close_nonnull=%s close_gt0=%s types=%s",
        len(df),
        df["symbol"].nunique() if "symbol" in df.columns and not df.empty else 0,
        df["datetime"].min() if "datetime" in df.columns and not df.empty else None,
        df["datetime"].max() if "datetime" in df.columns and not df.empty else None,
        int(df["close"].notna().sum()) if "close" in df.columns else 0,
        int((pd.to_numeric(df["close"], errors="coerce") > 0).sum()) if "close" in df.columns else 0,
        df["ranking_type"].value_counts().head(10).to_dict() if "ranking_type" in df.columns else {},
    )

    out = df.reset_index(drop=True)
    if ttl > 0.0:
        with _snapshot_cache_lock:
            _snapshot_cache[cache_key] = (_time.monotonic(), out)
            # 1 日ぶんの key しか増えないが、念のため上限を持たせる。
            while len(_snapshot_cache) > 16:
                oldest = min(_snapshot_cache, key=lambda k: _snapshot_cache[k][0])
                _snapshot_cache.pop(oldest, None)
    return out