# ============================================================
# File   : trading/summary/persistence/safe_upsert.py
# Version: PRODUCTION-STABLE-REV1.1-FIX-DATE-TIME-TIMERANGE
# Purpose:
#   stock_summary / ranking_summary 用の安全UPSERT
#
# Features:
#   - UNIQUE(symbol, datetime)
#   - CREATE TABLE IF NOT EXISTS
#   - 不足カラム自動追加
#   - sqlite database is locked リトライ
#   - symbol/datetime 重複排除
#   - datetime から date / time / time_range を必ず補完
#   - OHLC alias open_price/high_price/low_price/close_price を補完
#   - pandas.Timestamp / NaT / numpy scalar の SQLite 保存安全化
#
# Important:
#   - stock_summary_1min.date NOT NULL constraint failed 対策
#   - INSERT SQL 作成前に out.columns へ date/time/time_range を入れる
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional, Union

import pandas as pd

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]


SUMMARY_TABLES = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}

RANKING_SUMMARY_TABLES = {
    1: "ranking_summary_1min",
    3: "ranking_summary_3min",
    5: "ranking_summary_5min",
}

OHLC_ALIAS_MAP = {
    "open": "open_price",
    "high": "high_price",
    "low": "low_price",
    "close": "close_price",
}


def _connect(db_path: PathLike) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def _sqlite_type(series: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(series):
        return "INTEGER"
    if pd.api.types.is_float_dtype(series):
        return "REAL"
    if pd.api.types.is_bool_dtype(series):
        return "INTEGER"
    return "TEXT"


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _is_null_like(v: Any) -> bool:
    if v is None:
        return True

    try:
        if pd.isna(v):
            return True
    except Exception:
        pass

    try:
        if isinstance(v, float) and math.isnan(v):
            return True
    except Exception:
        pass

    return False


def _normalize_scalar_for_sqlite(v: Any) -> Any:
    """
    SQLite bind 可能なスカラ値に変換する。
    """
    if _is_null_like(v):
        return None

    try:
        if isinstance(v, bool):
            return int(v)

        if isinstance(v, pd.Timestamp):
            if pd.isna(v):
                return None
            return v.to_pydatetime().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

        if isinstance(v, dt.date):
            return v.strftime("%Y-%m-%d")

        if isinstance(v, dt.time):
            return v.replace(tzinfo=None).strftime("%H:%M:%S")

        if hasattr(v, "item"):
            try:
                x = v.item()
                if x is not v:
                    return _normalize_scalar_for_sqlite(x)
            except Exception:
                pass

        if isinstance(v, (list, dict, set, tuple)):
            return str(v)

    except Exception:
        logger.debug("[SAFE UPSERT] scalar normalize failed value=%r", v, exc_info=True)

    return v


def _normalize_blank_series_with_fallback(
    out: pd.DataFrame,
    *,
    col: str,
    fallback: pd.Series,
) -> pd.DataFrame:
    """
    col が無い、または None/NaN/空文字がある場合に fallback で補完する。
    """
    if col not in out.columns:
        out[col] = fallback
        return out

    try:
        mask = out[col].isna() | (out[col].astype(str).str.strip() == "")
        if mask.any():
            out.loc[mask, col] = fallback.loc[mask]
    except Exception:
        out[col] = fallback

    return out


def _ensure_ohlc_aliases(out: pd.DataFrame) -> pd.DataFrame:
    """
    open/high/low/close と open_price/high_price/low_price/close_price を相互補完する。
    DB側の実カラム差異を吸収するため。
    """
    for src, dst in OHLC_ALIAS_MAP.items():
        if src in out.columns and dst not in out.columns:
            out[dst] = out[src]
        elif dst in out.columns and src not in out.columns:
            out[src] = out[dst]
    return out


def normalize_df_for_upsert(
    df: pd.DataFrame,
    *,
    interval: Optional[int] = None,
    source: Optional[str] = None,
    table: Optional[str] = None,
) -> pd.DataFrame:
    """
    stock_summary / ranking_summary の UPSERT 前 DataFrame 正規化。

    ここで date / time / time_range を必ず作る。
    これをしないと、既存DBの date NOT NULL 制約で以下のように落ちる。

        sqlite3.IntegrityError: NOT NULL constraint failed: stock_summary_1min.date

    Parameters
    ----------
    df:
        保存対象 DataFrame
    interval:
        1 / 3 / 5
    source:
        source列が無い場合の補完値
    table:
        ログ用テーブル名

    Returns
    -------
    pd.DataFrame
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    # --------------------------------------------------------
    # symbol 必須
    # --------------------------------------------------------
    if "symbol" not in out.columns:
        raise ValueError("symbol column is required")

    out["symbol"] = out["symbol"].astype(str).str.strip()
    out = out[
        out["symbol"].notna()
        & (out["symbol"] != "")
        & (out["symbol"].str.lower() != "nan")
        & (out["symbol"].str.lower() != "none")
    ].copy()

    if out.empty:
        return out

    # --------------------------------------------------------
    # datetime 必須
    # --------------------------------------------------------
    if "datetime" not in out.columns:
        for alt in ("dt", "timestamp", "created_at", "updated_at", "inserted_at", "snapshot_time"):
            if alt in out.columns:
                out["datetime"] = out[alt]
                break

    if "datetime" not in out.columns:
        raise ValueError("datetime column is required")

    dt_ser = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.loc[dt_ser.notna()].copy()

    if out.empty:
        return out

    dt_ser = pd.to_datetime(out["datetime"], errors="coerce")
    out["datetime"] = dt_ser.dt.strftime("%Y-%m-%d %H:%M:%S")

    # --------------------------------------------------------
    # date / time / time_range を必ず補完
    # --------------------------------------------------------
    dt_ser = pd.to_datetime(out["datetime"], errors="coerce")

    out = _normalize_blank_series_with_fallback(
        out,
        col="date",
        fallback=dt_ser.dt.strftime("%Y-%m-%d"),
    )

    out = _normalize_blank_series_with_fallback(
        out,
        col="time",
        fallback=dt_ser.dt.strftime("%H:%M:%S"),
    )

    out = _normalize_blank_series_with_fallback(
        out,
        col="time_range",
        fallback=dt_ser.dt.strftime("%H:%M"),
    )

    # --------------------------------------------------------
    # interval / source 補完
    # --------------------------------------------------------
    if interval is not None:
        fallback_interval = pd.Series([interval] * len(out), index=out.index)
        out = _normalize_blank_series_with_fallback(
            out,
            col="interval",
            fallback=fallback_interval,
        )

    if source is not None:
        fallback_source = pd.Series([source] * len(out), index=out.index)
        out = _normalize_blank_series_with_fallback(
            out,
            col="source",
            fallback=fallback_source,
        )

    # --------------------------------------------------------
    # OHLC alias 補完
    # --------------------------------------------------------
    out = _ensure_ohlc_aliases(out)

    # --------------------------------------------------------
    # OHLC 数値化・異常行除外
    # --------------------------------------------------------
    ohlc_cols = [c for c in ("open", "high", "low", "close") if c in out.columns]
    if ohlc_cols:
        before = len(out)

        for col in ohlc_cols:
            out[col] = pd.to_numeric(out[col], errors="coerce")

        out = out.dropna(subset=ohlc_cols)

        if "close" in out.columns:
            out = out[out["close"] > 0]

        after = len(out)
        if before != after:
            logger.warning(
                "[SAFE UPSERT] dropped invalid OHLC rows table=%s rows=%s -> %s dropped=%s",
                table,
                before,
                after,
                before - after,
            )

    if out.empty:
        return out

    # --------------------------------------------------------
    # symbol/datetime 重複排除
    # --------------------------------------------------------
    before = len(out)
    out = out.sort_values(["symbol", "datetime"]).drop_duplicates(["symbol", "datetime"], keep="last")
    after = len(out)

    if before != after:
        logger.info(
            "[SAFE UPSERT] dedupe symbol/datetime table=%s rows=%s -> %s dropped=%s",
            table,
            before,
            after,
            before - after,
        )

    # --------------------------------------------------------
    # dtype / scalar normalize
    # --------------------------------------------------------
    for col in out.columns:
        try:
            if pd.api.types.is_datetime64_any_dtype(out[col]):
                out[col] = out[col].dt.strftime("%Y-%m-%d %H:%M:%S")
            elif pd.api.types.is_bool_dtype(out[col]):
                out[col] = out[col].astype(int)
        except Exception:
            logger.debug("[SAFE UPSERT] dtype normalize skipped col=%s table=%s", col, table, exc_info=True)

    out = out.where(pd.notna(out), None)

    # SQLite bind安全化
    for col in out.columns:
        try:
            out[col] = out[col].map(_normalize_scalar_for_sqlite)
        except Exception:
            logger.debug("[SAFE UPSERT] scalar map skipped col=%s table=%s", col, table, exc_info=True)

    logger.info(
        "[SAFE UPSERT] normalized table=%s interval=%s rows=%s cols=%s has_date=%s has_time=%s has_time_range=%s",
        table,
        interval,
        len(out),
        len(out.columns),
        "date" in out.columns,
        "time" in out.columns,
        "time_range" in out.columns,
    )

    return out


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table'
          AND name=?
        LIMIT 1
        """,
        (table,),
    ).fetchone()
    return row is not None


def get_existing_columns(con: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(con, table):
        return set()
    return {row[1] for row in con.execute(f"PRAGMA table_info({_quote_ident(table)})")}


def create_table_if_missing(
    con: sqlite3.Connection,
    *,
    table: str,
    df: pd.DataFrame,
) -> None:
    if table_exists(con, table):
        return

    cols_sql: list[str] = []

    for col in df.columns:
        if col in {
            "symbol",
            "datetime",
            "date",
            "time",
            "time_range",
            "symbolname",
            "name",
            "source",
        }:
            typ = "TEXT"
        else:
            typ = _sqlite_type(df[col])

        cols_sql.append(f"{_quote_ident(col)} {typ}")

    if "symbol" not in df.columns:
        cols_sql.insert(0, '"symbol" TEXT')

    if "datetime" not in df.columns:
        cols_sql.insert(1, '"datetime" TEXT')

    if "date" not in df.columns:
        cols_sql.insert(2, '"date" TEXT')

    if "time" not in df.columns:
        cols_sql.insert(3, '"time" TEXT')

    if "time_range" not in df.columns:
        cols_sql.insert(4, '"time_range" TEXT')

    sql = f"""
    CREATE TABLE IF NOT EXISTS {_quote_ident(table)} (
        {", ".join(cols_sql)}
    )
    """

    con.execute(sql)

    logger.info("[SAFE UPSERT] created table=%s cols=%s", table, len(cols_sql))


def ensure_columns(
    con: sqlite3.Connection,
    *,
    table: str,
    df: pd.DataFrame,
) -> None:
    existing = get_existing_columns(con, table)

    for col in df.columns:
        if col in existing:
            continue

        typ = _sqlite_type(df[col])

        logger.info(
            "[SAFE UPSERT] add column table=%s column=%s type=%s",
            table,
            col,
            typ,
        )

        con.execute(
            f"ALTER TABLE {_quote_ident(table)} "
            f"ADD COLUMN {_quote_ident(col)} {typ}"
        )


def ensure_unique_index(
    con: sqlite3.Connection,
    *,
    table: str,
    index_name: str,
) -> None:
    con.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {_quote_ident(index_name)}
        ON {_quote_ident(table)}("symbol", "datetime")
        """
    )


def _filter_to_existing_columns_if_needed(
    con: sqlite3.Connection,
    *,
    table: str,
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    既存テーブルに存在する列だけに絞る。

    この safe_upsert.py は ensure_columns() で不足列を追加する方針なので、
    通常は不要だが、何らかの理由で ALTER TABLE に失敗した場合でも
    後段の INSERT で未知列エラーを避けやすくするための保険。
    """
    existing = get_existing_columns(con, table)
    if not existing:
        return df

    keep = [c for c in df.columns if c in existing]

    if not keep:
        logger.warning(
            "[SAFE UPSERT] no matching columns table=%s df_cols=%s existing_cols=%s",
            table,
            list(df.columns),
            sorted(existing),
        )
        return pd.DataFrame()

    dropped = [c for c in df.columns if c not in existing]
    if dropped:
        logger.warning(
            "[SAFE UPSERT] dropped unknown columns table=%s dropped=%s",
            table,
            dropped,
        )

    return df[keep].copy()


def safe_upsert_df(
    df: pd.DataFrame,
    *,
    db_path: PathLike,
    table: str,
    unique_index_name: str | None = None,
    retries: int = 5,
    sleep_sec: float = 0.5,
    interval: Optional[int] = None,
    source: Optional[str] = None,
) -> int:
    if df is None or df.empty:
        logger.info("[SAFE UPSERT] skip empty table=%s", table)
        return 0

    out = normalize_df_for_upsert(
        df,
        interval=interval,
        source=source,
        table=table,
    )

    if out.empty:
        logger.info("[SAFE UPSERT] skip normalized empty table=%s", table)
        return 0

    unique_index_name = unique_index_name or f"uq_{table}_symbol_datetime"

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        con: sqlite3.Connection | None = None

        try:
            con = _connect(db_path)

            create_table_if_missing(con, table=table, df=out)
            ensure_columns(con, table=table, df=out)
            ensure_unique_index(
                con,
                table=table,
                index_name=unique_index_name,
            )

            # ------------------------------------------------
            # ここが INSERT SQL を作る直前
            # この時点で out.columns に date/time/time_range が必要
            # ------------------------------------------------
            out2 = _filter_to_existing_columns_if_needed(con, table=table, df=out)

            if out2.empty:
                logger.warning("[SAFE UPSERT] skip no columns after filter table=%s", table)
                return 0

            required_cols = {"symbol", "datetime"}
            missing_required = required_cols - set(out2.columns)
            if missing_required:
                raise ValueError(
                    f"[SAFE UPSERT] required columns missing before INSERT "
                    f"table={table} missing={sorted(missing_required)} cols={list(out2.columns)}"
                )

            # date/time/time_range が既存テーブルにあるのに out2 から落ちていないか確認
            existing_cols = get_existing_columns(con, table)
            for critical_col in ("date", "time", "time_range"):
                if critical_col in existing_cols and critical_col not in out2.columns:
                    raise ValueError(
                        f"[SAFE UPSERT] critical column missing before INSERT "
                        f"table={table} column={critical_col} cols={list(out2.columns)}"
                    )

            cols = list(out2.columns)
            q_cols = [_quote_ident(c) for c in cols]

            placeholders = ",".join(["?"] * len(cols))
            col_sql = ",".join(q_cols)

            update_cols = [c for c in cols if c not in {"symbol", "datetime"}]

            if update_cols:
                update_sql = ",".join(
                    f"{_quote_ident(c)}=excluded.{_quote_ident(c)}"
                    for c in update_cols
                )
            else:
                update_sql = '"datetime"=excluded."datetime"'

            sql = f"""
            INSERT INTO {_quote_ident(table)} ({col_sql})
            VALUES ({placeholders})
            ON CONFLICT("symbol", "datetime")
            DO UPDATE SET {update_sql}
            """

            rows = [
                tuple(row)
                for row in out2[cols].itertuples(index=False, name=None)
            ]

            con.executemany(sql, rows)
            con.commit()

            logger.info(
                "[SAFE UPSERT] ok db=%s table=%s rows=%s attempt=%s cols=%s",
                db_path,
                table,
                len(rows),
                attempt,
                len(cols),
            )

            return len(rows)

        except sqlite3.OperationalError as e:
            last_error = e
            msg = str(e).lower()

            if "locked" in msg or "busy" in msg:
                logger.warning(
                    "[SAFE UPSERT] locked db=%s table=%s attempt=%s/%s sleep=%.2f err=%s",
                    db_path,
                    table,
                    attempt,
                    retries,
                    sleep_sec * attempt,
                    e,
                )
                time.sleep(sleep_sec * attempt)
                continue

            logger.exception(
                "[SAFE UPSERT] operational error db=%s table=%s",
                db_path,
                table,
            )
            raise

        except sqlite3.IntegrityError as e:
            last_error = e

            logger.exception(
                "[SAFE UPSERT] integrity error db=%s table=%s attempt=%s/%s "
                "cols=%s has_date=%s has_time=%s has_time_range=%s err=%s",
                db_path,
                table,
                attempt,
                retries,
                list(out.columns),
                "date" in out.columns,
                "time" in out.columns,
                "time_range" in out.columns,
                e,
            )
            raise

        except Exception as e:
            last_error = e
            logger.exception(
                "[SAFE UPSERT] failed db=%s table=%s attempt=%s/%s",
                db_path,
                table,
                attempt,
                retries,
            )
            raise

        finally:
            if con is not None:
                con.close()

    raise RuntimeError(
        f"safe_upsert_df failed table={table} db={db_path}: {last_error}"
    )


def upsert_stock_summary(
    df: pd.DataFrame,
    *,
    db_path: PathLike,
    interval: int,
    source: Optional[str] = None,
) -> int:
    interval = int(interval)

    if interval not in SUMMARY_TABLES:
        raise ValueError(f"unsupported stock summary interval: {interval}")

    table = SUMMARY_TABLES[interval]

    return safe_upsert_df(
        df,
        db_path=db_path,
        table=table,
        unique_index_name=f"uq_{table}_symbol_datetime",
        interval=interval,
        source=source,
    )


def upsert_ranking_summary(
    df: pd.DataFrame,
    *,
    db_path: PathLike,
    interval: int,
    source: Optional[str] = None,
) -> int:
    interval = int(interval)

    if interval not in RANKING_SUMMARY_TABLES:
        raise ValueError(f"unsupported ranking summary interval: {interval}")

    table = RANKING_SUMMARY_TABLES[interval]

    return safe_upsert_df(
        df,
        db_path=db_path,
        table=table,
        unique_index_name=f"uq_{table}_symbol_datetime",
        interval=interval,
        source=source,
    )


__all__ = [
    "safe_upsert_df",
    "upsert_stock_summary",
    "upsert_ranking_summary",
    "normalize_df_for_upsert",
    "ensure_unique_index",
    "ensure_columns",
]