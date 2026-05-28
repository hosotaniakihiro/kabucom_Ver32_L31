# ============================================================
# File   : database/upsert/ranking_snapshot_upsert.py
# Version: PRODUCTION-STABLE-REV1.2-RANKING-SNAPSHOT-UPSERT-WITH-TECH-FILL
# ------------------------------------------------------------
# 【概要】
#   ranking_snapshot_1min upsert。
#
# 【REV1.2 修正内容】
#   - save_ranking_snapshot_rows() 成功後、保存したsymbolだけを対象に
#     database.technicals.ranking_snapshot_technical_fill.fill_ranking_snapshot_technicals()
#     を呼び、1m/3m/5m の MA5/25/75・RSI・MACD・slope等を後埋め保存する。
#   - テクニカル後埋めに失敗してもランキング保存自体は成功扱いにする。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from database.paths.ranking_paths import resolve_ranking_db_path
from database.schema.ranking_snapshot_schema import (
    SNAPSHOT_TABLE,
    ensure_ranking_snapshot_table,
    ensure_ranking_snapshot_unique_index,
    patch_ranking_snapshot_schema,
)
from database.sqlite import (
    is_lock_error,
    lock_sleep_seconds,
    prepare_sqlite_connection,
    quote_ident,
)

logger = logging.getLogger(__name__)

RETRY_COUNT = 5
CHUNK_SIZE = 300
ENABLE_COUNT_LOG = True


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
    if not s:
        return "ALL"
    return s


def _normalize_ranking_type(v: Any) -> str:
    s = _to_text(v)
    if not s:
        return "UNKNOWN"
    return s


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


def _combine_date_time(date_value: Any, time_value: Any) -> dt.datetime | None:
    d_txt = _to_text(date_value)
    t_txt = _to_text(time_value)
    if not d_txt:
        return None
    if not t_txt:
        t_txt = "00:00:00"
    return _parse_datetime_any(f"{d_txt} {t_txt}")


def normalize_datetime_text(v: Any, *, default_now: bool = False) -> str:
    d = _parse_datetime_any(v)
    if d is None and default_now:
        d = dt.datetime.now()
    if d is None:
        return ""
    return d.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


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


def normalize_snapshot_row(row: dict[str, Any]) -> tuple:
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
    exchange = _to_text(_first_non_empty(row.get("exchange"), row.get("market"), row.get("Exchange")), market)
    rank = _to_int(_first_non_empty(row.get("rank"), row.get("rank_position"), row.get("Rank"), row.get("順位")))
    source = _to_text(row.get("source"), "ranking")
    return (
        symbol,
        dt_text,
        dt_text,
        symbolname,
        current_price,
        current_price,
        change_percentage,
        change_percentage,
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
        dt_text,
        dt_text,
    )


def _tuple_legacy_to_current(old: Sequence[Any]) -> tuple:
    old = tuple(old)
    if len(old) == 22:
        return old
    if len(old) == 21:
        created_at = old[20]
        inserted_at = created_at or old[1]
        return tuple(old) + (inserted_at,)
    if len(old) == 19:
        dt_text = normalize_datetime_text(old[1], default_now=True)
        return tuple(old) + (None, dt_text, dt_text)
    if len(old) == 12:
        symbol = _normalize_symbol(old[0])
        dt_text = normalize_datetime_text(old[1], default_now=True)
        symbolname = old[2]
        current_price = _to_float(old[3])
        change_percentage = _to_float(old[4])
        trading_volume = _to_float(old[5])
        trading_value = _to_float(old[6])
        turnover = _to_float(old[7])
        tick_count = _to_float(old[8])
        ranking_type = _normalize_ranking_type(old[9])
        market = _normalize_market(old[10])
        source = _to_text(old[11], "ranking")
        return (
            symbol,
            dt_text,
            dt_text,
            symbolname,
            current_price,
            current_price,
            change_percentage,
            change_percentage,
            trading_volume,
            trading_volume,
            trading_value,
            turnover,
            tick_count,
            ranking_type,
            ranking_type,
            ranking_type,
            market,
            market,
            source,
            None,
            dt_text,
            dt_text,
        )
    return old


def _is_valid_normalized_row(t: Sequence[Any]) -> bool:
    try:
        return bool(str(t[0]).strip() and str(t[1]).strip() and str(t[2]).strip() and str(t[13]).strip() and str(t[16]).strip() and len(t) == 22)
    except Exception:
        return False


def _dedupe_rows(rows: list[tuple]) -> list[tuple]:
    latest: dict[tuple[str, str, str, str], tuple] = {}
    for r in rows:
        key = (str(r[0]).strip(), str(r[1]).strip(), str(r[13]).strip(), str(r[16]).strip())
        latest[key] = r
    return list(latest.values())


def _count_rows(conn: sqlite3.Connection) -> int:
    if not ENABLE_COUNT_LOG:
        return -1
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(SNAPSHOT_TABLE)}").fetchone()
        return int(row[0]) if row else -1
    except Exception:
        return -1


def _executemany_chunked(conn: sqlite3.Connection, sql: str, rows: list[tuple], *, chunk_size: int = CHUNK_SIZE) -> int:
    done = 0
    n = max(1, int(chunk_size))
    for i in range(0, len(rows), n):
        chunk = rows[i:i + n]
        conn.executemany(sql, chunk)
        done += len(chunk)
    return done


def _run_technical_fill_after_save(*, db_path: str, normalized_rows: list[tuple]) -> dict[str, Any] | None:
    try:
        symbols = sorted({str(r[0]).strip() for r in normalized_rows if len(r) >= 1 and str(r[0]).strip()})
        if not symbols:
            return None
        from database.technicals.ranking_snapshot_technical_fill import fill_ranking_snapshot_technicals
        return fill_ranking_snapshot_technicals(db_path=db_path, symbols=symbols, lookback_rows=220)
    except Exception as exc:
        logger.warning("[RANKING SNAPSHOT UPSERT] technical fill skipped db=%s err=%s", db_path, exc, exc_info=True)
        return {"ok": False, "error": str(exc)}


def save_ranking_snapshot_rows(
    rows: Iterable[dict[str, Any] | tuple],
    *,
    db_path: str | None = None,
    base_dir: str | None = None,
    ymd: str | None = None,
) -> dict[str, Any]:
    if db_path is None:
        db_path = resolve_ranking_db_path(base_dir=base_dir, ymd=ymd)

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    normalized_rows: list[tuple] = []
    skipped_invalid = 0
    input_count = 0

    for row in rows:
        input_count += 1
        try:
            if isinstance(row, dict):
                t = normalize_snapshot_row(row)
            else:
                t = _tuple_legacy_to_current(tuple(row))
            if not _is_valid_normalized_row(t):
                skipped_invalid += 1
                logger.debug("[RANKING SNAPSHOT UPSERT] invalid normalized row skipped index=%s len=%s row=%s", input_count, len(t) if isinstance(t, tuple) else None, t)
                continue
            normalized_rows.append(tuple(t))
        except Exception:
            skipped_invalid += 1
            logger.exception("[RANKING SNAPSHOT UPSERT] normalize row failed input_index=%s", input_count)

    normalized_rows = _dedupe_rows(normalized_rows)

    if not normalized_rows:
        logger.warning("[RANKING SNAPSHOT UPSERT] no rows to save db=%s input_rows=%s skipped_invalid=%s", db_path, input_count, skipped_invalid)
        return {"ok": True, "db_path": str(db_path), "input_rows": input_count, "normalized_rows": 0, "saved_rows": 0, "delta": 0, "skipped_invalid": skipped_invalid, "locked": False}

    sql = f"""
        INSERT INTO {quote_ident(SNAPSHOT_TABLE)} (
            symbol,
            datetime,
            snapshot_time,
            symbolname,
            current_price,
            price,
            change_percentage,
            change_rate,
            trading_volume,
            volume,
            trading_value,
            turnover,
            tick_count,
            ranking_type,
            rank_type,
            category,
            market,
            exchange,
            source,
            rank,
            created_at,
            inserted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, datetime, ranking_type, market)
        DO UPDATE SET
            snapshot_time=excluded.snapshot_time,
            symbolname=excluded.symbolname,
            current_price=excluded.current_price,
            price=excluded.price,
            change_percentage=excluded.change_percentage,
            change_rate=excluded.change_rate,
            trading_volume=excluded.trading_volume,
            volume=excluded.volume,
            trading_value=excluded.trading_value,
            turnover=excluded.turnover,
            tick_count=excluded.tick_count,
            rank_type=excluded.rank_type,
            category=excluded.category,
            exchange=excluded.exchange,
            source=excluded.source,
            rank=excluded.rank,
            created_at=COALESCE(excluded.created_at, created_at),
            inserted_at=COALESCE(excluded.inserted_at, inserted_at),
            updated_at=CURRENT_TIMESTAMP
    """

    last_exc: BaseException | None = None
    t0 = time.perf_counter()

    for attempt in range(1, max(1, RETRY_COUNT) + 1):
        conn: sqlite3.Connection | None = None
        try:
            conn = _connect(str(path))
            _begin_immediate(conn)
            ensure_ranking_snapshot_table(conn)
            patch_ranking_snapshot_schema(conn)
            ensure_ranking_snapshot_unique_index(conn)
            before = _count_rows(conn)
            saved = _executemany_chunked(conn, sql, normalized_rows, chunk_size=CHUNK_SIZE)
            _commit(conn)
            after = _count_rows(conn)
            delta = after - before if before >= 0 and after >= 0 else 0
            elapsed = time.perf_counter() - t0
            _close_quietly(conn)
            conn = None

            tech_result = _run_technical_fill_after_save(db_path=str(db_path), normalized_rows=normalized_rows)

            logger.info(
                "[RANKING SNAPSHOT UPSERT] db=%s input_rows=%s normalized_rows=%s saved_rows=%s before=%s after=%s delta=%s skipped_invalid=%s elapsed=%.3fs attempt=%s tech=%s",
                db_path,
                input_count,
                len(normalized_rows),
                saved,
                before,
                after,
                delta,
                skipped_invalid,
                elapsed,
                attempt,
                tech_result,
            )
            return {"ok": True, "db_path": str(db_path), "input_rows": input_count, "normalized_rows": len(normalized_rows), "saved_rows": saved, "delta": delta, "skipped_invalid": skipped_invalid, "locked": False, "technical_fill": tech_result}
        except sqlite3.OperationalError as exc:
            last_exc = exc
            _rollback_quietly(conn)
            if is_lock_error(exc) and attempt < RETRY_COUNT:
                slept = lock_sleep_seconds(attempt)
                logger.warning("[RANKING SNAPSHOT UPSERT] locked retry db=%s attempt=%s/%s sleep=%.2fs err=%s", db_path, attempt, RETRY_COUNT, slept, exc)
                time.sleep(slept)
                continue
            logger.exception("[RANKING SNAPSHOT UPSERT] sqlite operational error db=%s attempt=%s/%s", db_path, attempt, RETRY_COUNT)
            break
        except Exception as exc:
            last_exc = exc
            _rollback_quietly(conn)
            if is_lock_error(exc) and attempt < RETRY_COUNT:
                slept = lock_sleep_seconds(attempt)
                logger.warning("[RANKING SNAPSHOT UPSERT] locked retry db=%s attempt=%s/%s sleep=%.2fs err=%s", db_path, attempt, RETRY_COUNT, slept, exc)
                time.sleep(slept)
                continue
            logger.exception("[RANKING SNAPSHOT UPSERT] failed db=%s attempt=%s/%s", db_path, attempt, RETRY_COUNT)
            break
        finally:
            _close_quietly(conn)

    return {"ok": False, "db_path": str(db_path), "input_rows": input_count, "normalized_rows": len(normalized_rows), "saved_rows": 0, "delta": 0, "skipped_invalid": skipped_invalid, "locked": is_lock_error(last_exc) if last_exc else False, "error": str(last_exc) if last_exc else None}


__all__ = [
    "normalize_datetime_text",
    "normalize_snapshot_row",
    "save_ranking_snapshot_rows",
]
