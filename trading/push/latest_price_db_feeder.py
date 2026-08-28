# ============================================================
# File   : trading/push/latest_price_db_feeder.py
# Version: Ver1.1-MAIN-LATEST-PRICE-FROM-PUSH-DB-ROWID-INCREMENTAL
# ------------------------------------------------------------
# main.py 用: PUSH DB から latest_price_cache を供給する。
#
# 背景:
#   kabu Station の PUSH WebSocket は 1 セッション 1 接続しか許容しない。
#   main.py と push_receiver_runner.py が同時に接続すると互いを切断し合い、
#   再接続ループになる (2026-08-25 実測: 両者が分単位で交互にフラップし、
#   main.py 側だけで 13 分間に DISCONNECTED 190 回、
#   CONNECTED -> DISCONNECTED の中央値 2.0 秒)。
#
#   そのため PUSH WebSocket の所有者は push_receiver_runner.py に一本化した。
#   main.py は push_receiver が書いた PUSH DB から最新値を読む。
#
#   latest_price_cache は元々 ws_callbacks.on_message からしか更新されておらず、
#   main.py が WS を張らなくなると供給源が消えてしまうため、この経路が要る。
#
# NAS 負荷について (実測 2026-08-25, push20260825.db: 66 列 12,636 行):
#   stream_data_raw には datetime 単独の index が無い
#   (index は (symbol, datetime) と received_at のみ)。
#   そのため ORDER BY datetime DESC は全件ソートになり非常に重い。
#
#     SELECT * ORDER BY datetime DESC LIMIT 5000      14.4s
#     5 列    ORDER BY datetime DESC LIMIT 5000       10.8s
#     5 列    ORDER BY rowid    DESC LIMIT 800         0.65s
#     5 列    WHERE rowid > ?   ORDER BY rowid DESC    0.61s
#
#   よって「必要な列だけ」を「rowid 昇順の増分」で読む。
#   起動時だけ prime 件数ぶん遡り、以後は前回の rowid より新しい行だけ読む。
#   main.py へ NAS 重処理を戻さないための措置。
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "Ver1.1-MAIN-LATEST-PRICE-FROM-PUSH-DB-ROWID-INCREMENTAL"

_THREAD: threading.Thread | None = None
_LOCK = threading.Lock()
_STOP = threading.Event()

_LAST_ROWID: int = 0

_TABLES_PREFER = ("stream_data_raw", "stream_data", "push_stream", "push_ticks", "ticks")
_SYMBOL_COLS = ("symbol", "code", "Symbol")
_PRICE_COLS = ("price", "current_price", "CurrentPrice", "Price", "close")
_NAME_COLS = ("symbolname", "symbol_name", "SymbolName", "name")
_VOLUME_COLS = ("volume", "Volume", "trading_volume")
_DT_COLS = ("datetime", "received_at", "created_at", "updated_at", "time")


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env_float(name, float(default))))
    except Exception:
        return int(default)


def _db_path() -> str:
    """PUSH DB のパス。既存 summary 側と同じ解決を使う。"""
    from scheduler_jobs.summary.memory_latest_1m import _push_db_path

    return str(_push_db_path())


def _connect(path: str, timeout: float) -> sqlite3.Connection:
    # reader なので read-only URI で開く。
    # 素の sqlite3.connect(path) は **パスが無ければ空 DB を作る**ため、
    # NAS が見えないときに 0 テーブルの DB を作ってしまう事故になる。
    import os as _os
    if not _os.path.exists(path):
        raise FileNotFoundError(
            "[MAIN PRICE DB FEED] push db が存在しない (reader は作成しない): %s" % path)
    # 空 DB を作らない保証は上の os.path.exists() で得ている。
    # mode=ro は活発な WAL DB では遅い (実測 5.496s vs query_only 3.830s)。
    # この feeder は 250MB の push db を 2 秒ごとに読むので差が効く。
    conn = sqlite3.connect(path, timeout=timeout)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
    except Exception:
        logger.debug("[MAIN PRICE DB FEED] pragma failed", exc_info=True)
    return conn


# ============================================================
# P0-4 / P0-10: read session を共有する
# ------------------------------------------------------------
# 2026-08-28 の実測では、増分読みで 1〜34 行しか取っていないのに
# read= が 2.5〜13.9 秒かかっていた。行数と無関係なので
# 中身を分解したところ (NAS がアイドルの状態でも):
#
#   exists              17.4 ms
#   connect             16.0 ms
#   pragma               0.0 ms
#   sqlite_master      521.5 ms   <- 毎回スキーマを探し直していた
#   table_info           3.4 ms
#   incremental_query   93.5 ms   <- 実際に必要な仕事はここだけ
#   close              495.1 ms   <- 100MB の WAL DB を NAS 越しに閉じる
#   ------------------------------
#   TOTAL             1146.9 ms
#
# つまり 88% が open / schema 探索 / close で、
# 実際のクエリは 8% しかなかった。
#
# 対策を実測で比較した結果:
#   現行 (毎回 open+schema+close)        548.5 ms
#   A: schema キャッシュのみ             686.1 ms   (効かない)
#   B: schema キャッシュ + 接続再利用      24.1 ms   (22.8 倍)
#
# よって **読み取り接続を 1 本だけ保持して使い回す**。
#   - query_only=ON なので書かない
#   - autocommit なので WAL を pin し続けない
#   - 一定時間で作り直す (長時間保持を避ける)
#   - 例外時は必ず閉じて次回作り直す
#   - 接続は常に 1 本なので、前ラウンドで潰した「接続リーク」には
#     ならない (ハンドルは定数)
# ============================================================

_conn_lock = threading.Lock()
_shared_conn: sqlite3.Connection | None = None
_shared_conn_path: str = ""
_shared_conn_at: float = 0.0
_schema_cache: dict = {}
_conn_opened = 0
_conn_closed = 0


def _conn_max_age_sec() -> float:
    return max(30.0, _env_float("MAIN_PUSH_PRICE_DB_FEED_CONN_MAX_AGE_SEC", 300.0))


def _reuse_conn_enabled() -> bool:
    v = os.getenv("MAIN_PUSH_PRICE_DB_FEED_REUSE_CONN")
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _close_shared_conn_locked() -> None:
    global _shared_conn, _shared_conn_path, _shared_conn_at, _conn_closed
    if _shared_conn is not None:
        try:
            _shared_conn.close()
            _conn_closed += 1
        except Exception:
            logger.debug("[MAIN PRICE DB FEED] shared conn close failed", exc_info=True)
    _shared_conn = None
    _shared_conn_path = ""
    _shared_conn_at = 0.0


def _get_shared_conn(path: str, timeout: float) -> tuple[sqlite3.Connection, float, bool]:
    """(conn, connect_ms, reused) を返す。"""
    global _shared_conn, _shared_conn_path, _shared_conn_at, _conn_opened

    if not _reuse_conn_enabled():
        t0 = time.perf_counter()
        conn = _connect(path, timeout)
        return conn, (time.perf_counter() - t0) * 1000.0, False

    now = time.monotonic()
    if (
        _shared_conn is not None
        and _shared_conn_path == path
        and (now - _shared_conn_at) < _conn_max_age_sec()
    ):
        return _shared_conn, 0.0, True

    _close_shared_conn_locked()
    t0 = time.perf_counter()
    conn = _connect(path, timeout)
    ms = (time.perf_counter() - t0) * 1000.0
    _shared_conn = conn
    _shared_conn_path = path
    _shared_conn_at = now
    _conn_opened += 1
    return conn, ms, False


def _schema_for(conn: sqlite3.Connection, path: str) -> tuple[str, list[str], float]:
    """(table, cols, elapsed_ms)。スキーマは 1 日変わらないのでキャッシュする。"""
    cached = _schema_cache.get(path)
    if cached is not None:
        return cached[0], cached[1], 0.0
    t0 = time.perf_counter()
    table = _pick_table(conn)
    cols = _columns(conn, table) if table else []
    ms = (time.perf_counter() - t0) * 1000.0
    if table and cols:
        _schema_cache[path] = (table, cols)
    return table, cols, ms


def feeder_conn_stats() -> dict:
    """診断用。接続は 1 本のはずなので opened-closed は 0 か 1。"""
    with _conn_lock:
        return {
            "opened": _conn_opened,
            "closed": _conn_closed,
            "outstanding": _conn_opened - _conn_closed,
            "reuse": _reuse_conn_enabled(),
            "schema_cached": list(_schema_cache.keys()),
        }


def _existing_tables(conn: sqlite3.Connection) -> list[str]:
    try:
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
    except Exception:
        return []


def _pick_table(conn: sqlite3.Connection) -> str:
    tables = _existing_tables(conn)
    for t in _TABLES_PREFER:
        if t in tables:
            return t
    return tables[0] if tables else ""


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(r[1]) for r in conn.execute('PRAGMA table_info("%s")' % table).fetchall()]
    except Exception:
        return []


def _pick(cols: list[str], candidates: tuple[str, ...]) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    return None


def read_new_rows() -> tuple[list[dict[str, Any]], int, str]:
    """前回より新しい行だけを読む。

    Returns:
        (rows, max_rowid, note)
    """
    path = _db_path()
    if not os.path.exists(path):
        return [], _LAST_ROWID, "db missing path=%s" % path

    timeout = max(0.2, _env_float("MAIN_PUSH_PRICE_DB_FEED_TIMEOUT_SEC", 3.0))
    cap = max(200, _env_int("MAIN_PUSH_PRICE_DB_FEED_MAX_ROWS", 4000))

    # P0-10: connect_ms / schema_ms / query_ms / fetch_ms を必ず残す。
    _t_all = time.perf_counter()
    _connect_ms = 0.0
    _schema_ms = 0.0
    _query_ms = 0.0
    _fetch_ms = 0.0
    _reused = False
    _db_size = 0
    try:
        _db_size = os.path.getsize(path)
    except Exception:
        _db_size = -1

    _conn_lock.acquire()
    conn = None
    try:
        conn, _connect_ms, _reused = _get_shared_conn(path, timeout)
        table, cols, _schema_ms = _schema_for(conn, path)
        if not table:
            return [], _LAST_ROWID, "no table"

        sym_c = _pick(cols, _SYMBOL_COLS)
        px_c = _pick(cols, _PRICE_COLS)
        if not sym_c or not px_c:
            return [], _LAST_ROWID, "symbol/price column not found table=%s" % table

        name_c = _pick(cols, _NAME_COLS)
        vol_c = _pick(cols, _VOLUME_COLS)
        dt_c = _pick(cols, _DT_COLS)

        sel = [sym_c, px_c] + [c for c in (name_c, vol_c, dt_c) if c]
        sel_sql = ", ".join('"%s"' % c for c in sel)

        if _LAST_ROWID <= 0:
            # 初回だけ全銘柄の最新行を埋める。
            # 実測 (2026-08-25, 12,636 行):
            #   rowid DESC LIMIT 6000        3.8s /  258 銘柄
            #   rowid DESC LIMIT 3000        1.8s /  151 銘柄
            #   銘柄ごと MAX(rowid) JOIN     0.47s /  542 銘柄  <- これを使う
            # 転送量が銘柄数ぶんだけで済み、取りこぼしも無い。
            sql = (
                'SELECT t.rowid, %s FROM "%s" t '
                'JOIN (SELECT "%s" s, MAX(rowid) mr FROM "%s" GROUP BY "%s") m '
                "ON t.rowid = m.mr"
                % (
                    ", ".join('t."%s"' % c for c in sel),
                    table,
                    sym_c,
                    table,
                    sym_c,
                )
            )
            params: tuple = ()
            note = "prime latest-per-symbol"
        else:
            sql = (
                'SELECT rowid, %s FROM "%s" WHERE rowid > ? ORDER BY rowid DESC LIMIT %d'
                % (sel_sql, table, cap)
            )
            params = (int(_LAST_ROWID),)
            note = "incremental from rowid=%d" % _LAST_ROWID

        _t = time.perf_counter()
        cur = conn.execute(sql, params)
        _query_ms = (time.perf_counter() - _t) * 1000.0
        _t = time.perf_counter()
        raw = cur.fetchall()
        _fetch_ms = (time.perf_counter() - _t) * 1000.0
        try:
            cur.close()
        except Exception:
            logger.debug("[MAIN PRICE DB FEED] cursor close failed", exc_info=True)
    except Exception:
        # 壊れた接続を使い回さない。次回に作り直す。
        _close_shared_conn_locked()
        _schema_cache.pop(path, None)
        raise
    finally:
        # 共有を使わない設定のときだけ都度閉じる。
        # 共有時は _get_shared_conn が寿命を管理するのでここでは閉じない。
        if conn is not None and not _reuse_conn_enabled():
            try:
                conn.close()
            except Exception:
                logger.debug("[MAIN PRICE DB FEED] close failed", exc_info=True)
        _conn_lock.release()

    _total_ms = (time.perf_counter() - _t_all) * 1000.0
    _log = logger.warning if _total_ms >= 1000.0 else logger.info
    _log(
        "[MAIN PRICE DB FEED LATENCY] connect_ms=%.1f schema_ms=%.1f query_ms=%.1f "
        "fetch_ms=%.1f total_ms=%.1f rows=%d db_size_mb=%.1f rowid_delta=%s "
        "conn_reused=%s conn_stats=%s",
        _connect_ms, _schema_ms, _query_ms, _fetch_ms, _total_ms, len(raw),
        (_db_size / 1024.0 / 1024.0) if _db_size and _db_size > 0 else -1.0,
        (max(int(r[0]) for r in raw) - int(_LAST_ROWID)) if raw else 0,
        _reused,
        {"opened": _conn_opened, "closed": _conn_closed,
         "outstanding": _conn_opened - _conn_closed},
    )

    if not raw:
        return [], _LAST_ROWID, note + " (no new rows)"

    max_rowid = max(int(r[0]) for r in raw)

    # rowid DESC なので、銘柄ごとに最初に現れた行が最新。
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for r in raw:
        sym = r[1]
        key = str(sym)
        if key in seen:
            continue
        seen.add(key)
        d: dict[str, Any] = {sym_c: sym, px_c: r[2]}
        i = 3
        for c in (name_c, vol_c, dt_c):
            if c:
                d[c] = r[i]
                i += 1
        rows.append(d)

    return rows, max_rowid, note


def feed_once() -> tuple[int, int]:
    """1 サイクル分だけ供給する。戻り値 (対象銘柄数, 更新した銘柄数)。"""
    global _LAST_ROWID

    from trading.push.latest_price_cache import update_latest_prices_from_push_rows

    t0 = time.perf_counter()
    rows, max_rowid, note = read_new_rows()
    read_sec = time.perf_counter() - t0

    if not rows:
        logger.debug(
            "[MAIN PRICE DB FEED] no rows (%s) read=%.3fs version=%s", note, read_sec, VERSION
        )
        return 0, 0

    updated = update_latest_prices_from_push_rows(rows, source="push_db")
    _LAST_ROWID = max(int(_LAST_ROWID), int(max_rowid))
    elapsed = time.perf_counter() - t0

    logger.info(
        "[MAIN PRICE DB FEED] symbols=%s updated=%s rowid=%s read=%.3fs total=%.3fs %s version=%s",
        len(rows),
        updated,
        _LAST_ROWID,
        read_sec,
        elapsed,
        note,
        VERSION,
    )
    if updated <= 0:
        logger.warning(
            "[MAIN PRICE DB FEED] got %s symbols but updated 0; check push db columns (%s)",
            len(rows),
            note,
        )
    return len(rows), updated


def _feed_backoff_enabled() -> bool:
    v = os.getenv("MAIN_PUSH_PRICE_DB_FEED_BACKOFF")
    if v is None or str(v).strip() == "":
        return True
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _feed_backoff_sec(fail_streak: int, interval: float) -> float:
    """連続失敗に応じた待ち時間。1 -> 2 -> 5 -> 10 -> 30 秒 (上限)。

    DB が見えないのに毎 2 秒接続を試み続けると、
    NAS への接続要求と例外ログだけが増えて状況を悪化させる。
    """
    if not _feed_backoff_enabled():
        return interval
    try:
        cap = float(os.getenv("MAIN_PUSH_PRICE_DB_FEED_BACKOFF_MAX_SEC", "30"))
    except Exception:
        cap = 30.0
    ladder = (1.0, 2.0, 5.0, 10.0, 30.0)
    idx = min(max(0, int(fail_streak) - 1), len(ladder) - 1)
    return max(interval, min(cap, ladder[idx]))


def _loop() -> None:
    interval = max(0.5, _env_float("MAIN_PUSH_PRICE_DB_FEED_INTERVAL_SEC", 2.0))
    logger.warning(
        "[MAIN PRICE DB FEED] feeder started interval=%.1fs version=%s "
        "(PUSH WS owner is push_receiver_runner.py)",
        interval,
        VERSION,
    )
    fail_streak = 0
    idle_streak = 0
    wait_sec = interval
    while not _STOP.is_set():
        try:
            _, updated = feed_once()
            if fail_streak:
                logger.warning(
                    "[MAIN PRICE DB FEED] recovered after %d failures -> interval=%.1fs",
                    fail_streak, interval,
                )
            fail_streak = 0
            wait_sec = interval
            if updated <= 0:
                idle_streak += 1
                # 供給が長く止まったら黙認せず警告する。
                if idle_streak in (30, 120) or (idle_streak and idle_streak % 300 == 0):
                    logger.warning(
                        "[MAIN PRICE DB FEED] no price update for %d cycles "
                        "(interval=%.1fs); is push_receiver_runner.py writing the push db?",
                        idle_streak,
                        interval,
                    )
            else:
                idle_streak = 0
        except Exception as e:
            fail_streak += 1
            wait_sec = _feed_backoff_sec(fail_streak, interval)
            # ログを間引く。817 個のトレースバックを NAS 上のログへ書くこと自体が
            # NAS 障害中の負荷になっていた。
            if fail_streak <= 3:
                logger.exception(
                    "[MAIN PRICE DB FEED] cycle failed streak=%s next_wait=%.1fs version=%s",
                    fail_streak, wait_sec, VERSION,
                )
            elif fail_streak <= 10 or fail_streak % 60 == 0:
                logger.warning(
                    "[MAIN PRICE DB FEED] cycle failed streak=%s next_wait=%.1fs err=%r "
                    "(memory PUSH 経路が生きていれば Entry は継続する)",
                    fail_streak, wait_sec, e,
                )
        _STOP.wait(wait_sec)
    logger.warning("[MAIN PRICE DB FEED] feeder stopped version=%s", VERSION)


def is_running() -> bool:
    with _LOCK:
        return bool(_THREAD is not None and _THREAD.is_alive())


def start_latest_price_db_feeder() -> bool:
    """main.py から呼ぶ。PUSH DB -> latest_price_cache の供給を開始する。"""
    global _THREAD

    if not _env_bool("MAIN_PUSH_PRICE_DB_FEED_ENABLED", True):
        logger.warning(
            "[MAIN PRICE DB FEED] disabled by MAIN_PUSH_PRICE_DB_FEED_ENABLED=0; "
            "latest_price_cache will have no source in main process"
        )
        return False

    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            logger.info("[MAIN PRICE DB FEED] already running version=%s", VERSION)
            return True
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, name="main-latest-price-db-feed", daemon=True)
        _THREAD.start()

    return True


def stop_latest_price_db_feeder() -> None:
    _STOP.set()


__all__ = [
    "start_latest_price_db_feeder",
    "stop_latest_price_db_feeder",
    "is_running",
    "feed_once",
    "read_new_rows",
    "VERSION",
]
