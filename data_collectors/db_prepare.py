# ============================================================
# File   : data_collectors/db_prepare.py
# Version: DATA-COLLECTORS-DB-PREPARE-V3-NO-FORCED-JOURNAL-CHANGE
# ------------------------------------------------------------
# Purpose:
#   - 当日DBを作成し、最低限のテーブル・INDEX・WALを準備する
#   - 既存データは削除しない
#   - PUSH / ranking / subscription DB を最優先で準備する
#   - summary DB が main.py 等に掴まれていても data_collectors 全体を落とさない
# ============================================================

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

from data_collectors.config import (
    SQLITE_BUSY_TIMEOUT_MS,
    SQLITE_TIMEOUT_SEC,
    push_db_path,
    ranking_db_path,
    summary_db_path,
    subscription_db_path,
    trade_date_yyyymmdd,
)

logger = logging.getLogger(__name__)

DB_PREPARE_RETRY_MAX = int(os.environ.get("DB_PREPARE_RETRY_MAX", "3"))
DB_PREPARE_RETRY_SLEEP_SEC = float(os.environ.get("DB_PREPARE_RETRY_SLEEP_SEC", "2.0"))
# DB 1 つあたりの総時間上限 (秒)。retry の天井。
DB_PREPARE_BUDGET_SEC = float(os.environ.get("DB_PREPARE_BUDGET_SEC", "30"))

# db_prepare 専用の busy_timeout。
# 共通の SQLITE_BUSY_TIMEOUT_MS は 30,000ms で、これは「実際にデータを
# 読み書きする経路」向けの値。db_prepare がやるのは schema 確認だけなので、
# ロックされている DB を 1 文あたり 30 秒も待つ必要はない。
# (実測: summary DB がロック中に CREATE TABLE 9 文 x 30 秒 x 3 回で
#  起動が数分止まった)
DB_PREPARE_BUSY_TIMEOUT_MS = int(os.environ.get("DB_PREPARE_BUSY_TIMEOUT_MS", "3000"))

# data_collectors は「DB作成・ランキング取得・PUSH受信」が主目的。
# summary DB は main.py 側でも準備されるため、ロック時は非致命扱いにする。
DB_PREPARE_SUMMARY_FATAL = str(os.environ.get("DB_PREPARE_SUMMARY_FATAL", "0")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


# ============================================================
# 2026-09-03: sqlite3 エラーの分類
# ------------------------------------------------------------
# 旧実装は `"locked" in str(e)` だけを見ていた。
# ところが実際に出た例外は
#     sqlite3.OperationalError: locking protocol
# で、"locking" は含むが **"locked" は含まない**。
# そのため一時的な競合が「不明なエラー」と判定されて raise され、
# fatal=True の push db で db_prepare が異常終了 →
# main_database.py が exit code 1 で落ちていた。
#
# 一時的な競合と、本当に壊れている/開けないものを明確に分ける。
# 破損・schema 不備は絶対に握りつぶさない。
# ============================================================

# 一時的な競合。retry で解ける可能性があり、致命扱いしない。
_SQLITE_TRANSIENT_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "locking protocol",      # SQLITE_PROTOCOL: SMB 上の -shm ロック交渉が
                             # 何度リトライしても収束しなかったときに出る
    "is busy",
    "database is busy",
    "locked",                # 旧実装の互換 (上に該当しない "... locked" 系)
)

# 本当に壊れている / 開けない / schema が無い。握りつぶさない。
_SQLITE_FATAL_MARKERS = (
    "malformed",
    "unable to open database",
    "file is not a database",
    "is not a database",
    "encrypted",
    "no such table",
    "no such column",
)


def _classify_sqlite_error(e: BaseException) -> str:
    """"fatal" / "transient" / "unknown" を返す。"""
    s = str(e).lower()
    for m in _SQLITE_FATAL_MARKERS:
        if m in s:
            return "fatal"
    for m in _SQLITE_TRANSIENT_MARKERS:
        if m in s:
            return "transient"
    return "unknown"


def _is_locked_error(e: BaseException) -> bool:
    """後方互換。一時的な競合なら True。"""
    return _classify_sqlite_error(e) == "transient"


def _is_fatal_db_error(e: BaseException) -> bool:
    return _classify_sqlite_error(e) == "fatal"


# journal_mode 変更の retry は「短く数回」だけ。
# 30 秒 60 秒待って解決させる設計にはしない。既存 DB では変更自体を避ける。
JOURNAL_RETRY_MAX = int(os.environ.get("DB_PREPARE_JOURNAL_RETRY_MAX", "4"))
JOURNAL_RETRY_SLEEP_SEC = float(os.environ.get("DB_PREPARE_JOURNAL_RETRY_SLEEP_SEC", "0.5"))

# 既存 DB の journal_mode を強制的に WAL へ変えたいときだけ 1 にする。
FORCE_JOURNAL_WAL_ON_EXISTING = str(
    os.environ.get("DB_PREPARE_FORCE_JOURNAL_WAL", "0")
).strip().lower() in {"1", "true", "yes", "on"}

RO_TIMEOUT_SEC = float(os.environ.get("DB_PREPARE_RO_TIMEOUT_SEC", "10"))

# 既存 DB がロック競合で確認できなかっただけのとき、致命扱いにするか。
# 既定 0 = 落とさない (locking protocol / database is locked)。
# 破損・open 不能は DB_PREPARE_TRANSIENT_FATAL に関係なく常に致命。
DB_PREPARE_TRANSIENT_FATAL = str(
    os.environ.get("DB_PREPARE_TRANSIENT_FATAL", "0")
).strip().lower() in {"1", "true", "yes", "on"}


def _db_is_new(path: Path) -> bool:
    """まだ中身が無い (これから作る) DB か。"""
    try:
        if not path.exists():
            return True
        return path.stat().st_size == 0
    except Exception:
        return False


def _current_journal_mode(conn: sqlite3.Connection) -> str:
    """現在の journal_mode を **読むだけ**。変更はしない。"""
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        if row and row[0] is not None:
            return str(row[0]).strip().lower()
    except Exception:
        logger.debug("[DB PREPARE] journal_mode read failed", exc_info=True)
    return ""


def _ensure_journal_mode_wal(conn: sqlite3.Connection, path: Path, label: str) -> None:
    """起動のたびに journal_mode を書き換えない。

    journal_mode の変更は DB 全体に効く排他操作で、SMB 上では
    -shm のロック交渉が絡む。writer (push_receiver 等) が動いているだけで
    `locking protocol` になり得る。
    既に wal なら **何もしない** のが正しい。
    """
    current = _current_journal_mode(conn)
    if current == "wal":
        logger.info(
            "[DB PREPARE] %s journal_mode already WAL -> skip change path=%s", label, path
        )
        return

    created_now = _db_is_new(path)
    if not created_now and not FORCE_JOURNAL_WAL_ON_EXISTING:
        logger.warning(
            "[DB PREPARE] %s journal_mode=%s は WAL ではないが、既存 DB なので変更しない "
            "path=%s (変更するなら DB_PREPARE_FORCE_JOURNAL_WAL=1。"
            "稼働中の writer/reader が居る DB の journal_mode 変更は locking protocol を招く)",
            label, current or "unknown", path,
        )
        return

    for attempt in range(1, JOURNAL_RETRY_MAX + 1):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            logger.info(
                "[DB PREPARE] %s journal_mode -> WAL (was %s) path=%s attempt=%d",
                label, current or "unknown", path, attempt,
            )
            return
        except sqlite3.OperationalError as e:
            if _is_fatal_db_error(e):
                raise
            if attempt < JOURNAL_RETRY_MAX:
                time.sleep(JOURNAL_RETRY_SLEEP_SEC)
                continue
            logger.warning(
                "[DB PREPARE] %s journal_mode=WAL を %d 回試して失敗 path=%s err=%s "
                "(journal_mode の設定失敗だけでは落とさない。schema 確認は続行する)",
                label, JOURNAL_RETRY_MAX, path, e,
            )
            return
        except Exception as e:
            logger.warning(
                "[DB PREPARE] %s journal_mode 設定を諦めた path=%s err=%s", label, path, e
            )
            return


def _connect(path: Path, label: str = "db") -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=min(SQLITE_TIMEOUT_SEC, DB_PREPARE_BUSY_TIMEOUT_MS / 1000.0 * 2))
    conn.execute(f"PRAGMA busy_timeout={DB_PREPARE_BUSY_TIMEOUT_MS}")

    # 既存 DB では journal_mode を触らない (上の docstring 参照)。
    _ensure_journal_mode_wal(conn, path, label)

    # synchronous / temp_store は接続単位なので他プロセスに影響しない。
    for pragma in ("PRAGMA synchronous=NORMAL", "PRAGMA temp_store=MEMORY"):
        try:
            conn.execute(pragma)
        except sqlite3.OperationalError as e:
            if _is_fatal_db_error(e):
                raise
            logger.warning("[DB PREPARE] %s %s skipped path=%s err=%s", label, pragma, path, e)
    return conn


@contextlib.contextmanager
def _connect_cm(path: Path, label: str):
    """commit / rollback に加えて **close する** context manager。

    `with sqlite3.connect(...) as conn:` は commit/rollback しかせず
    close しない (Python 公式仕様)。NAS 上でこれを繰り返すと接続が残り、
    ハンドル枯渇と、ここで問題になっているロック競合の両方を招く。
    """
    conn = _connect(path, label)
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            logger.debug("[DB PREPARE] %s close failed path=%s", label, path, exc_info=True)


def _read_only_object_names(path: Path) -> set[str] | None:
    """read-only で table / index 名を取る。取れなければ None。

    書き込みロックを一切取らずに「もう schema が揃っているか」を判定するため。
    揃っていれば書き込み接続を開かずに済む = locking protocol を踏まない。
    """
    if _db_is_new(path):
        return None

    conn = None
    try:
        raw = str(path).replace("\\", "/")
        if raw.startswith("//"):
            uri = "file:" + raw + "?mode=ro"      # UNC
        else:
            uri = "file:///" + raw.lstrip("/") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=RO_TIMEOUT_SEC)
    except Exception:
        conn = None

    if conn is None:
        try:
            conn = sqlite3.connect(str(path), timeout=RO_TIMEOUT_SEC)
            conn.execute("PRAGMA query_only=ON")
        except Exception as e:
            if _is_fatal_db_error(e):
                raise
            logger.debug("[DB PREPARE] read-only open failed path=%s err=%s", path, e)
            return None

    try:
        conn.execute("PRAGMA busy_timeout=%d" % int(RO_TIMEOUT_SEC * 1000))
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
    except Exception as e:
        if _is_fatal_db_error(e):
            raise
        logger.debug("[DB PREPARE] read-only schema scan failed path=%s err=%s", path, e)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _schema_already_complete(path: Path, label: str, required: set) -> bool:
    """必要な table / index が全部あるなら True (書き込み接続を開かない)。"""
    if not required:
        return False
    try:
        have = _read_only_object_names(path)
    except sqlite3.DatabaseError:
        raise                      # 破損などは隠さない
    if have is None:
        return False
    missing = {n for n in required if n not in have}
    if missing:
        logger.info(
            "[DB PREPARE] %s schema 不足あり missing=%s -> 作成する path=%s",
            label, sorted(missing), path,
        )
        return False
    logger.info(
        "[DB PREPARE] %s schema already complete (%d objects) -> skip write path=%s",
        label, len(required), path,
    )
    return True


def _run_with_retry(label: str, path: Path, fn: Callable[[Path], None], *, fatal: bool = True) -> bool:
    last_exc: BaseException | None = None
    # DB 1 つに使ってよい時間の上限。
    # 「長く待てば解決する」設計にしないための天井。
    # SQLite は locking protocol を返すまで内部で ~10 秒リトライするので、
    # ここを無制限にすると 1 DB で 3 分近く起動が止まる (実測 175.9 秒)。
    _deadline = time.monotonic() + DB_PREPARE_BUDGET_SEC

    for attempt in range(1, DB_PREPARE_RETRY_MAX + 1):
        if attempt > 1 and time.monotonic() > _deadline:
            logger.warning(
                "[DB PREPARE] %s 時間上限 %.0fs に達したので retry を打ち切る path=%s",
                label, DB_PREPARE_BUDGET_SEC, path,
            )
            break
        try:
            fn(path)
            return True
        except sqlite3.OperationalError as e:
            last_exc = e
            if _is_fatal_db_error(e):
                # 破損 / 開けない / schema 不備 は隠さない。
                logger.error(
                    "[DB PREPARE] %s FATAL db error path=%s err=%s", label, path, e
                )
                raise
            if not _is_locked_error(e):
                if fatal:
                    raise
                logger.exception("[DB PREPARE] %s failed non-locked but non-fatal path=%s", label, path)
                return False

            logger.warning(
                "[DB PREPARE] %s locked attempt=%d/%d path=%s err=%s",
                label,
                attempt,
                DB_PREPARE_RETRY_MAX,
                path,
                e,
            )
            if attempt < DB_PREPARE_RETRY_MAX:
                time.sleep(DB_PREPARE_RETRY_SLEEP_SEC * attempt)
        except Exception as e:
            last_exc = e
            if fatal:
                raise
            logger.exception("[DB PREPARE] %s failed but non-fatal path=%s", label, path)
            return False

    if fatal:
        # ------------------------------------------------------------
        # 2026-09-03: 「既存 DB がロック競合で触れなかっただけ」で
        # data_collectors 全体を落とさない。
        #
        #   locking protocol / database is locked  -> 一時的。起動は継続する。
        #   malformed / unable to open database    -> 上の except で即 raise。
        #
        # DB ファイル自体は存在するので、writer (push_receiver 等) が
        # 改めて開き直せば復旧しうる。ここで exit code 1 にすると
        # main_database.py ごと落ちて何も動かなくなる。
        # 従来どおり致命扱いにするなら DB_PREPARE_TRANSIENT_FATAL=1。
        # ------------------------------------------------------------
        if (
            last_exc is not None
            and _classify_sqlite_error(last_exc) == "transient"
            and not _db_is_new(path)
            and not DB_PREPARE_TRANSIENT_FATAL
        ):
            logger.error(
                "[DB PREPARE] %s 既存 DB がロック競合で確認できなかった path=%s last_err=%s "
                "-> 起動は継続する (DB は存在する。schema 確認は writer 側の再試行に委ねる)。"
                "致命扱いに戻すなら DB_PREPARE_TRANSIENT_FATAL=1",
                label, path, last_exc,
            )
            return False
        raise RuntimeError(f"{label} failed after retry path={path}: {last_exc}") from last_exc

    logger.error(
        "[DB PREPARE] %s skipped after retry because locked/non-fatal path=%s last_err=%s",
        label,
        path,
        last_exc,
    )
    return False


def _ensure_push_db(path: Path) -> None:
    _required = {
        "push_raw_1min",
        "idx_push_raw_1min_symbol_datetime",
        "push_ticks",
        "idx_push_ticks_symbol_datetime",
    }
    if _schema_already_complete(path, "push", _required):
        return
    with _connect_cm(path, "push") as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_raw_1min (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                symbolname TEXT,
                datetime TEXT NOT NULL,
                price REAL,
                current_price REAL,
                volume REAL,
                trading_volume REAL,
                source TEXT DEFAULT 'push',
                inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_push_raw_1min_symbol_datetime
            ON push_raw_1min(symbol, datetime)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                symbolname TEXT,
                datetime TEXT NOT NULL,
                price REAL,
                current_price REAL,
                volume REAL,
                trading_volume REAL,
                raw_json TEXT,
                inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_push_ticks_symbol_datetime
            ON push_ticks(symbol, datetime)
        """)

        # ------------------------------------------------------------
        # stream_data / stream_data_raw の datetime 単独 index
        # ------------------------------------------------------------
        # これらのテーブルは push_db_writer が作るのでスキーマ定義は
        # ここには無い。ただし index が (symbol, datetime) の複合しか無く、
        # 先頭列が symbol なので
        #     WHERE datetime >= ? ORDER BY datetime DESC LIMIT ?
        # に一切使えなかった。
        #
        # 2026-09-03 実測 (push20260903.db = 403MB / 52,739行 / 65列):
        #     EXPLAIN QUERY PLAN -> SCAN stream_data_raw
        #                           USE TEMP B-TREE FOR ORDER BY
        # つまり毎回 403MB を NAS 越しに全読みして全ソートしていた。
        # この read は pandas.read_sql_query -> cursor.execute の内側で
        # Python からキャンセルできないため、cancelled=True の
        # Summary worker が 13〜16 分生き残る原因になっていた。
        #
        # datetime 単独 index を足すと
        #     SCAN ... USING INDEX idx_stream_data_raw_datetime
        # となり TEMP B-TREE が消え、LIMIT で早期終了できる。
        # クエリ本文は変更していない (挙動は同じで速くなるだけ)。
        # ------------------------------------------------------------
        # covering index が必要な理由 (2026-09-03 実測)
        # ------------------------------------------------------------
        # datetime 単独 index を足すと EXPLAIN は
        #     SCAN ... USING INDEX idx_stream_data_raw_datetime
        # になり TEMP B-TREE は消えるが、**行ごとにテーブル本体を引く**ため
        # SMB 越しでは 1 行あたり往復が発生する。実測:
        #     LIMIT=500  -> 2.17s (4.3 ms/row)
        #     LIMIT=2000 -> 5.64s (2.8 ms/row)
        #     => 30,000 行で約 85 秒、負荷時に 267 秒
        # 転送量ではない (14 列で 247 bytes/row = 30,000 行でも 7.1MB、
        #  raw_json は実データが空)。往復回数が支配的。
        #
        # push_summary_engine._push_summary_db_source_read_recent_rows() が
        # 選ぶ 14 列を index に含めると
        #     SCAN ... USING COVERING INDEX
        # になりテーブル参照が消える (同一スキーマのローカル複製で検証済み)。
        # この read は pandas の cursor.fetchall() の内側で
        # Python からキャンセルできないため、cancel SLA を守るには
        # 往復そのものを減らすしかない。
        _summary_read_cols = (
            "datetime", "symbol", "symbolname", "date", "time", "price", "volume",
            "trading_value", "vwap", "previousclose", "opening_price",
            "high_price", "low_price", "received_at",
        )
        for _tbl in ("stream_data", "stream_data_raw"):
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_tbl,)
                ).fetchone()
                if not exists:
                    continue

                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_%s_datetime ON %s(datetime)" % (_tbl, _tbl)
                )
                logger.info("[DB PREPARE] push %s(datetime) index ensured", _tbl)

                # covering index は **読み取り側が実際に使うテーブルだけ** に付ける。
                # _push_summary_db_source_read_recent_rows() は
                # stream_data_raw があればそちらを選ぶので、stream_data 側に
                # 付けても読みには一切寄与せず INSERT を重くするだけ。
                #
                # 2026-09-03 実測 (PUSH DB writer の 1000 行バッチ commit):
                #     covering index なし  20.8 22.3 23.4 24.1 26.5 28.7 28.8 30.9 s (中央値 ~25s)
                #     両テーブルに付与     20.7 25.1 27.2 28.4 30.1 48.1 49.7 53.8 s (中央値 ~28s)
                # 書き込みは元々律速 (1000行/25秒 = 2400行/分 に対し受信 2000行/分)
                # なので、無駄な index 1 本で queue が単調増加へ転じた。
                have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % _tbl)}
                cover = [c for c in _summary_read_cols if c in have]
                if _tbl != "stream_data_raw":
                    # 読みに使わないテーブルには付けない (古い版が作っていたら消す)
                    try:
                        conn.execute("DROP INDEX IF EXISTS idx_%s_summary_cover" % _tbl)
                    except Exception:
                        logger.exception("[DB PREPARE] push %s covering index drop failed", _tbl)
                elif "datetime" in cover and len(cover) >= 4:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_%s_summary_cover ON %s(%s)"
                        % (_tbl, _tbl, ", ".join('"%s"' % c for c in cover))
                    )
                    logger.info(
                        "[DB PREPARE] push %s summary covering index ensured cols=%d",
                        _tbl, len(cover),
                    )
            except Exception:
                # index が作れなくても PUSH 受信は続けられる。落とさない。
                logger.exception("[DB PREPARE] push %s index failed", _tbl)

    logger.info("[DB PREPARE] push db ready: %s", path)


def _ensure_ranking_db(path: Path) -> None:
    _required = {
        "ranking_snapshot_1min",
        "ux_ranking_snapshot_1min_key",
        "idx_ranking_snapshot_1min_datetime",
        "ranking_raw_1min",
        "idx_ranking_raw_1min_datetime",
    }
    if _schema_already_complete(path, "ranking", _required):
        return
    with _connect_cm(path, "ranking") as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ranking_snapshot_1min (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                symbolname TEXT,
                rank INTEGER,
                rank_type TEXT NOT NULL DEFAULT '',
                market TEXT NOT NULL DEFAULT 'ALL',
                category TEXT,
                ranking_type TEXT,
                price REAL,
                current_price REAL,
                change_rate REAL,
                change_percentage REAL,
                change_ratio REAL,
                volume REAL,
                trading_volume REAL,
                turnover REAL,
                trading_value REAL,
                tick_count REAL,
                snapshot_time TEXT NOT NULL,
                datetime TEXT NOT NULL,
                inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_ranking_snapshot_1min_key
            ON ranking_snapshot_1min(symbol, datetime, rank_type, market)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ranking_snapshot_1min_datetime
            ON ranking_snapshot_1min(datetime)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS ranking_raw_1min (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                symbolname TEXT,
                rank INTEGER,
                rank_type TEXT,
                market TEXT,
                category TEXT,
                ranking_type TEXT,
                price REAL,
                current_price REAL,
                change_rate REAL,
                change_percentage REAL,
                change_ratio REAL,
                volume REAL,
                trading_volume REAL,
                turnover REAL,
                trading_value REAL,
                tick_count REAL,
                snapshot_time TEXT,
                datetime TEXT,
                raw_json TEXT,
                inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ranking_raw_1min_datetime
            ON ranking_raw_1min(datetime)
        """)
    logger.info("[DB PREPARE] ranking db ready: %s", path)


def _ensure_summary_db(path: Path) -> None:
    _required = {
        n
        for t in ("stock_summary_1min", "stock_summary_3min", "stock_summary_5min")
        for n in (t, "ux_%s_symbol_datetime" % t, "idx_%s_datetime" % t)
    }
    if _schema_already_complete(path, "summary", _required):
        return
    with _connect_cm(path, "summary") as conn:
        for table in ("stock_summary_1min", "stock_summary_3min", "stock_summary_5min"):
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    symbolname TEXT,
                    datetime TEXT NOT NULL,
                    date TEXT,
                    time TEXT,
                    interval INTEGER,
                    source TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume REAL,
                    ma5 REAL,
                    ma25 REAL,
                    ma75 REAL,
                    rsi REAL,
                    macd REAL,
                    signal REAL,
                    atr REAL,
                    slope REAL,
                    slope_atr_scaled REAL,
                    score REAL,
                    score_buy REAL,
                    score_sell REAL,
                    score_total REAL,
                    final_score REAL,
                    display_score REAL,
                    inserted_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute(f"""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_{table}_symbol_datetime
                ON {table}(symbol, datetime)
            """)
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{table}_datetime
                ON {table}(datetime)
            """)

            # ------------------------------------------------------------
            # indicator warmup 用の covering index (2026-09-03)
            # ------------------------------------------------------------
            # trading/summary/indicators/indicator_calculator._read_summary_tail が
            # symbol ごとの直近 N 本を読む。summary テーブルは 166 列あるため、
            # 通常の index だと 1 行ごとに行本体を取りに行き SMB で 1 行 1 往復になる。
            # 実測: 241.344s / 32,458 行 = 7.4 ms/row。
            # 必要列を index に含めると SEARCH ... USING COVERING INDEX になり
            # 行フェッチが消える (push DB で同じ形の問題を確認済み)。
            try:
                _have = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                _cover = [c for c in ("symbol", "datetime", "open", "high", "low",
                                      "close", "volume", "open_price", "high_price",
                                      "low_price", "close_price")
                          if c in _have]
                if "symbol" in _cover and "datetime" in _cover and len(_cover) >= 4:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_%s_warmup_cover ON \"%s\"(%s)"
                        % (table, table, ", ".join('"%s"' % c for c in _cover))
                    )
                    logger.info(
                        "[DB PREPARE] summary %s warmup covering index ensured cols=%d",
                        table, len(_cover),
                    )
            except Exception:
                logger.exception("[DB PREPARE] summary %s warmup covering index failed", table)
    logger.info("[DB PREPARE] summary db ready: %s", path)


def _ensure_subscription_db(path: Path) -> None:
    _required = {
        "subscription_history",
        "idx_subscription_history_datetime",
    }
    if _schema_already_complete(path, "subscription", _required):
        return
    with _connect_cm(path, "subscription") as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT,
                reason TEXT,
                symbol TEXT NOT NULL,
                symbolname TEXT,
                registered INTEGER DEFAULT 1,
                datetime TEXT NOT NULL,
                inserted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_subscription_history_datetime
            ON subscription_history(datetime)
        """)
    logger.info("[DB PREPARE] subscription db ready: %s", path)


def prepare_all_daily_dbs(trade_date: str | None = None) -> None:
    d = trade_date or trade_date_yyyymmdd()
    logger.info("[DB PREPARE] start trade_date=%s", d)

    push_ok = _run_with_retry("push db", push_db_path(d), _ensure_push_db, fatal=True)
    ranking_ok = _run_with_retry("ranking db", ranking_db_path(d), _ensure_ranking_db, fatal=True)
    subscription_ok = _run_with_retry(
        "subscription db",
        subscription_db_path(d),
        _ensure_subscription_db,
        fatal=True,
    )

    summary_ok = _run_with_retry(
        "summary db",
        summary_db_path(d),
        _ensure_summary_db,
        fatal=DB_PREPARE_SUMMARY_FATAL,
    )

    logger.info(
        "[DB PREPARE] done trade_date=%s push=%s ranking=%s subscription=%s summary=%s summary_fatal=%s",
        d,
        push_ok,
        ranking_ok,
        subscription_ok,
        summary_ok,
        DB_PREPARE_SUMMARY_FATAL,
    )
