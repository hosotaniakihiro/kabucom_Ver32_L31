# ============================================================
# File   : database/upsert/ranking_raw_upsert.py
# Version: PRODUCTION-STABLE-REV1.0-RANKING-RAW-UPSERT
# ------------------------------------------------------------
# 【概要】
#   ranking_raw_1min 保存。
#
# 【目的】
#   - ranking_raw_1min に raw ranking rows を保存する
#   - 既存DBに datetime 等が無い場合でも schema patch で補完
#   - SQLite lock retry
#   - dict / tuple の両方を受ける
#   - raw は履歴なので UNIQUE upsert ではなく INSERT OR IGNORE
#
# Notes:
#   - snapshot は database/upsert/ranking_snapshot_upsert.py
#   - raw はこのファイルで担当
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

from database.paths.ranking_paths import resolve_ranking_db_path
from database.schema.ranking_raw_schema import (
    RAW_TABLE,
    ensure_ranking_raw_table,
    patch_ranking_raw_schema,
    delete_null_key_rows,
)
from database.sqlite import (
    is_lock_error,
    lock_sleep_seconds,
    prepare_sqlite_connection,
    quote_ident,
)

logger = logging.getLogger(__name__)

RETRY_COUNT = 5
CHUNK_SIZE = 500
ENABLE_COUNT_LOG = True


# ============================================================
# SQLite helpers
# ============================================================

def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        path,
        timeout=60,
        check_same_thread=False,
        isolation_level=None,
    )
    prepare_sqlite_connection(conn)
    return conn


def _begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def _commit(conn: sqlite3.Connection) -> None:
    conn.execute("COMMIT")


def _rollback_quietly(conn: sqlite3.Connection | None) -> None:
    if conn is None:
        return

    try:
        conn.execute("ROLLBACK")
    except Exception:
        pass


def _close_quietly(conn: sqlite3.Connection | None) -> None:
    if conn is None:
        return

    try:
        conn.close()
    except Exception:
        pass


# ============================================================
# normalize helpers
# ============================================================

def _to_text(v: Any, default: str = "") -> str:
    if v is None:
        return default

    try:
        s = str(v).strip()
    except Exception:
        return default

    return s if s else default


def _to_float(v: Any, default: float | None = None):
    try:
        if v is None:
            return default

        if isinstance(v, str):
            s = v.strip()
            if not s:
                return default

            s = (
                s.replace(",", "")
                 .replace("%", "")
                 .replace("％", "")
                 .replace("円", "")
                 .strip()
            )

            if s in ("-", "－", "—", "None", "nan", "NaN"):
                return default

            return float(s)

        return float(v)

    except Exception:
        return default


def _to_int(v: Any, default: int | None = None) -> int | None:
    try:
        if v is None:
            return default

        if isinstance(v, str):
            s = v.strip().replace(",", "")
            if not s or s in ("-", "－", "—", "None", "nan", "NaN"):
                return default
            return int(float(s))

        return int(float(v))

    except Exception:
        return default


def _first_non_empty(*values: Any, default: Any = None) -> Any:
    for v in values:
        if v is None:
            continue

        if isinstance(v, str) and not v.strip():
            continue

        return v

    return default


def _normalize_symbol(v: Any) -> str:
    s = _to_text(v)

    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            s = s2

    return s.strip()


def _normalize_market(v: Any) -> str:
    s = _to_text(v)
    return s if s else "ALL"


def _normalize_ranking_type(v: Any) -> str:
    s = _to_text(v)
    return s if s else "UNKNOWN"


def _parse_datetime_any(v: Any) -> dt.datetime | None:
    if v is None:
        return None

    if isinstance(v, dt.datetime):
        return v

    if isinstance(v, dt.date):
        return dt.datetime.combine(v, dt.time())

    s = str(v).strip()
    if not s:
        return None

    s = s.replace("T", " ").strip()

    if "+" in s:
        s = s.split("+", 1)[0].strip()

    if s.endswith("Z"):
        s = s[:-1].strip()

    if "." in s:
        s = s.split(".", 1)[0].strip()

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y%m%d",
    ):
        try:
            return dt.datetime.strptime(s, fmt)
        except Exception:
            pass

    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def normalize_datetime_text(v: Any, *, default_now: bool = False) -> str:
    d = _parse_datetime_any(v)

    if d is None and default_now:
        d = dt.datetime.now()

    if d is None:
        return ""

    return d.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _combine_date_time(date_value: Any, time_value: Any) -> dt.datetime | None:
    d_txt = _to_text(date_value)
    t_txt = _to_text(time_value)

    if not d_txt:
        return None

    if not t_txt:
        t_txt = "00:00:00"

    return _parse_datetime_any(f"{d_txt} {t_txt}")


def _resolve_row_datetime(row: dict[str, Any]) -> str:
    raw = _first_non_empty(
        row.get("datetime"),
        row.get("Datetime"),
        row.get("date_time"),
        row.get("DateTime"),
        row.get("snapshot_time"),
        row.get("SnapshotTime"),
        row.get("inserted_at"),
        row.get("InsertedAt"),
        row.get("created_at"),
        row.get("CreatedAt"),
        row.get("updated_at"),
        row.get("UpdatedAt"),
    )

    dt_text = normalize_datetime_text(raw)
    if dt_text:
        return dt_text

    combined = _combine_date_time(
        _first_non_empty(row.get("date"), row.get("Date")),
        _first_non_empty(row.get("time"), row.get("Time")),
    )

    if combined is not None:
        return combined.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

    return normalize_datetime_text(None, default_now=True)


def _raw_json(row: dict[str, Any]) -> str:
    try:
        return json.dumps(row, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


# ============================================================
# row normalization
# ============================================================

def normalize_raw_row(row: dict[str, Any]) -> tuple:
    """
    dict形式のランキング行を ranking_raw_1min 用 tuple へ正規化する。

    tuple順:
      0  ingest_id
      1  symbol
      2  datetime
      3  snapshot_time
      4  symbolname
      5  current_price
      6  price
      7  change_percentage
      8  change_rate
      9  change_ratio
      10 trading_volume
      11 volume
      12 trading_value
      13 turnover
      14 tick_count
      15 ranking_type
      16 rank_type
      17 category
      18 market
      19 exchange
      20 source
      21 rank
      22 date
      23 time
      24 raw_json
      25 received_at
      26 created_at
      27 inserted_at
      28 updated_at
    """
    symbol = _normalize_symbol(
        _first_non_empty(
            row.get("symbol"),
            row.get("Symbol"),
            row.get("code"),
            row.get("Code"),
            row.get("銘柄コード"),
        )
    )

    dt_text = _resolve_row_datetime(row)
    d = dt_text[:10] if dt_text else ""
    t = dt_text[11:19] if len(dt_text) >= 19 else ""

    symbolname = _to_text(
        _first_non_empty(
            row.get("symbolname"),
            row.get("SymbolName"),
            row.get("name"),
            row.get("Name"),
            row.get("銘柄名"),
        )
    )

    current_price = _to_float(
        _first_non_empty(
            row.get("current_price"),
            row.get("price"),
            row.get("CurrentPrice"),
            row.get("現在値"),
        )
    )

    change_percentage = _to_float(
        _first_non_empty(
            row.get("change_percentage"),
            row.get("change_rate"),
            row.get("change_ratio"),
            row.get("ChangePercentage"),
            row.get("騰落率"),
            row.get("value"),
        )
    )

    change_ratio = _to_float(
        _first_non_empty(
            row.get("change_ratio"),
            row.get("ChangeRatio"),
        )
    )

    trading_volume = _to_float(
        _first_non_empty(
            row.get("trading_volume"),
            row.get("volume"),
            row.get("TradingVolume"),
            row.get("売買高"),
        )
    )

    trading_value = _to_float(
        _first_non_empty(
            row.get("trading_value"),
            row.get("turnover"),
            row.get("TradingValue"),
            row.get("売買代金"),
            row.get("value_amount"),
        )
    )

    turnover = _to_float(
        _first_non_empty(
            row.get("turnover"),
            row.get("trading_value"),
            row.get("TradingValue"),
            row.get("売買代金"),
            row.get("value_amount"),
        )
    )

    tick_count = _to_float(
        _first_non_empty(
            row.get("tick_count"),
            row.get("TickCount"),
            row.get("TICK回数"),
        )
    )

    ranking_type = _normalize_ranking_type(
        _first_non_empty(
            row.get("ranking_type"),
            row.get("rank_type"),
            row.get("category"),
            row.get("type"),
            row.get("Type"),
            row.get("ランキング種別"),
        )
    )

    market = _normalize_market(
        _first_non_empty(
            row.get("market"),
            row.get("exchange"),
            row.get("Market"),
            row.get("市場"),
        )
    )

    exchange = _to_text(
        _first_non_empty(
            row.get("exchange"),
            row.get("market"),
            row.get("Exchange"),
        ),
        market,
    )

    rank = _to_int(
        _first_non_empty(
            row.get("rank"),
            row.get("rank_position"),
            row.get("Rank"),
            row.get("順位"),
        )
    )

    source = _to_text(row.get("source"), "ranking")

    received_at = normalize_datetime_text(
        _first_non_empty(row.get("received_at"), row.get("ReceivedAt")),
        default_now=True,
    )
    created_at = normalize_datetime_text(
        _first_non_empty(row.get("created_at"), row.get("CreatedAt")),
        default_now=True,
    )
    inserted_at = normalize_datetime_text(
        _first_non_empty(row.get("inserted_at"), row.get("InsertedAt")),
        default_now=True,
    )
    updated_at = normalize_datetime_text(
        _first_non_empty(row.get("updated_at"), row.get("UpdatedAt")),
        default_now=True,
    )

    ingest_id = _to_text(row.get("ingest_id"))
    if not ingest_id:
        base = _to_text(row.get("_ranking_writer_batch_id"), uuid.uuid4().hex)
        ingest_id = (
            f"{base}_"
            f"{symbol}_"
            f"{dt_text}_"
            f"{ranking_type}_"
            f"{market}_"
            f"{uuid.uuid4().hex[:8]}"
        )

    return (
        ingest_id,
        symbol,
        dt_text,
        dt_text,
        symbolname,
        current_price,
        current_price,
        change_percentage,
        change_percentage,
        change_ratio,
        trading_volume,
        trading_volume,
        trading_value,
        turnover,
        tick_count,
        ranking_type,
        ranking_type,
        ranking_type,
        market,
        exchange,
        source,
        rank,
        d,
        t,
        _raw_json(row),
        received_at,
        created_at,
        inserted_at,
        updated_at,
    )


def _tuple_legacy_to_current(old: Sequence[Any]) -> tuple:
    """
    旧形式tupleを現行29列tupleへ変換する。
    """
    old = tuple(old)

    if len(old) == 29:
        return old

    # 旧28列: updated_atが無い
    if len(old) == 28:
        return tuple(old) + (normalize_datetime_text(None, default_now=True),)

    # 旧22列 snapshot 形式に近いもの:
    # symbol, datetime, snapshot_time, symbolname, current_price, price,
    # change_percentage, change_rate, trading_volume, volume, trading_value,
    # turnover, tick_count, ranking_type, rank_type, category, market, exchange,
    # source, rank, created_at, inserted_at
    if len(old) == 22:
        dt_text = normalize_datetime_text(old[1], default_now=True)
        now = normalize_datetime_text(None, default_now=True)
        base = uuid.uuid4().hex
        ingest_id = f"{base}_{old[0]}_{dt_text}_{old[13]}_{old[16]}_{uuid.uuid4().hex[:8]}"
        d = dt_text[:10]
        t = dt_text[11:19] if len(dt_text) >= 19 else ""
        return (
            ingest_id,
            _normalize_symbol(old[0]),
            dt_text,
            normalize_datetime_text(old[2], default_now=True),
            old[3],
            _to_float(old[4]),
            _to_float(old[5]),
            _to_float(old[6]),
            _to_float(old[7]),
            None,
            _to_float(old[8]),
            _to_float(old[9]),
            _to_float(old[10]),
            _to_float(old[11]),
            _to_float(old[12]),
            _normalize_ranking_type(old[13]),
            _normalize_ranking_type(old[14]),
            _normalize_ranking_type(old[15]),
            _normalize_market(old[16]),
            _to_text(old[17], _normalize_market(old[16])),
            _to_text(old[18], "ranking"),
            _to_int(old[19]),
            d,
            t,
            "{}",
            now,
            normalize_datetime_text(old[20], default_now=True),
            normalize_datetime_text(old[21], default_now=True),
            now,
        )

    # 旧12列:
    # symbol, datetime, symbolname, current_price, change_percentage,
    # trading_volume, trading_value, turnover, tick_count, ranking_type, market, source
    if len(old) == 12:
        dt_text = normalize_datetime_text(old[1], default_now=True)
        now = normalize_datetime_text(None, default_now=True)
        symbol = _normalize_symbol(old[0])
        ranking_type = _normalize_ranking_type(old[9])
        market = _normalize_market(old[10])
        ingest_id = f"{uuid.uuid4().hex}_{symbol}_{dt_text}_{ranking_type}_{market}_{uuid.uuid4().hex[:8]}"
        d = dt_text[:10]
        t = dt_text[11:19] if len(dt_text) >= 19 else ""

        return (
            ingest_id,
            symbol,
            dt_text,
            dt_text,
            old[2],
            _to_float(old[3]),
            _to_float(old[3]),
            _to_float(old[4]),
            _to_float(old[4]),
            None,
            _to_float(old[5]),
            _to_float(old[5]),
            _to_float(old[6]),
            _to_float(old[7]),
            _to_float(old[8]),
            ranking_type,
            ranking_type,
            ranking_type,
            market,
            market,
            _to_text(old[11], "ranking"),
            None,
            d,
            t,
            "{}",
            now,
            now,
            now,
            now,
        )

    return old


def _is_valid_normalized_row(t: Sequence[Any]) -> bool:
    try:
        return bool(
            len(t) == 29
            and str(t[1]).strip()
            and str(t[2]).strip()
            and str(t[15]).strip()
            and str(t[18]).strip()
        )
    except Exception:
        return False


def _dedupe_rows(rows: list[tuple]) -> list[tuple]:
    """
    ingest_id 単位で重複を排除する。
    """
    latest: dict[str, tuple] = {}

    for r in rows:
        key = str(r[0]).strip()
        if not key:
            key = uuid.uuid4().hex
        latest[key] = r

    return list(latest.values())


# ============================================================
# DB helpers
# ============================================================

def _count_rows(conn: sqlite3.Connection) -> int:
    if not ENABLE_COUNT_LOG:
        return -1

    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {quote_ident(RAW_TABLE)}"
        ).fetchone()
        return int(row[0]) if row else -1
    except Exception:
        return -1


def _executemany_chunked(
    conn: sqlite3.Connection,
    sql: str,
    rows: list[tuple],
    *,
    chunk_size: int = CHUNK_SIZE,
) -> int:
    done = 0
    n = max(1, int(chunk_size))

    for i in range(0, len(rows), n):
        chunk = rows[i:i + n]
        conn.executemany(sql, chunk)
        done += len(chunk)

    return done


# ============================================================
# public save
# ============================================================

def save_ranking_raw_rows(
    rows: Iterable[dict[str, Any] | tuple],
    *,
    db_path: str | None = None,
    base_dir: str | None = None,
    ymd: str | None = None,
) -> dict[str, Any]:
    """
    ranking_raw_1min にランキングraw行を保存する。

    Returns
    -------
    dict:
        ok / input_rows / normalized_rows / saved_rows / delta / skipped_invalid
    """
    if db_path is None:
        db_path = resolve_ranking_db_path(base_dir=base_dir, ymd=ymd)

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    input_list = list(rows or [])
    if not input_list:
        return {
            "ok": True,
            "input_rows": 0,
            "normalized_rows": 0,
            "saved_rows": 0,
            "delta": 0,
            "skipped_invalid": 0,
            "db_path": str(path),
        }

    normalized: list[tuple] = []

    for row in input_list:
        try:
            if isinstance(row, dict):
                t = normalize_raw_row(row)
            else:
                t = _tuple_legacy_to_current(tuple(row))

            if _is_valid_normalized_row(t):
                normalized.append(t)
            else:
                logger.debug("[RANKING RAW UPSERT] invalid normalized row skipped row=%r tuple=%r", row, t)

        except Exception:
            logger.warning("[RANKING RAW UPSERT] normalize row failed row=%r", row, exc_info=True)

    skipped_invalid = len(input_list) - len(normalized)
    normalized = _dedupe_rows(normalized)

    if not normalized:
        logger.warning(
            "[RANKING RAW UPSERT] no valid rows input=%s skipped_invalid=%s path=%s",
            len(input_list),
            skipped_invalid,
            path,
        )
        return {
            "ok": False,
            "input_rows": len(input_list),
            "normalized_rows": 0,
            "saved_rows": 0,
            "delta": 0,
            "skipped_invalid": skipped_invalid,
            "db_path": str(path),
            "reason": "no_valid_rows",
        }

    cols = [
        "ingest_id",
        "symbol",
        "datetime",
        "snapshot_time",
        "symbolname",
        "current_price",
        "price",
        "change_percentage",
        "change_rate",
        "change_ratio",
        "trading_volume",
        "volume",
        "trading_value",
        "turnover",
        "tick_count",
        "ranking_type",
        "rank_type",
        "category",
        "market",
        "exchange",
        "source",
        "rank",
        "date",
        "time",
        "raw_json",
        "received_at",
        "created_at",
        "inserted_at",
        "updated_at",
    ]

    sql = f"""
        INSERT OR IGNORE INTO {quote_ident(RAW_TABLE)} (
            {", ".join(quote_ident(c) for c in cols)}
        )
        VALUES ({",".join(["?"] * len(cols))})
    """

    last_err: BaseException | None = None

    for attempt in range(1, RETRY_COUNT + 1):
        conn: sqlite3.Connection | None = None

        try:
            conn = _connect(str(path))
            _begin_immediate(conn)

            ensure_ranking_raw_table(conn)
            patch_ranking_raw_schema(conn)
            delete_null_key_rows(conn)

            before = _count_rows(conn)

            saved = _executemany_chunked(
                conn,
                sql,
                normalized,
                chunk_size=CHUNK_SIZE,
            )

            after = _count_rows(conn)

            _commit(conn)
            _close_quietly(conn)
            conn = None

            delta = (after - before) if before >= 0 and after >= 0 else saved

            logger.info(
                "[RANKING RAW UPSERT] saved table=%s input=%s normalized=%s saved=%s delta=%s skipped_invalid=%s db=%s attempt=%s/%s",
                RAW_TABLE,
                len(input_list),
                len(normalized),
                saved,
                delta,
                skipped_invalid,
                path,
                attempt,
                RETRY_COUNT,
            )

            return {
                "ok": True,
                "input_rows": len(input_list),
                "normalized_rows": len(normalized),
                "saved_rows": saved,
                "delta": delta,
                "skipped_invalid": skipped_invalid,
                "db_path": str(path),
                "attempt": attempt,
            }

        except sqlite3.OperationalError as e:
            last_err = e
            _rollback_quietly(conn)

            if is_lock_error(e) and attempt < RETRY_COUNT:
                slept = lock_sleep_seconds(attempt)
                logger.warning(
                    "[RANKING RAW UPSERT] locked retry table=%s rows=%s attempt=%s/%s sleep=%.2fs err=%s",
                    RAW_TABLE,
                    len(normalized),
                    attempt,
                    RETRY_COUNT,
                    slept,
                    e,
                )
                time.sleep(slept)
                continue

            logger.exception(
                "[RANKING RAW UPSERT] sqlite operational error table=%s rows=%s attempt=%s/%s db=%s",
                RAW_TABLE,
                len(normalized),
                attempt,
                RETRY_COUNT,
                path,
            )
            break

        except Exception as e:
            last_err = e
            _rollback_quietly(conn)

            if attempt < RETRY_COUNT:
                slept = lock_sleep_seconds(attempt)
                logger.warning(
                    "[RANKING RAW UPSERT] retry by exception table=%s rows=%s attempt=%s/%s sleep=%.2fs err=%s",
                    RAW_TABLE,
                    len(normalized),
                    attempt,
                    RETRY_COUNT,
                    slept,
                    e,
                )
                time.sleep(slept)
                continue

            logger.exception(
                "[RANKING RAW UPSERT] failed table=%s rows=%s attempt=%s/%s db=%s",
                RAW_TABLE,
                len(normalized),
                attempt,
                RETRY_COUNT,
                path,
            )
            break

        finally:
            _close_quietly(conn)

    return {
        "ok": False,
        "input_rows": len(input_list),
        "normalized_rows": len(normalized),
        "saved_rows": 0,
        "delta": 0,
        "skipped_invalid": skipped_invalid,
        "db_path": str(path),
        "error": str(last_err) if last_err else "unknown",
    }


# 旧名互換
insert_ranking_raw_1min = save_ranking_raw_rows
save_ranking_raw_1min = save_ranking_raw_rows


__all__ = [
    "save_ranking_raw_rows",
    "insert_ranking_raw_1min",
    "save_ranking_raw_1min",
    "normalize_raw_row",
    "normalize_datetime_text",
]