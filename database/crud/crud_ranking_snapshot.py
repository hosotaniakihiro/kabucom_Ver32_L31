# ============================================================
# File   : database/crud/crud_ranking_snapshot.py
# Version: PRODUCTION-STABLE-RANKING-SNAPSHOT-CRUD-V10-SCHEMA-CACHE
# ------------------------------------------------------------
# ✔ ranking_snapshot_1min へ毎分INSERT
# ✔ rows空時の安全化
# ✔ datetime / snapshot_time を minute 単位へ丸め
# ✔ symbol / rank / rank_type / market 正規化
# ✔ 同一 snapshot の軽い重複除去
# ✔ UNIQUE(symbol, datetime, ranking_type, market) 重複除去
# ✔ sqlite lock 時のみ軽い retry
# ✔ 保存件数ログ強化
# ✔ scheduler を落としにくい fail-safe
# ✔ ranking_snapshot_1min 不足列を自動追加
# ✔ rank_type / market / snapshot_time の NOT NULL 制約に対応
# ✔ 実テーブルの必須列不足を順次吸収しやすい構造
# ✔ INSERT OR REPLACE により同一分の再取得を最新値で上書き
# ✔ UNIQUE index 自動作成
# ✔ 既存重複データ cleanup
# ✔ NEW: schema / unique index ensure を DBファイル単位でキャッシュ
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Dict, Iterable, List, Tuple

import pandas as pd
from sqlalchemy import text

from database.session import get_ranking_engine

logger = logging.getLogger(__name__)


TABLE_NAME = "ranking_snapshot_1min"
UNIQUE_INDEX_NAME = "uq_ranking_snapshot_1min_symbol_datetime_type_market"

# schema ensure を毎回実行しないためのキャッシュ
# key は SQLite DB の実ファイルパス
_SCHEMA_ENSURED_DB_KEYS: set[str] = set()


# ============================================================
# helpers
# ============================================================

def _quote_ident(name: str) -> str:
    """
    SQLite identifier quote.
    """
    return '"' + str(name).replace('"', '""') + '"'


def _to_naive_datetime(v: Any) -> dt.datetime | None:
    """
    任意の datetime 入力を timezoneなし・分単位に丸めて返す。
    """
    if v is None or v == "":
        return None

    try:
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None

        try:
            if getattr(ts, "tzinfo", None) is not None:
                try:
                    ts = ts.tz_localize(None)
                except Exception:
                    ts = ts.tz_convert(None)
        except Exception:
            pass

        py_dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts

        if isinstance(py_dt, dt.datetime):
            return py_dt.replace(tzinfo=None, second=0, microsecond=0)

        if isinstance(py_dt, dt.date):
            return dt.datetime(
                py_dt.year,
                py_dt.month,
                py_dt.day,
            ).replace(second=0, microsecond=0)

        return None

    except Exception:
        return None


def _now_minute() -> dt.datetime:
    return dt.datetime.now().replace(second=0, microsecond=0, tzinfo=None)


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None

    try:
        if pd.isna(v):
            return None
    except Exception:
        pass

    try:
        x = float(v)
        if pd.isna(x):
            return None
        return x
    except Exception:
        return None


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None

    try:
        if pd.isna(v):
            return None
    except Exception:
        pass

    try:
        return int(float(v))
    except Exception:
        return None


def _is_lock_error(e: Exception) -> bool:
    s = str(e).lower()
    return (
        "database is locked" in s
        or "database table is locked" in s
        or "sqlite busy" in s
        or "database schema is locked" in s
    )


def _is_unique_error(e: Exception) -> bool:
    s = str(e).lower()
    return (
        "unique constraint failed" in s
        or "integrityerror" in s and "unique" in s
    )


def _chunked(rows: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    if size <= 0:
        yield rows
        return

    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def _safe_len(v: Any) -> int:
    try:
        return len(v)
    except Exception:
        return 0


# ============================================================
# schema bootstrap
# ============================================================

_REQUIRED_COLUMNS: dict[str, str] = {
    "symbol": "TEXT",
    "symbolname": "TEXT",
    "rank": "INTEGER",
    "rank_type": "TEXT",
    "market": "TEXT",
    "price": "REAL",
    "change_rate": "REAL",
    "volume": "REAL",
    "turnover": "REAL",
    "category": "TEXT",
    "ranking_type": "TEXT",
    "snapshot_time": "TIMESTAMP",
    "datetime": "TIMESTAMP",
}


def _get_existing_columns(conn, table_name: str) -> set[str]:
    cols: set[str] = set()

    try:
        rows = conn.execute(
            text(f"PRAGMA table_info({_quote_ident(table_name)})")
        ).fetchall()

        for row in rows:
            try:
                cols.add(str(row[1]))
            except Exception:
                pass

    except Exception:
        logger.exception(
            "[RANKING SNAPSHOT][SCHEMA] get existing columns failed table=%s",
            table_name,
        )

    return cols


def _table_exists(conn, table_name: str) -> bool:
    try:
        row = conn.execute(
            text(
                """
                SELECT name
                  FROM sqlite_master
                 WHERE type='table'
                   AND name=:name
                 LIMIT 1
                """
            ),
            {"name": table_name},
        ).fetchone()
        return row is not None
    except Exception:
        logger.exception(
            "[RANKING SNAPSHOT][SCHEMA] table_exists failed table=%s",
            table_name,
        )
        return False


def _index_exists(conn, index_name: str) -> bool:
    try:
        row = conn.execute(
            text(
                """
                SELECT name
                  FROM sqlite_master
                 WHERE type='index'
                   AND name=:name
                 LIMIT 1
                """
            ),
            {"name": index_name},
        ).fetchone()
        return row is not None
    except Exception:
        logger.exception(
            "[RANKING SNAPSHOT][SCHEMA] index_exists failed index=%s",
            index_name,
        )
        return False


def _ensure_table_and_columns(conn) -> None:
    """
    ranking_snapshot_1min を作成し、不足列を追加する。

    既存テーブルが id だけの状態でも順次吸収する。
    """
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {_quote_ident(TABLE_NAME)} (
                id INTEGER PRIMARY KEY AUTOINCREMENT
            )
            """
        )
    )

    existing = _get_existing_columns(conn, TABLE_NAME)

    for col, col_type in _REQUIRED_COLUMNS.items():
        if col in existing:
            continue

        try:
            conn.execute(
                text(
                    f"""
                    ALTER TABLE {_quote_ident(TABLE_NAME)}
                    ADD COLUMN {_quote_ident(col)} {col_type}
                    """
                )
            )
            logger.info(
                "[RANKING SNAPSHOT][SCHEMA] added column table=%s column=%s type=%s",
                TABLE_NAME,
                col,
                col_type,
            )
        except Exception:
            logger.exception(
                "[RANKING SNAPSHOT][SCHEMA] failed add column table=%s column=%s type=%s",
                TABLE_NAME,
                col,
                col_type,
            )


def _cleanup_duplicate_existing_rows(conn) -> int:
    """
    UNIQUE index 作成前に、既存重複行を削除する。

    残す行:
      - 同一(symbol, datetime, ranking_type, market) の中で rowid 最大
    """
    try:
        if not _table_exists(conn, TABLE_NAME):
            return 0

        existing = _get_existing_columns(conn, TABLE_NAME)
        required = {"symbol", "datetime", "ranking_type", "market"}
        if not required.issubset(existing):
            return 0

        before = conn.execute(
            text(f"SELECT COUNT(*) FROM {_quote_ident(TABLE_NAME)}")
        ).fetchone()[0]

        dup_count = conn.execute(
            text(
                f"""
                SELECT COUNT(*)
                  FROM (
                        SELECT symbol, datetime, ranking_type, market, COUNT(*) AS cnt
                          FROM {_quote_ident(TABLE_NAME)}
                         WHERE symbol IS NOT NULL
                           AND datetime IS NOT NULL
                           AND ranking_type IS NOT NULL
                           AND market IS NOT NULL
                           AND TRIM(CAST(symbol AS TEXT)) <> ''
                           AND TRIM(CAST(datetime AS TEXT)) <> ''
                           AND TRIM(CAST(ranking_type AS TEXT)) <> ''
                           AND TRIM(CAST(market AS TEXT)) <> ''
                         GROUP BY symbol, datetime, ranking_type, market
                        HAVING COUNT(*) > 1
                       )
                """
            )
        ).fetchone()[0]

        if int(dup_count) <= 0:
            return 0

        conn.execute(
            text(
                f"""
                DELETE FROM {_quote_ident(TABLE_NAME)}
                 WHERE rowid NOT IN (
                        SELECT MAX(rowid)
                          FROM {_quote_ident(TABLE_NAME)}
                         WHERE symbol IS NOT NULL
                           AND datetime IS NOT NULL
                           AND ranking_type IS NOT NULL
                           AND market IS NOT NULL
                           AND TRIM(CAST(symbol AS TEXT)) <> ''
                           AND TRIM(CAST(datetime AS TEXT)) <> ''
                           AND TRIM(CAST(ranking_type AS TEXT)) <> ''
                           AND TRIM(CAST(market AS TEXT)) <> ''
                         GROUP BY symbol, datetime, ranking_type, market
                 )
                   AND symbol IS NOT NULL
                   AND datetime IS NOT NULL
                   AND ranking_type IS NOT NULL
                   AND market IS NOT NULL
                   AND TRIM(CAST(symbol AS TEXT)) <> ''
                   AND TRIM(CAST(datetime AS TEXT)) <> ''
                   AND TRIM(CAST(ranking_type AS TEXT)) <> ''
                   AND TRIM(CAST(market AS TEXT)) <> ''
                """
            )
        )

        after = conn.execute(
            text(f"SELECT COUNT(*) FROM {_quote_ident(TABLE_NAME)}")
        ).fetchone()[0]

        deleted = int(before) - int(after)

        if deleted > 0:
            logger.warning(
                "[RANKING SNAPSHOT][SCHEMA] existing duplicate cleanup done duplicate_keys=%s before=%s after=%s deleted=%s",
                dup_count,
                before,
                after,
                deleted,
            )

        return deleted

    except Exception:
        logger.exception("[RANKING SNAPSHOT][SCHEMA] duplicate cleanup failed")
        return 0


def _ensure_unique_index(conn) -> None:
    """
    UNIQUE(symbol, datetime, ranking_type, market) を作成する。
    """
    try:
        _cleanup_duplicate_existing_rows(conn)

        conn.execute(
            text(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS {_quote_ident(UNIQUE_INDEX_NAME)}
                ON {_quote_ident(TABLE_NAME)} (
                    symbol,
                    datetime,
                    ranking_type,
                    market
                )
                """
            )
        )

        logger.info(
            "[RANKING SNAPSHOT][SCHEMA] unique index ensured index=%s columns=symbol,datetime,ranking_type,market",
            UNIQUE_INDEX_NAME,
        )

    except Exception:
        logger.exception(
            "[RANKING SNAPSHOT][SCHEMA] unique index ensure failed index=%s",
            UNIQUE_INDEX_NAME,
        )


def _schema_cache_key(conn) -> str:
    """
    SQLite DB の実ファイルパスを schema cache key にする。
    取得できない場合だけ connection id を使う。
    """
    try:
        row = conn.execute(text("PRAGMA database_list")).fetchone()
        if row is not None:
            # SQLite: seq, name, file
            try:
                db_file = row[2]
            except Exception:
                db_file = None

            if db_file:
                return str(db_file)
    except Exception:
        pass

    return f"conn:{id(conn)}"


def _ensure_schema(conn) -> None:
    """
    ranking_snapshot_1min の schema を保証する。

    毎分・カテゴリごとに何度も UNIQUE index ensure が走ると重いため、
    DBファイル単位で1回だけ実行する。
    """
    key = _schema_cache_key(conn)

    if key in _SCHEMA_ENSURED_DB_KEYS:
        return

    _ensure_table_and_columns(conn)
    _ensure_unique_index(conn)

    _SCHEMA_ENSURED_DB_KEYS.add(key)


# ============================================================
# normalize
# ============================================================

def _normalize_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _normalize_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    入力rowsを ranking_snapshot_1min 保存用に正規化する。

    軽い重複除去:
      - symbol, type, market, snapshot_time, rank が完全一致するものは除去

    注意:
      - UNIQUE単位の symbol, datetime, ranking_type, market 重複は、
        この後 _dedupe_by_unique_key で後勝ち処理する。
    """
    src_count = len(rows or [])
    out: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str, dt.datetime, int]] = set()

    for r in rows or []:
        if not isinstance(r, dict):
            continue

        symbol = _normalize_symbol(r.get("symbol"))
        if not symbol:
            continue

        rank = _to_int(r.get("rank"))
        if rank is None:
            rank = _to_int(r.get("rank_position"))
        if rank is None:
            continue

        snapshot_time = _to_naive_datetime(r.get("snapshot_time"))
        if snapshot_time is None:
            snapshot_time = _to_naive_datetime(r.get("datetime"))
        if snapshot_time is None:
            snapshot_time = _now_minute()

        datetime_v = _to_naive_datetime(r.get("datetime"))
        if datetime_v is None:
            datetime_v = snapshot_time

        type_name = str(
            r.get("rank_type")
            or r.get("ranking_type")
            or r.get("category")
            or ""
        ).strip()
        if not type_name:
            type_name = "不明"

        market = str(r.get("market") or r.get("exchange") or "ALL").strip()
        if not market:
            market = "ALL"

        dedup_key = (symbol, type_name, market, snapshot_time, rank)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        symbolname = str(
            r.get("symbolname")
            or r.get("name")
            or r.get("symbol_name")
            or ""
        ).strip()

        price = _to_float(r.get("price", r.get("current_price")))
        change_rate = _to_float(
            r.get(
                "change_rate",
                r.get("change_percentage", r.get("change_ratio")),
            )
        )
        volume = _to_float(r.get("volume", r.get("trading_volume")))
        turnover = _to_float(r.get("turnover", r.get("trading_value")))

        row = {
            "symbol": symbol,
            "symbolname": symbolname,
            "rank": rank,
            "rank_type": type_name,
            "market": market,
            "price": price,
            "change_rate": change_rate,
            "volume": volume,
            "turnover": turnover,
            "category": str(r.get("category") or type_name).strip(),
            "ranking_type": str(r.get("ranking_type") or type_name).strip(),
            "snapshot_time": snapshot_time,
            "datetime": datetime_v,
        }

        out.append(row)

    if out:
        logger.info(
            "[RANKING SNAPSHOT] normalize rows=%s -> %s symbols=%s dt_min=%s dt_max=%s",
            src_count,
            len(out),
            len({r["symbol"] for r in out}),
            min(r["snapshot_time"] for r in out),
            max(r["snapshot_time"] for r in out),
        )
    else:
        logger.warning("[RANKING SNAPSHOT] normalize rows=%s -> 0", src_count)

    return out


def _unique_key(row: Dict[str, Any]) -> Tuple[str, dt.datetime, str, str]:
    """
    DB UNIQUE と同じキーを作る。
    """
    return (
        str(row.get("symbol") or "").strip(),
        row.get("datetime"),
        str(row.get("ranking_type") or "").strip(),
        str(row.get("market") or "").strip(),
    )


def _dedupe_by_unique_key(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    UNIQUE(symbol, datetime, ranking_type, market) 単位で重複除去する。

    同一分・同一ランキング種別・同一市場に同じsymbolが複数ある場合、
    最後の行を採用する。

    今回の例:
      6702 富士通 rank=2
      6702 富士通 rank=6

    この場合、後から来た rank=6 を残す。
    """
    if not rows:
        return []

    deduped: Dict[Tuple[str, dt.datetime, str, str], Dict[str, Any]] = {}
    invalid = 0

    for r in rows:
        key = _unique_key(r)

        if not key[0] or key[1] is None or not key[2] or not key[3]:
            invalid += 1
            continue

        # 後勝ち
        deduped[key] = r

    out = list(deduped.values())

    removed = len(rows) - len(out) - invalid

    if removed > 0 or invalid > 0:
        logger.warning(
            "[RANKING SNAPSHOT] duplicate rows removed before insert removed=%s invalid=%s before=%s after=%s",
            removed,
            invalid,
            len(rows),
            len(out),
        )

    return out


# ============================================================
# insert
# ============================================================

def insert_ranking_snapshot_1min(
    rows: List[Dict[str, Any]],
    max_retries: int = 3,
    chunk_size: int = 300,
) -> int:
    """
    ranking_snapshot_1min へ保存する。

    保存キー:
      UNIQUE(symbol, datetime, ranking_type, market)

    同一キーが来た場合:
      INSERT OR REPLACE により最新行で置換する。
    """
    norm = _normalize_rows(rows)
    norm = _dedupe_by_unique_key(norm)

    if not norm:
        logger.warning("[RANKING SNAPSHOT] no normalized rows")
        return 0

    engine = get_ranking_engine()

    sql = text(
        f"""
        INSERT OR REPLACE INTO {_quote_ident(TABLE_NAME)} (
            symbol,
            symbolname,
            rank,
            rank_type,
            market,
            price,
            change_rate,
            volume,
            turnover,
            category,
            ranking_type,
            snapshot_time,
            datetime
        ) VALUES (
            :symbol,
            :symbolname,
            :rank,
            :rank_type,
            :market,
            :price,
            :change_rate,
            :volume,
            :turnover,
            :category,
            :ranking_type,
            :snapshot_time,
            :datetime
        )
        """
    )

    total_inserted = 0
    total_requested = len(norm)

    try:
        with engine.begin() as conn:
            conn.execute(text("PRAGMA busy_timeout=15000"))
            _ensure_schema(conn)
    except Exception:
        logger.exception("[RANKING SNAPSHOT][SCHEMA] ensure schema failed")

    for chunk_idx, chunk in enumerate(_chunked(norm, chunk_size), start=1):
        chunk = _dedupe_by_unique_key(chunk)

        if not chunk:
            logger.warning(
                "[RANKING SNAPSHOT] chunk skipped after dedupe chunk=%s",
                chunk_idx,
            )
            continue

        inserted_this_chunk = 0

        for attempt in range(1, max_retries + 1):
            try:
                with engine.begin() as conn:
                    conn.execute(text("PRAGMA busy_timeout=15000"))
                    _ensure_schema(conn)
                    result = conn.execute(sql, chunk)

                # INSERT OR REPLACE の rowcount は環境により -1/0 になり得るため、
                # 保存対象件数を成功件数として扱う。
                rc = getattr(result, "rowcount", None)
                if rc is None or int(rc) < 0:
                    inserted_this_chunk = len(chunk)
                else:
                    inserted_this_chunk = len(chunk)

                total_inserted += inserted_this_chunk

                logger.info(
                    "[RANKING SNAPSHOT] inserted/replaced chunk=%s rows=%s dt_min=%s dt_max=%s",
                    chunk_idx,
                    inserted_this_chunk,
                    min(r["snapshot_time"] for r in chunk),
                    max(r["snapshot_time"] for r in chunk),
                )
                break

            except Exception as e:
                if _is_lock_error(e) and attempt < max_retries:
                    sleep_s = 0.25 * attempt
                    logger.warning(
                        "[RANKING SNAPSHOT] lock retry chunk=%s attempt=%s/%s rows=%s sleep=%.2fs err=%s",
                        chunk_idx,
                        attempt,
                        max_retries,
                        len(chunk),
                        sleep_s,
                        e,
                    )
                    time.sleep(sleep_s)
                    continue

                if _is_unique_error(e):
                    logger.exception(
                        "[RANKING SNAPSHOT] unique error remained after dedupe chunk=%s rows=%s attempt=%s/%s",
                        chunk_idx,
                        len(chunk),
                        attempt,
                        max_retries,
                    )
                else:
                    logger.exception(
                        "[RANKING SNAPSHOT] insert failed chunk=%s rows=%s attempt=%s/%s",
                        chunk_idx,
                        len(chunk),
                        attempt,
                        max_retries,
                    )

                inserted_this_chunk = 0
                break

        if inserted_this_chunk == 0:
            logger.warning(
                "[RANKING SNAPSHOT] chunk skipped chunk=%s rows=%s",
                chunk_idx,
                len(chunk),
            )

    if total_inserted:
        logger.info(
            "[RANKING SNAPSHOT] inserted/replaced total=%s requested=%s db=%s",
            total_inserted,
            total_requested,
            str(engine.url),
        )
    else:
        logger.warning(
            "[RANKING SNAPSHOT] inserted/replaced total=0 requested=%s db=%s",
            total_requested,
            str(engine.url),
        )

    return total_inserted


# ============================================================
# compat aliases
# ============================================================

def save_ranking_snapshot_1min(
    rows: List[Dict[str, Any]],
    max_retries: int = 3,
    chunk_size: int = 300,
) -> int:
    return insert_ranking_snapshot_1min(
        rows,
        max_retries=max_retries,
        chunk_size=chunk_size,
    )


__all__ = [
    "insert_ranking_snapshot_1min",
    "save_ranking_snapshot_1min",
]