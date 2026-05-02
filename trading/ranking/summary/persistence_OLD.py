# ============================================================
# File   : trading/ranking/summary/persistence.py
# Ver    : PRODUCTION-STABLE-REV2.1-RANKING-SUMMARY-PERSISTENCE
#          -PUSH-COMPAT-OHLCV-SCHEMA
#          -AUTO-ALTER-MISSING-COLUMNS
#          -SQLITE-LOCK-SAFE-TX
#          -SAME-CONNECTION-CLEANUP
#          -NAS-SAFE
#          -NO-JOURNALMODE-SWITCH-ON-EVERY-CONNECT
#          -COMMIT-STRICT
#          -LOCK-RETRY-ENHANCED
# ------------------------------------------------------------
# 【概要】
#   ranking_summary_1min / 3min / 5min 専用保存モジュール
#
# 【重要方針】
#   - stock_summary_* には保存しない
#   - PUSH由来サマリーとはDBテーブルを分離する
#   - ranking_summary_* はランキング専用DBに保存する
#   - ただし列構成は PUSH/Yahoo summary と互換に寄せる
#   - UNIQUE(symbol, datetime) でUPSERTする
#   - cleanup / dedupe / schema alter / upsert を同一transactionで実行する
#   - SQLite lock に配慮し busy_timeout を設定
#   - NAS運用では journal_mode の「毎回切替」を避ける
#
# 【REV2.1 修正】
#   - COMMIT失敗を握りつぶさない
#   - database is locked / busy / locked table を判定してリトライ
#   - BEGIN IMMEDIATE 失敗時もリトライ
#   - connection close をより安全化
#   - 保存成功ログは COMMIT 成功後のみ
#   - load側の ensure でロックした場合も読み込みを止めにくくする
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from typing import Optional, Dict, Any, List

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"
DEFAULT_RANKING_DIR = os.path.join(
    DEFAULT_BASE_DIR,
    "raw_data",
    "kabu_station",
    "ranking",
)

BUSY_TIMEOUT_MS = 60000
SQLITE_TIMEOUT_SEC = 60
MAX_SAVE_RETRY = 6
RETRY_SLEEP_BASE_SEC = 0.5
RETRY_SLEEP_MAX_SEC = 5.0

# 同一プロセス内の ranking summary write を直列化
_RANKING_SUMMARY_DB_LOCK = threading.RLock()


# ============================================================
# path helpers
# ============================================================

def _today_yyyymmdd() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _normalize_yyyymmdd(value=None) -> str:
    if value is None:
        return _today_yyyymmdd()

    if isinstance(value, dt.datetime):
        return value.strftime("%Y%m%d")

    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")

    s = str(value).strip()
    if not s:
        return _today_yyyymmdd()

    if "-" in s or "/" in s:
        return pd.to_datetime(s).strftime("%Y%m%d")

    if len(s) == 8 and s.isdigit():
        return s

    return pd.to_datetime(s).strftime("%Y%m%d")


def get_ranking_db_path(
    trade_date=None,
    *,
    ranking_dir: str = DEFAULT_RANKING_DIR,
) -> str:
    ymd = _normalize_yyyymmdd(trade_date)
    return os.path.join(ranking_dir, f"ranking{ymd}.db")


# ============================================================
# sqlite helpers
# ============================================================

def _is_sqlite_locked_error(exc: BaseException) -> bool:
    try:
        msg = str(exc).lower()
        return (
            "database is locked" in msg
            or "database table is locked" in msg
            or "database schema is locked" in msg
            or "database is busy" in msg
            or "locked" in msg
            or "busy" in msg
        )
    except Exception:
        return False


def _retry_sleep(attempt: int) -> float:
    sleep_sec = min(
        RETRY_SLEEP_BASE_SEC * (2 ** max(int(attempt) - 1, 0)),
        RETRY_SLEEP_MAX_SEC,
    )
    time.sleep(sleep_sec)
    return sleep_sec


def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)

    con = sqlite3.connect(
        path,
        timeout=SQLITE_TIMEOUT_SEC,
        check_same_thread=False,
        isolation_level=None,  # 手動で BEGIN / COMMIT / ROLLBACK
    )

    try:
        con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    except Exception:
        logger.warning("[RANKING SUMMARY DB] PRAGMA busy_timeout set failed", exc_info=True)

    # NAS運用では接続のたびの journal_mode 切替がロック競合を起こしやすい。
    # ここでは切替を行わず、診断用に現在値だけ取得する。
    try:
        row = con.execute("PRAGMA journal_mode").fetchone()
        current_mode = row[0] if row else None
        logger.debug("[RANKING SUMMARY DB] journal_mode current=%s", current_mode)
    except Exception:
        logger.debug("[RANKING SUMMARY DB] PRAGMA journal_mode read skipped", exc_info=True)

    try:
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("PRAGMA locking_mode=NORMAL")
    except Exception:
        logger.warning("[RANKING SUMMARY DB] PRAGMA setup partial failure", exc_info=True)

    return con


def _begin_immediate(con: sqlite3.Connection) -> None:
    con.execute("BEGIN IMMEDIATE")


def _rollback_quietly(con: Optional[sqlite3.Connection]) -> None:
    if con is None:
        return
    try:
        con.execute("ROLLBACK")
    except Exception:
        pass


def _commit_or_raise(con: sqlite3.Connection) -> None:
    con.execute("COMMIT")


def _close_quietly(con: Optional[sqlite3.Connection]) -> None:
    if con is None:
        return
    try:
        con.close()
    except Exception:
        pass


def _quote_ident(name: str) -> str:
    """
    SQLite識別子を安全にクォートする。
    """
    s = str(name).replace('"', '""')
    return f'"{s}"'


def _table_name(interval: int) -> str:
    interval = int(interval)
    if interval not in (1, 3, 5):
        raise ValueError(f"unsupported interval: {interval}")
    return f"ranking_summary_{interval}min"


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
        row = con.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type='table'
               AND name=?
             LIMIT 1
            """,
            (table,),
        ).fetchone()
        return row is not None
    except Exception:
        logger.exception("[RANKING SUMMARY TABLE] table_exists failed table=%s", table)
        return False


def _index_exists(con: sqlite3.Connection, index_name: str) -> bool:
    try:
        row = con.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type='index'
               AND name=?
             LIMIT 1
            """,
            (index_name,),
        ).fetchone()
        return row is not None
    except Exception:
        logger.exception("[RANKING SUMMARY TABLE] index_exists failed index=%s", index_name)
        return False


def _get_existing_columns(con: sqlite3.Connection, table: str) -> Dict[str, Dict[str, Any]]:
    """
    PRAGMA table_info から既存列を取得する。
    """
    cols: Dict[str, Dict[str, Any]] = {}
    try:
        if not _table_exists(con, table):
            return cols

        rows = con.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
        for row in rows:
            cols[str(row[1])] = {
                "cid": row[0],
                "name": row[1],
                "type": row[2],
                "notnull": row[3],
                "dflt_value": row[4],
                "pk": row[5],
            }
    except Exception:
        logger.warning(
            "[RANKING SUMMARY TABLE] get columns failed table=%s",
            table,
            exc_info=True,
        )
    return cols


def _dedupe_ranking_summary_table(con: sqlite3.Connection, table: str) -> int:
    """
    ranking_summary_* の既存重複を削除する。
    symbol + datetime が同じ行は rowid が最大のものだけ残す。
    """
    try:
        if not _table_exists(con, table):
            return 0

        before = con.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0]

        dup_count = con.execute(
            f"""
            SELECT COUNT(*)
              FROM (
                    SELECT symbol, datetime, COUNT(*) AS cnt
                      FROM {_quote_ident(table)}
                     WHERE symbol IS NOT NULL
                       AND datetime IS NOT NULL
                       AND TRIM(CAST(symbol AS TEXT)) <> ''
                       AND TRIM(CAST(datetime AS TEXT)) <> ''
                     GROUP BY symbol, datetime
                    HAVING COUNT(*) > 1
                   )
            """
        ).fetchone()[0]

        if int(dup_count) <= 0:
            return 0

        con.execute(
            f"""
            DELETE FROM {_quote_ident(table)}
             WHERE rowid NOT IN (
                    SELECT MAX(rowid)
                      FROM {_quote_ident(table)}
                     WHERE symbol IS NOT NULL
                       AND datetime IS NOT NULL
                       AND TRIM(CAST(symbol AS TEXT)) <> ''
                       AND TRIM(CAST(datetime AS TEXT)) <> ''
                     GROUP BY symbol, datetime
             )
               AND symbol IS NOT NULL
               AND datetime IS NOT NULL
               AND TRIM(CAST(symbol AS TEXT)) <> ''
               AND TRIM(CAST(datetime AS TEXT)) <> ''
            """
        )

        after = con.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0]
        deleted = int(before) - int(after)

        if deleted > 0:
            logger.warning(
                "[RANKING SUMMARY TABLE] dedupe done table=%s duplicate_keys=%s before=%s after=%s deleted=%s",
                table,
                dup_count,
                before,
                after,
                deleted,
            )
        return deleted

    except Exception:
        logger.exception("[RANKING SUMMARY TABLE] dedupe failed table=%s", table)
        return 0


def _delete_null_key_rows(con: sqlite3.Connection, table: str) -> int:
    """
    symbol/datetime が欠損している行を削除する。
    cleanup 失敗は保存本体を止めない。
    """
    try:
        if not _table_exists(con, table):
            return 0

        before = con.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0]

        con.execute(
            f"""
            DELETE FROM {_quote_ident(table)}
             WHERE symbol IS NULL
                OR datetime IS NULL
                OR TRIM(CAST(symbol AS TEXT)) = ''
                OR TRIM(CAST(datetime AS TEXT)) = ''
            """
        )

        after = con.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0]
        deleted = int(before) - int(after)

        if deleted > 0:
            logger.warning(
                "[RANKING SUMMARY TABLE] null-key rows deleted table=%s before=%s after=%s deleted=%s",
                table,
                before,
                after,
                deleted,
            )

        return deleted

    except Exception:
        logger.warning(
            "[RANKING SUMMARY TABLE] null-key cleanup skipped table=%s",
            table,
            exc_info=True,
        )
        return 0


# ============================================================
# schema definition
# ============================================================

RANKING_SUMMARY_COLUMNS: List[tuple[str, str]] = [
    ("symbol", "TEXT NOT NULL"),
    ("symbolname", "TEXT"),
    ("datetime", "TEXT NOT NULL"),
    ("date", "TEXT"),
    ("time", "TEXT"),
    ("time_range", "TEXT"),

    ("open", "REAL"),
    ("high", "REAL"),
    ("low", "REAL"),
    ("close", "REAL"),
    ("volume", "REAL DEFAULT 0"),

    ("open_price", "REAL"),
    ("high_price", "REAL"),
    ("low_price", "REAL"),
    ("close_price", "REAL"),
    ("current_price", "REAL"),

    ("ranking_type", "TEXT"),
    ("rank", "REAL"),
    ("best_rank", "REAL"),
    ("hit_count", "REAL"),
    ("hist", "REAL"),
    ("change_percentage", "REAL"),
    ("trading_volume", "REAL"),
    ("trading_value", "REAL"),
    ("turnover", "REAL"),
    ("tick_count", "REAL"),

    ("ma5", "REAL"),
    ("ma25", "REAL"),
    ("ma75", "REAL"),
    ("rsi", "REAL"),
    ("rsi_slope", "REAL"),
    ("macd", "REAL"),
    ("signal", "REAL"),
    ("macd_signal", "REAL"),
    ("macd_hist", "REAL"),
    ("macd_hist_slope", "REAL"),
    ("slope", "REAL"),
    ("slope_atr_scaled", "REAL"),
    ("mtf", "REAL"),
    ("score_mtf", "REAL"),
    ("mtf_score", "REAL"),

    ("flag_macd_cross", "INTEGER"),
    ("flag_macd_hist_expand", "INTEGER"),
    ("flag_rsi_rebound", "INTEGER"),
    ("flag_rsi_midline_cross", "INTEGER"),
    ("flag_macd_dc", "INTEGER"),
    ("flag_macd_hist_contract", "INTEGER"),
    ("flag_rsi_falling", "INTEGER"),
    ("flag_rsi_overbought_70", "INTEGER"),

    ("score", "REAL"),
    ("score_buy", "REAL"),
    ("score_sell", "REAL"),
    ("score_total", "REAL"),
    ("final_score", "REAL"),
    ("display_score", "REAL"),
    ("disp_score", "REAL"),
    ("score_slope", "REAL"),

    ("base", "REAL"),
    ("trend", "REAL"),
    ("mom", "REAL"),
    ("vel", "REAL"),
    ("pen", "REAL"),

    ("interval", "INTEGER"),
    ("source", "TEXT"),
    ("price_source", "TEXT"),
    ("mode", "TEXT"),
    ("updated_at", "TEXT"),
]


def _column_type_map() -> Dict[str, str]:
    return dict(RANKING_SUMMARY_COLUMNS)


def _create_table_sql(table: str) -> str:
    col_lines = []
    for name, typ in RANKING_SUMMARY_COLUMNS:
        col_lines.append(f"            {_quote_ident(name)} {typ}")

    cols_sql = ",\n".join(col_lines)

    return f"""
        CREATE TABLE IF NOT EXISTS {_quote_ident(table)} (
{cols_sql},

            PRIMARY KEY (symbol, datetime)
        )
    """


def _add_missing_columns(con: sqlite3.Connection, table: str) -> None:
    """
    既存 ranking_summary_* に足りない列を追加する。
    既存列の型変更はしない。
    """
    if not _table_exists(con, table):
        return

    existing = _get_existing_columns(con, table)
    expected = _column_type_map()

    added = 0
    for col, typ in expected.items():
        if col in existing:
            continue

        add_typ = typ
        if "NOT NULL" in add_typ.upper() and "DEFAULT" not in add_typ.upper():
            add_typ = add_typ.upper().replace(" NOT NULL", "")

        try:
            con.execute(
                f"""
                ALTER TABLE {_quote_ident(table)}
                ADD COLUMN {_quote_ident(col)} {add_typ}
                """
            )
            added += 1
            logger.warning(
                "[RANKING SUMMARY TABLE] added missing column table=%s column=%s type=%s",
                table,
                col,
                add_typ,
            )
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                continue
            logger.warning(
                "[RANKING SUMMARY TABLE] add column failed table=%s column=%s err=%s",
                table,
                col,
                e,
                exc_info=True,
            )

    if added > 0:
        logger.warning(
            "[RANKING SUMMARY TABLE] schema upgraded table=%s added_columns=%s",
            table,
            added,
        )


# ============================================================
# schema
# ============================================================

def ensure_ranking_summary_table(
    con: sqlite3.Connection,
    *,
    interval: int,
) -> None:
    table = _table_name(interval)

    con.execute(_create_table_sql(table))
    _add_missing_columns(con, table)

    _delete_null_key_rows(con, table)
    _dedupe_ranking_summary_table(con, table)

    index_name = f"uq_{table}_symbol_datetime"

    if not _index_exists(con, index_name):
        try:
            con.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    {_quote_ident(index_name)}
                ON {_quote_ident(table)}(symbol, datetime)
                """
            )
            logger.info(
                "[RANKING SUMMARY TABLE] unique index ensured table=%s index=%s",
                table,
                index_name,
            )
        except sqlite3.IntegrityError:
            logger.warning(
                "[RANKING SUMMARY TABLE] unique index retry after dedupe table=%s",
                table,
                exc_info=True,
            )
            _delete_null_key_rows(con, table)
            _dedupe_ranking_summary_table(con, table)
            con.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    {_quote_ident(index_name)}
                ON {_quote_ident(table)}(symbol, datetime)
                """
            )
            logger.info(
                "[RANKING SUMMARY TABLE] unique index ensured after retry table=%s index=%s",
                table,
                index_name,
            )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS
            {_quote_ident(f"idx_{table}_datetime")}
        ON {_quote_ident(table)}(datetime)
        """
    )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS
            {_quote_ident(f"idx_{table}_score")}
        ON {_quote_ident(table)}(display_score)
        """
    )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS
            {_quote_ident(f"idx_{table}_symbol_datetime")}
        ON {_quote_ident(table)}(symbol, datetime)
        """
    )

    con.execute(
        f"""
        CREATE INDEX IF NOT EXISTS
            {_quote_ident(f"idx_{table}_ranking_type")}
        ON {_quote_ident(table)}(ranking_type)
        """
    )


def ensure_all_ranking_summary_tables(
    *,
    trade_date=None,
    db_path: Optional[str] = None,
    ranking_dir: str = DEFAULT_RANKING_DIR,
) -> None:
    """
    起動時などに 1min/3min/5min の全テーブルを作成・スキーマ更新する補助関数。
    """
    path = db_path or get_ranking_db_path(trade_date, ranking_dir=ranking_dir)

    last_err = None
    for attempt in range(1, MAX_SAVE_RETRY + 1):
        con: Optional[sqlite3.Connection] = None
        try:
            with _RANKING_SUMMARY_DB_LOCK:
                con = _connect(path)
                _begin_immediate(con)

                for interval in (1, 3, 5):
                    ensure_ranking_summary_table(con, interval=interval)

                _commit_or_raise(con)
                _close_quietly(con)
                con = None

            logger.info("[RANKING SUMMARY TABLE] all tables ensured path=%s attempt=%s", path, attempt)
            return

        except sqlite3.OperationalError as e:
            last_err = e
            _rollback_quietly(con)
            if _is_sqlite_locked_error(e) and attempt < MAX_SAVE_RETRY:
                slept = _retry_sleep(attempt)
                logger.warning(
                    "[RANKING SUMMARY TABLE] ensure_all locked retry path=%s attempt=%s/%s sleep=%.2fs err=%s",
                    path,
                    attempt,
                    MAX_SAVE_RETRY,
                    slept,
                    e,
                )
                continue
            logger.exception("[RANKING SUMMARY TABLE] ensure_all failed path=%s", path)

        except Exception as e:
            last_err = e
            _rollback_quietly(con)
            logger.exception("[RANKING SUMMARY TABLE] ensure_all failed path=%s", path)

        finally:
            _close_quietly(con)

        break

    logger.error("[RANKING SUMMARY TABLE] ensure_all failed after retries path=%s last_err=%s", path, last_err)


# ============================================================
# normalize helpers
# ============================================================

def _first_existing_col(df: pd.DataFrame, names: list[str]) -> Optional[str]:
    for name in names:
        if name in df.columns:
            return name
    return None


def _ensure_col_from_alias(
    df: pd.DataFrame,
    target: str,
    aliases: list[str],
    default=None,
) -> None:
    if target in df.columns:
        return

    src = _first_existing_col(df, aliases)
    if src is not None:
        df[target] = df[src]
    else:
        df[target] = default


def _fill_missing_from_alias(
    df: pd.DataFrame,
    target: str,
    aliases: list[str],
) -> None:
    if target not in df.columns:
        df[target] = None

    for src in aliases:
        if src not in df.columns or src == target:
            continue
        try:
            df[target] = df[target].where(df[target].notna(), df[src])
        except Exception:
            pass


def _coerce_datetime_series(s: pd.Series) -> pd.Series:
    out = pd.to_datetime(s, errors="coerce")

    try:
        out = out.dt.tz_localize(None)
    except Exception:
        try:
            out = out.dt.tz_convert(None)
        except Exception:
            pass

    return out


def _safe_numeric(df: pd.DataFrame, cols: list[str]) -> None:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")


def _safe_text(df: pd.DataFrame, cols: list[str]) -> None:
    for c in cols:
        if c in df.columns:
            df[c] = df[c].fillna("").astype(str)


def _safe_int_flags(df: pd.DataFrame, cols: list[str]) -> None:
    for c in cols:
        if c not in df.columns:
            df[c] = 0
        try:
            df[c] = df[c].fillna(False).astype(bool).astype(int)
        except Exception:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)


def _add_date_time_columns(df: pd.DataFrame) -> None:
    if "datetime" not in df.columns:
        return

    dt_s = pd.to_datetime(df["datetime"], errors="coerce")

    if "date" not in df.columns:
        df["date"] = dt_s.dt.strftime("%Y-%m-%d")

    if "time" not in df.columns:
        df["time"] = dt_s.dt.strftime("%H:%M:%S")

    if "time_range" not in df.columns:
        df["time_range"] = df["time"]


def _normalize_for_save(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if "symbol" not in out.columns:
        logger.warning("[RANKING SUMMARY SAVE] no symbol column interval=%s", interval)
        return pd.DataFrame()

    if "datetime" not in out.columns:
        logger.warning("[RANKING SUMMARY SAVE] no datetime column interval=%s", interval)
        return pd.DataFrame()

    _ensure_col_from_alias(out, "symbolname", ["name", "symbol_name", "SymbolName"], "")

    _ensure_col_from_alias(
        out,
        "current_price",
        ["close", "close_price", "price", "last_price", "CurrentPrice"],
        None,
    )
    _ensure_col_from_alias(
        out,
        "close",
        ["close_price", "current_price", "price", "last_price", "CurrentPrice"],
        None,
    )
    _ensure_col_from_alias(out, "open", ["open_price", "close", "current_price"], None)
    _ensure_col_from_alias(out, "high", ["high_price", "close", "current_price"], None)
    _ensure_col_from_alias(out, "low", ["low_price", "close", "current_price"], None)

    _ensure_col_from_alias(out, "open_price", ["open", "close", "current_price"], None)
    _ensure_col_from_alias(out, "high_price", ["high", "close", "current_price"], None)
    _ensure_col_from_alias(out, "low_price", ["low", "close", "current_price"], None)
    _ensure_col_from_alias(out, "close_price", ["close", "current_price"], None)

    _fill_missing_from_alias(out, "close", ["close_price", "current_price", "price", "last_price"])
    _fill_missing_from_alias(out, "current_price", ["close", "close_price"])
    _fill_missing_from_alias(out, "close_price", ["close", "current_price"])

    for price_col in ["open", "high", "low", "open_price", "high_price", "low_price"]:
        _fill_missing_from_alias(out, price_col, ["close", "close_price", "current_price"])

    _ensure_col_from_alias(out, "volume", ["vol", "Volume", "volume_1m"], 0)

    _ensure_col_from_alias(out, "signal", ["macd_signal"], None)
    _ensure_col_from_alias(out, "macd_signal", ["signal"], None)
    _fill_missing_from_alias(out, "signal", ["macd_signal"])
    _fill_missing_from_alias(out, "macd_signal", ["signal"])

    _ensure_col_from_alias(out, "score_mtf", ["mtf_score"], None)
    _ensure_col_from_alias(out, "mtf_score", ["score_mtf"], None)
    _fill_missing_from_alias(out, "score_mtf", ["mtf_score"])
    _fill_missing_from_alias(out, "mtf_score", ["score_mtf"])

    _ensure_col_from_alias(
        out,
        "display_score",
        ["disp_score", "final_score", "score_total", "score"],
        None,
    )
    _ensure_col_from_alias(
        out,
        "disp_score",
        ["display_score", "final_score", "score_total", "score"],
        None,
    )
    _ensure_col_from_alias(
        out,
        "final_score",
        ["score_total", "display_score", "disp_score", "score"],
        None,
    )
    _ensure_col_from_alias(
        out,
        "score_total",
        ["final_score", "display_score", "disp_score", "score"],
        None,
    )
    _ensure_col_from_alias(out, "score_buy", ["buy_score", "disp_buy_score"], None)
    _ensure_col_from_alias(out, "score_sell", ["sell_score", "disp_sell_score"], None)
    _ensure_col_from_alias(out, "score_slope", ["slope_score"], None)

    _ensure_col_from_alias(out, "best_rank", ["rank"], None)
    _ensure_col_from_alias(out, "hit_count", ["hist"], None)
    _ensure_col_from_alias(out, "hist", ["hit_count"], None)

    out["datetime"] = _coerce_datetime_series(out["datetime"])
    out = out.dropna(subset=["symbol", "datetime"])

    if out.empty:
        return pd.DataFrame()

    out["symbol"] = (
        out["symbol"]
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )
    out = out[out["symbol"] != ""]

    if out.empty:
        return pd.DataFrame()

    _add_date_time_columns(out)

    out["datetime"] = out["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")

    defaults: Dict[str, Any] = {
        "symbolname": "",
        "date": "",
        "time": "",
        "time_range": "",

        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "volume": 0,

        "open_price": None,
        "high_price": None,
        "low_price": None,
        "close_price": None,
        "current_price": None,

        "ranking_type": "",
        "rank": None,
        "best_rank": None,
        "hit_count": None,
        "hist": None,
        "change_percentage": None,
        "trading_volume": None,
        "trading_value": None,
        "turnover": None,
        "tick_count": None,

        "ma5": None,
        "ma25": None,
        "ma75": None,
        "rsi": None,
        "rsi_slope": None,
        "macd": None,
        "signal": None,
        "macd_signal": None,
        "macd_hist": None,
        "macd_hist_slope": None,
        "slope": None,
        "slope_atr_scaled": None,
        "mtf": None,
        "score_mtf": None,
        "mtf_score": None,

        "flag_macd_cross": 0,
        "flag_macd_hist_expand": 0,
        "flag_rsi_rebound": 0,
        "flag_rsi_midline_cross": 0,
        "flag_macd_dc": 0,
        "flag_macd_hist_contract": 0,
        "flag_rsi_falling": 0,
        "flag_rsi_overbought_70": 0,

        "score": None,
        "score_buy": None,
        "score_sell": None,
        "score_total": None,
        "final_score": None,
        "display_score": None,
        "disp_score": None,
        "score_slope": None,

        "base": None,
        "trend": None,
        "mom": None,
        "vel": None,
        "pen": None,

        "interval": int(interval),
        "source": "ranking",
        "price_source": "",
        "mode": "",
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    for c, default in defaults.items():
        if c not in out.columns:
            out[c] = default

    numeric_cols = [
        "open", "high", "low", "close", "volume",
        "open_price", "high_price", "low_price", "close_price", "current_price",
        "rank", "best_rank", "hit_count", "hist", "change_percentage",
        "trading_volume", "trading_value", "turnover", "tick_count",
        "ma5", "ma25", "ma75", "rsi", "rsi_slope",
        "macd", "signal", "macd_signal", "macd_hist", "macd_hist_slope",
        "slope", "slope_atr_scaled", "mtf", "score_mtf", "mtf_score",
        "score", "score_buy", "score_sell", "score_total",
        "final_score", "display_score", "disp_score", "score_slope",
        "base", "trend", "mom", "vel", "pen",
    ]
    _safe_numeric(out, numeric_cols)

    text_cols = [
        "symbolname", "date", "time", "time_range",
        "ranking_type", "source", "price_source", "mode", "updated_at",
    ]
    _safe_text(out, text_cols)

    flag_cols = [
        "flag_macd_cross",
        "flag_macd_hist_expand",
        "flag_rsi_rebound",
        "flag_rsi_midline_cross",
        "flag_macd_dc",
        "flag_macd_hist_contract",
        "flag_rsi_falling",
        "flag_rsi_overbought_70",
    ]
    _safe_int_flags(out, flag_cols)

    out["interval"] = int(interval)
    out["source"] = out["source"].replace("", "ranking")
    out["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for c in ["close", "close_price", "current_price"]:
        _fill_missing_from_alias(out, c, ["close", "close_price", "current_price"])

    for c in ["open", "high", "low", "open_price", "high_price", "low_price"]:
        _fill_missing_from_alias(out, c, ["close", "close_price", "current_price"])

    _fill_missing_from_alias(out, "signal", ["macd_signal"])
    _fill_missing_from_alias(out, "macd_signal", ["signal"])

    _fill_missing_from_alias(out, "display_score", ["disp_score", "final_score", "score_total", "score"])
    _fill_missing_from_alias(out, "disp_score", ["display_score"])
    _fill_missing_from_alias(out, "final_score", ["score_total", "display_score", "score"])
    _fill_missing_from_alias(out, "score_total", ["final_score", "display_score", "score"])

    save_cols = [name for name, _typ in RANKING_SUMMARY_COLUMNS]

    for c in save_cols:
        if c not in out.columns:
            out[c] = defaults.get(c, None)

    out = out[save_cols]

    before = len(out)
    out = out.drop_duplicates(["symbol", "datetime"], keep="last")
    after = len(out)

    if before != after:
        logger.warning(
            "[RANKING SUMMARY SAVE] input dedupe interval=%s before=%s after=%s deleted=%s",
            interval,
            before,
            after,
            before - after,
        )

    price_missing = out["close"].isna().sum() if "close" in out.columns else len(out)
    if price_missing > 0:
        logger.warning(
            "[RANKING SUMMARY SAVE] close missing rows interval=%s missing=%s total=%s",
            interval,
            int(price_missing),
            len(out),
        )

    return out.reset_index(drop=True)


# ============================================================
# save
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
    cleanup / dedupe / schema alter / upsert は同じ connection / 同じ transaction で行う。
    """
    if df is None or df.empty:
        logger.info("[RANKING SUMMARY SAVE] skip empty interval=%s", interval)
        return 0

    interval = int(interval)
    table = _table_name(interval)
    path = db_path or get_ranking_db_path(trade_date, ranking_dir=ranking_dir)

    save_df = _normalize_for_save(df, interval=interval)
    if save_df.empty:
        logger.info("[RANKING SUMMARY SAVE] normalized empty interval=%s", interval)
        return 0

    cols = list(save_df.columns)
    placeholders = ",".join(["?"] * len(cols))
    col_sql = ",".join([_quote_ident(c) for c in cols])

    update_cols = [c for c in cols if c not in ("symbol", "datetime")]
    update_sql = ",".join([f"{_quote_ident(c)}=excluded.{_quote_ident(c)}" for c in update_cols])

    sql = f"""
        INSERT INTO {_quote_ident(table)} ({col_sql})
        VALUES ({placeholders})
        ON CONFLICT(symbol, datetime)
        DO UPDATE SET {update_sql}
    """

    rows = [tuple(row) for row in save_df.itertuples(index=False, name=None)]
    last_err: BaseException | None = None

    for attempt in range(1, MAX_SAVE_RETRY + 1):
        con: Optional[sqlite3.Connection] = None
        try:
            with _RANKING_SUMMARY_DB_LOCK:
                con = _connect(path)
                _begin_immediate(con)

                ensure_ranking_summary_table(con, interval=interval)

                _delete_null_key_rows(con, table)
                _dedupe_ranking_summary_table(con, table)

                con.executemany(sql, rows)

                # ここは quiet にしない。COMMIT失敗は保存失敗として扱う。
                _commit_or_raise(con)

                _close_quietly(con)
                con = None

            logger.info(
                "[RANKING SUMMARY SAVE] saved table=%s rows=%s symbols=%s dt_min=%s dt_max=%s attempt=%s/%s cols=%s",
                table,
                len(save_df),
                save_df["symbol"].nunique(),
                save_df["datetime"].min(),
                save_df["datetime"].max(),
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
                _retry_sleep(attempt)

        except sqlite3.OperationalError as e:
            last_err = e
            _rollback_quietly(con)

            if _is_sqlite_locked_error(e) and attempt < MAX_SAVE_RETRY:
                slept = _retry_sleep(attempt)
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
                _retry_sleep(attempt)

        except Exception as e:
            last_err = e
            _rollback_quietly(con)

            if _is_sqlite_locked_error(e) and attempt < MAX_SAVE_RETRY:
                slept = _retry_sleep(attempt)
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
                "[RANKING SUMMARY SAVE] failed table=%s rows=%s path=%s attempt=%s/%s",
                table,
                len(save_df),
                path,
                attempt,
                MAX_SAVE_RETRY,
            )
            if attempt < MAX_SAVE_RETRY:
                _retry_sleep(attempt)

        finally:
            _close_quietly(con)

    logger.error(
        "[RANKING SUMMARY SAVE] failed after retries table=%s rows=%s path=%s last_err=%s",
        table,
        len(save_df),
        path,
        last_err,
    )
    return 0


# ============================================================
# load
# ============================================================

def load_latest_ranking_summary(
    *,
    interval: int,
    trade_date=None,
    db_path: Optional[str] = None,
    ranking_dir: str = DEFAULT_RANKING_DIR,
    limit_minutes: int = 240,
) -> pd.DataFrame:
    """
    ranking_summary_* から直近データを読む。
    TOP10表示・確認用。
    """
    interval = int(interval)
    table = _table_name(interval)
    path = db_path or get_ranking_db_path(trade_date, ranking_dir=ranking_dir)

    if not os.path.exists(path):
        logger.warning("[RANKING SUMMARY LOAD] db not found path=%s", path)
        return pd.DataFrame()

    since_dt = dt.datetime.now() - dt.timedelta(minutes=int(limit_minutes))

    try:
        with _RANKING_SUMMARY_DB_LOCK:
            with sqlite3.connect(path, timeout=10) as con:
                con.execute("PRAGMA busy_timeout=10000")

                try:
                    ensure_ranking_summary_table(con, interval=interval)
                except sqlite3.OperationalError as e:
                    if _is_sqlite_locked_error(e):
                        logger.warning(
                            "[RANKING SUMMARY LOAD] ensure table skipped by locked table=%s err=%s",
                            table,
                            e,
                        )
                    else:
                        raise
                except Exception:
                    logger.warning(
                        "[RANKING SUMMARY LOAD] ensure table skipped table=%s",
                        table,
                        exc_info=True,
                    )

                exists = con.execute(
                    """
                    SELECT name FROM sqlite_master
                     WHERE type='table' AND name=?
                     LIMIT 1
                    """,
                    (table,),
                ).fetchone()

                if not exists:
                    logger.warning("[RANKING SUMMARY LOAD] table not found %s", table)
                    return pd.DataFrame()

                df = pd.read_sql_query(
                    f"""
                    SELECT *
                      FROM {_quote_ident(table)}
                     WHERE datetime >= ?
                     ORDER BY datetime ASC
                    """,
                    con,
                    params=[since_dt.strftime("%Y-%m-%d %H:%M:%S")],
                )

    except Exception:
        logger.exception("[RANKING SUMMARY LOAD] failed table=%s path=%s", table, path)
        return pd.DataFrame()

    if df.empty:
        return df

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df


def load_ranking_summary_at_latest_slot(
    *,
    interval: int,
    trade_date=None,
    db_path: Optional[str] = None,
    ranking_dir: str = DEFAULT_RANKING_DIR,
) -> pd.DataFrame:
    """
    ranking_summary_* の最新 datetime の行だけを読む。
    announce.py 側で fallback せず、summary DB の最新slotを読むための補助関数。
    """
    interval = int(interval)
    table = _table_name(interval)
    path = db_path or get_ranking_db_path(trade_date, ranking_dir=ranking_dir)

    if not os.path.exists(path):
        logger.warning("[RANKING SUMMARY LOAD SLOT] db not found path=%s", path)
        return pd.DataFrame()

    try:
        with _RANKING_SUMMARY_DB_LOCK:
            with sqlite3.connect(path, timeout=10) as con:
                con.execute("PRAGMA busy_timeout=10000")

                try:
                    ensure_ranking_summary_table(con, interval=interval)
                except sqlite3.OperationalError as e:
                    if _is_sqlite_locked_error(e):
                        logger.warning(
                            "[RANKING SUMMARY LOAD SLOT] ensure table skipped by locked table=%s err=%s",
                            table,
                            e,
                        )
                    else:
                        raise
                except Exception:
                    logger.warning(
                        "[RANKING SUMMARY LOAD SLOT] ensure table skipped table=%s",
                        table,
                        exc_info=True,
                    )

                exists = con.execute(
                    """
                    SELECT name FROM sqlite_master
                     WHERE type='table' AND name=?
                     LIMIT 1
                    """,
                    (table,),
                ).fetchone()

                if not exists:
                    logger.warning("[RANKING SUMMARY LOAD SLOT] table not found %s", table)
                    return pd.DataFrame()

                row = con.execute(
                    f"""
                    SELECT MAX(datetime)
                      FROM {_quote_ident(table)}
                     WHERE datetime IS NOT NULL
                       AND TRIM(CAST(datetime AS TEXT)) <> ''
                    """
                ).fetchone()

                latest_dt = row[0] if row else None
                if not latest_dt:
                    return pd.DataFrame()

                df = pd.read_sql_query(
                    f"""
                    SELECT *
                      FROM {_quote_ident(table)}
                     WHERE datetime = ?
                     ORDER BY display_score DESC, final_score DESC, score DESC
                    """,
                    con,
                    params=[latest_dt],
                )

    except Exception:
        logger.exception("[RANKING SUMMARY LOAD SLOT] failed table=%s path=%s", table, path)
        return pd.DataFrame()

    if df.empty:
        return df

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    return df


def get_ranking_summary_schema_columns(
    *,
    interval: int,
    trade_date=None,
    db_path: Optional[str] = None,
    ranking_dir: str = DEFAULT_RANKING_DIR,
) -> list[str]:
    """
    診断用: ranking_summary_* の列名一覧を返す。
    """
    interval = int(interval)
    table = _table_name(interval)
    path = db_path or get_ranking_db_path(trade_date, ranking_dir=ranking_dir)

    if not os.path.exists(path):
        return []

    try:
        with sqlite3.connect(path, timeout=10) as con:
            con.execute("PRAGMA busy_timeout=10000")
            if not _table_exists(con, table):
                return []
            cols = _get_existing_columns(con, table)
            return list(cols.keys())
    except Exception:
        logger.warning(
            "[RANKING SUMMARY SCHEMA] get columns failed table=%s path=%s",
            table,
            path,
            exc_info=True,
        )
        return []


# ============================================================
# manual test
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    ensure_all_ranking_summary_tables()
    print("ranking summary tables ensured.")