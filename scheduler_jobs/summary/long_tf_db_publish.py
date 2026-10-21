# -*- coding: utf-8 -*-
# ============================================================
# File   : scheduler_jobs/summary/long_tf_db_publish.py
# Version: V1-MAIN-LONG-TF-FROM-SUMMARY-DB
# ------------------------------------------------------------
# main.py で 3分足 / 5分足 merged summary が rows=0 のままになる問題への対処。
#
# 実測 (2026-08-26 main.py console ログ):
#   [SUMMARY PARALLEL RESET] tick start push_targets=[1, 3]
#       wait_push_targets=[1] bg_push_targets=[]
#   [SUMMARY PARALLEL] main wait limited to 1m; long push targets not waited
#       targets=[1, 3] wait=[1] skipped_long=[3]
#   -> 3 は push_targets に入っているのに wait にも bg にも入らず、
#      「投げられないまま黙って捨てられて」いた。
#   その結果 [MERGED SET] tf=3 / tf=5 は 08:47 の rows=0 以降一度も更新されず、
#   [MERGED GET] tf=3 rows=0 / tf=5 rows=0 が続いていた。
#
# 一方 main_database 側 (summary_database_runner) は 3分/5分を計算して
#   summaryYYYYMMDD.db の stock_summary_3min / stock_summary_5min
# へ保存できている (2026-08-26 09:39 時点 3min=1938行 / 5min=1700行、
# score / rsi / macd も入っている)。
#
# したがって main.py は「自分で計算し直す」のではなく
# 「データ側が書いたものを読む」のが正しい。
# これは main.py = 取引 / main_database = データ格納 という方針とも一致し、
# main.py へ NAS 重処理を戻すことにもならない (読むのは数百行)。
#
# 設計:
#   - 1分 tick を絶対にブロックしない (別スレッド + 単一実行ガード)
#   - 同じ足スロットを二度読まない (throttle)
#   - 未来足 / 前日足は採用しない (既存 stale ガードと同じ考え方)
#   - 例外は握り潰さず logger.exception で残す
#   - env で無効化できる (SUMMARY_LONG_TF_DB_PUBLISH=0)
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Optional

import pandas as pd
from nas_root import nas_path, nas_root

logger = logging.getLogger(__name__)

VERSION = "V1-MAIN-LONG-TF-FROM-SUMMARY-DB"

_LOCK = threading.Lock()
_RUNNING = False
_LAST_SLOT: dict[int, str] = {}


# ------------------------------------------------------------
# env helpers
# ------------------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return int(float(str(v).strip()))
    except Exception:
        pass
    return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return float(str(v).strip())
    except Exception:
        pass
    return float(default)


def enabled() -> bool:
    return _env_bool("SUMMARY_LONG_TF_DB_PUBLISH", True)


def _summary_db_path() -> str:
    base = os.getenv(
        "AUTOSTOCK_SUMMARY_DB_DIR",
        nas_path(r"raw_data\kabu_station\summary"),
    )
    return os.path.join(base, "summary%s.db" % dt.datetime.now().strftime("%Y%m%d"))


def _now_naive() -> dt.datetime:
    return dt.datetime.now().replace(tzinfo=None)


def _future_tolerance_sec(interval: int) -> float:
    return max(5.0, _env_float("SUMMARY_LONG_TF_DB_FUTURE_TOLERANCE_SEC", 5.0))


def _max_age_sec(interval: int) -> float:
    """この秒数より古い足しか無ければ publish しない (stale を新鮮扱いしない)。"""
    default = max(120.0, float(interval) * 60.0 * 4.0)
    return max(60.0, _env_float("SUMMARY_LONG_TF_DB_MAX_AGE_SEC", default))


# ------------------------------------------------------------
# db read
# ------------------------------------------------------------
def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _read_interval(interval: int) -> tuple[pd.DataFrame, str]:
    """summaryYYYYMMDD.db の stock_summary_{interval}min から直近スロットを読む。

    Returns (df, note)。df が空のときは note に理由が入る。
    """
    interval = int(interval)
    table = "stock_summary_%dmin" % interval
    db = _summary_db_path()
    if not os.path.exists(db):
        return pd.DataFrame(), "db_missing:%s" % db

    today = _now_naive().date().isoformat()
    now = _now_naive()
    tol = _future_tolerance_sec(interval)
    max_allowed = (now + dt.timedelta(seconds=tol)).strftime("%Y-%m-%d %H:%M:%S")
    lookback_min = max(interval * 2, _env_int("SUMMARY_LONG_TF_DB_LOOKBACK_MIN", interval * 4))
    max_rows = max(100, _env_int("SUMMARY_LONG_TF_DB_MAX_ROWS", 4000))

    # P0-9: date(col)=? は列に関数を掛けるので index が効かない。
    # 同じ「当日」を index の効く範囲述語で書き直す。
    # 実測 (summary20260828.db):
    #   rows  date(datetime)=?      mean 349.0ms
    #   rows  範囲述語              mean  73.4ms   (4.8 倍)
    day_start = "%s 00:00:00" % today
    day_end = "%s 00:00:00" % (_now_naive().date() + dt.timedelta(days=1)).isoformat()

    t_all = time.perf_counter()
    t_connect = t_schema = t_max = t_rows = 0.0
    conn = None
    try:
        t0 = time.perf_counter()
        # 旧実装は `with sqlite3.connect(...) as conn:` で close していなかった。
        # `with sqlite3.Connection` は commit/rollback するだけで閉じない
        # (Python 公式仕様)。NAS 上の DB では SMB のオープンが積み上がる。
        conn = sqlite3.connect(db, timeout=float(_env_float("SUMMARY_LONG_TF_DB_TIMEOUT_SEC", 8.0)))
        t_connect = time.perf_counter() - t0
        try:
            conn.execute("PRAGMA busy_timeout=8000")
            conn.execute("PRAGMA query_only=ON")
        except Exception:
            logger.debug("[LONG TF DB PUBLISH] pragma skipped", exc_info=True)
        conn.row_factory = sqlite3.Row

        t0 = time.perf_counter()
        if not _table_exists(conn, table):
            return pd.DataFrame(), "table_missing:%s" % table

        cols = {str(r[1]) for r in conn.execute('PRAGMA table_info("%s")' % table).fetchall()}
        t_schema = time.perf_counter() - t0
        dt_col = "datetime" if "datetime" in cols else ("time" if "time" in cols else None)
        if dt_col is None:
            return pd.DataFrame(), "datetime_col_missing"

        t0 = time.perf_counter()
        latest = conn.execute(
            'SELECT MAX("%s") FROM "%s" WHERE "%s">=? AND "%s"<? AND "%s"<=?'
            % (dt_col, table, dt_col, dt_col, dt_col),
            (day_start, day_end, max_allowed),
        ).fetchone()[0]
        t_max = time.perf_counter() - t0
        if not latest:
            return pd.DataFrame(), "no_slot_today"

        latest_ts = pd.to_datetime(latest, errors="coerce")
        if pd.isna(latest_ts):
            return pd.DataFrame(), "latest_unparsable:%s" % latest
        latest_py = pd.Timestamp(latest_ts).to_pydatetime().replace(tzinfo=None)
        age = (now - latest_py).total_seconds()
        if age > _max_age_sec(interval):
            # stale を新鮮なものとして publish しない (ガードは緩めない)
            return pd.DataFrame(), "stale:age=%.1fs>max=%.1fs latest=%s" % (
                age, _max_age_sec(interval), latest,
            )

        start = (latest_py - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")
        t0 = time.perf_counter()
        rows = conn.execute(
            'SELECT * FROM "%s" WHERE "%s">=? AND "%s"<? AND "%s"<=? '
            'ORDER BY "%s" DESC LIMIT ?'
            % (table, dt_col, dt_col, dt_col, dt_col),
            (start, day_end, max_allowed, max_rows),
        ).fetchall()
        t_rows = time.perf_counter() - t0
        if not rows:
            return pd.DataFrame(), "no_rows_in_lookback"
        df = pd.DataFrame([dict(r) for r in rows])
    except Exception:
        logger.exception("[LONG TF DB PUBLISH] read failed interval=%s db=%s", interval, db)
        return pd.DataFrame(), "read_exception"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.debug("[LONG TF DB PUBLISH] close failed", exc_info=True)
        total = time.perf_counter() - t_all
        log = logger.warning if total >= 2.0 else logger.info
        log(
            "[LONG TF DB READ] interval=%s connect=%.3fs schema=%.3fs max_slot=%.3fs "
            "rows_query=%.3fs total=%.3fs db=%s",
            interval, t_connect, t_schema, t_max, t_rows, total, os.path.basename(db),
        )

    if dt_col != "datetime":
        df["datetime"] = df[dt_col]
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    if "symbol" not in df.columns:
        return pd.DataFrame(), "symbol_col_missing"
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df = df[df["symbol"].ne("")]
    if df.empty:
        return pd.DataFrame(), "empty_after_normalize"

    # 未来足は採用しない
    limit_ts = pd.Timestamp(now + dt.timedelta(seconds=tol))
    before = len(df)
    df = df[df["datetime"] <= limit_ts]
    if len(df) != before:
        logger.warning(
            "[LONG TF DB PUBLISH] future rows dropped interval=%s %d->%d limit=%s",
            interval, before, len(df), limit_ts,
        )
    if df.empty:
        return pd.DataFrame(), "all_future"

    df = df.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
    return df, ""


def _latest_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    return (
        df.sort_values(["symbol", "datetime"], kind="stable")
        .groupby("symbol", as_index=False)
        .tail(1)
        .reset_index(drop=True)
    )


# ------------------------------------------------------------
# publish
# ------------------------------------------------------------
def _publish(interval: int, hist: pd.DataFrame, latest: pd.DataFrame) -> bool:
    ok = False
    try:
        from trading.summary.controller_cache import (
            safe_global_set_history,
            safe_global_set_latest,
            safe_global_set_merged_summary,
        )
    except Exception:
        logger.exception("[LONG TF DB PUBLISH] controller_cache import failed interval=%s", interval)
        return False

    try:
        safe_global_set_history(int(interval), hist)
        safe_global_set_latest(int(interval), latest)
        safe_global_set_merged_summary(int(interval), latest)
        ok = True
    except Exception:
        logger.exception("[LONG TF DB PUBLISH] global publish failed interval=%s", interval)
        return False

    # GlobalContext 側にも明示的に入れる (merged_get が参照するのはこちら)
    try:
        from core.global_context.context import global_context as GC

        for name, payload in (
            ("set_summary_history", hist),
            ("set_merged_summary", latest),
        ):
            fn = getattr(GC, name, None)
            if not callable(fn):
                continue
            try:
                fn(int(interval), payload.copy(), source="push")
            except TypeError:
                fn(int(interval), payload.copy())
    except Exception:
        logger.exception("[LONG TF DB PUBLISH] global_context publish failed interval=%s", interval)

    return ok


def publish_long_tf_from_db(intervals: tuple[int, ...] = (3, 5), *, reason: str = "") -> dict[int, int]:
    """3分/5分の merged summary を summary DB から publish する。

    main.py が自分で長足を計算しないぶんをここで埋める。
    戻り値は {interval: published_rows}。
    """
    out: dict[int, int] = {}
    if not enabled():
        logger.info("[LONG TF DB PUBLISH] disabled by SUMMARY_LONG_TF_DB_PUBLISH=0")
        return out

    for iv in intervals:
        iv = int(iv)
        if iv not in (3, 5):
            continue
        t0 = time.perf_counter()
        df, note = _read_interval(iv)
        if df.empty:
            logger.warning(
                "[LONG TF DB PUBLISH] interval=%s no publish reason=%s elapsed=%.3fs reason_ctx=%s version=%s",
                iv, note or "empty", time.perf_counter() - t0, reason, VERSION,
            )
            out[iv] = 0
            continue

        latest = _latest_per_symbol(df)
        latest_dt = df["datetime"].max()
        slot_key = str(latest_dt)
        if _LAST_SLOT.get(iv) == slot_key:
            logger.info(
                "[LONG TF DB PUBLISH] interval=%s slot unchanged latest=%s -> skip (elapsed=%.3fs)",
                iv, slot_key, time.perf_counter() - t0,
            )
            out[iv] = 0
            continue

        if _publish(iv, df, latest):
            _LAST_SLOT[iv] = slot_key
            out[iv] = int(len(latest))
            age = (_now_naive() - pd.Timestamp(latest_dt).to_pydatetime().replace(tzinfo=None)).total_seconds()
            logger.warning(
                "[LONG TF DB PUBLISH] interval=%s published hist_rows=%s latest_rows=%s symbols=%s "
                "latest_dt=%s age=%.1fs elapsed=%.3fs reason=%s version=%s",
                iv, len(df), len(latest), int(latest["symbol"].nunique()),
                latest_dt, age, time.perf_counter() - t0, reason, VERSION,
            )
        else:
            out[iv] = 0

    return out


def _worker(intervals: tuple[int, ...], reason: str) -> None:
    global _RUNNING
    try:
        publish_long_tf_from_db(intervals, reason=reason)
    except Exception:
        logger.exception("[LONG TF DB PUBLISH] worker failed intervals=%s", intervals)
    finally:
        with _LOCK:
            _RUNNING = False


def start_long_tf_db_publish_async(intervals: tuple[int, ...] = (3, 5), *, reason: str = "") -> bool:
    """1分 tick をブロックしないよう、別スレッドで publish する。

    多重起動はしない (前回がまだ走っていたらスキップする)。
    """
    global _RUNNING
    if not enabled():
        return False
    ivs = tuple(int(x) for x in intervals if int(x) in (3, 5))
    if not ivs:
        return False
    with _LOCK:
        if _RUNNING:
            logger.info("[LONG TF DB PUBLISH] previous publish still running -> skip intervals=%s", ivs)
            return False
        _RUNNING = True
    try:
        th = threading.Thread(
            target=_worker,
            args=(ivs, reason),
            name="long-tf-db-publish",
            daemon=True,
        )
        th.start()
        return True
    except Exception:
        with _LOCK:
            _RUNNING = False
        logger.exception("[LONG TF DB PUBLISH] thread start failed intervals=%s", ivs)
        return False


__all__ = [
    "VERSION",
    "enabled",
    "publish_long_tf_from_db",
    "start_long_tf_db_publish_async",
]
