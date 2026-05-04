# ============================================================
# database/summary_dao.py
# Ver30.0-DUCKDB-NATIVE-FULL-COMPAT
# ------------------------------------------------------------
# ✔ 機能省略ゼロ
# ✔ Bollinger Band 正本（bb_mid / bb_upper / bb_lower / bb_width）
# ✔ suffix(bb_*_1/3/5) 完全吸収・完全排除
# ✔ 1min / 3min / 5min 完全共通 DAO
# ✔ ORM依存完全排除（DuckDBネイティブ）
# ✔ ★ NaN / NaT 完全吸収
# ✔ ★ id 強制除外（UNIQUE爆死完全防止）
# ✔ ★ BULK / UPSERT 完全安全
# ✔ ★ ON CONFLICT Native対応
# ============================================================

import logging
from typing import Dict, Iterable

import pandas as pd

from database.models import (
    StockSummary1Min,
    StockSummary3Min,
    StockSummary5Min,
)

from database.session import summary_engine as ENGINE

logger = logging.getLogger(__name__)

# ============================================================
# テーブル解決
# ============================================================

SUMMARY_TABLE_MAP: Dict[str, object] = {
    "1min": StockSummary1Min,
    "3min": StockSummary3Min,
    "5min": StockSummary5Min,
}

# ============================================================
# NaN / NaT 完全吸収
# ============================================================

def _sanitize_nan(row: dict) -> dict:
    clean = {}
    for k, v in row.items():
        if pd.isna(v):
            clean[k] = None
        else:
            clean[k] = v
    return clean


# ============================================================
# Bollinger 正規化
# ============================================================

def _normalize_bb_columns(row: dict) -> dict:
    row = dict(row)

    if "bb_mid" not in row:
        for k in ("bb_mid_1", "bb_mid_3", "bb_mid_5"):
            if k in row:
                row["bb_mid"] = row.pop(k)
                break

    if "bb_upper" not in row:
        for k in ("bb_upper_1", "bb_upper_3", "bb_upper_5"):
            if k in row:
                row["bb_upper"] = row.pop(k)
                break

    if "bb_lower" not in row:
        for k in ("bb_lower_1", "bb_lower_3", "bb_lower_5"):
            if k in row:
                row["bb_lower"] = row.pop(k)
                break

    if "bb_width" not in row:
        for k in ("bb_width_1", "bb_width_3", "bb_width_5"):
            if k in row:
                row["bb_width"] = row.pop(k)
                break

    for k in list(row.keys()):
        if k.startswith("bb_") and k[-2:] in ("_1", "_3", "_5"):
            row.pop(k)

    return row


# ============================================================
# 内部共通：安全フィルタ
# ============================================================

def _prepare_row(table, row: dict) -> dict:

    row = _normalize_bb_columns(row)
    row = _sanitize_nan(row)

    valid_columns = {
        c.name for c in table.__table__.columns
        if c.name != "id"
    }

    filtered_row = {
        k: v for k, v in row.items()
        if k in valid_columns
    }

    return filtered_row


# ============================================================
# 内部：DuckDB UPSERT
# ============================================================

def _duckdb_upsert(table, row: dict):

    try:

        # ------------------------------------------------
        # ENGINE check
        # ------------------------------------------------

        if ENGINE is None:

            logger.error("[SUMMARY DB] ENGINE is None")
            return

        # ------------------------------------------------
        # row filter
        # ------------------------------------------------

        filtered_row = _prepare_row(table, row)

        if not filtered_row:
            return

        columns = list(filtered_row.keys())

        if not columns:
            return

        # ------------------------------------------------
        # conflict keys
        # ------------------------------------------------

        if "datetime" in columns:

            conflict_cols = ["symbol", "datetime"]

        else:

            conflict_cols = ["symbol", "date", "time_range"]

        conflict_clause = ", ".join(conflict_cols)

        # ------------------------------------------------
        # update clause
        # ------------------------------------------------

        update_clause = ", ".join(
            [f"{col}=excluded.{col}" for col in columns]
        )

        # ------------------------------------------------
        # SQL
        # ------------------------------------------------

        param_names = [f":{c}" for c in columns]

        sql = f"""
        INSERT INTO {table.__tablename__}
        ({",".join(columns)})
        VALUES ({",".join(param_names)})
        ON CONFLICT({conflict_clause})
        DO UPDATE SET {update_clause}
        """

        from sqlalchemy import text

        stmt = text(sql)

        # ------------------------------------------------
        # execute
        # ------------------------------------------------

        with ENGINE.begin() as conn:

            conn.execute(stmt, filtered_row)

    except Exception:

        logger.exception(
            "[SUMMARY DB] duckdb upsert failed"
        )


# ============================================================
# 単行 INSERT（互換維持）
# ============================================================

def insert_summary(session, interval: str, row: dict):

    table = SUMMARY_TABLE_MAP.get(interval)
    if table is None:
        raise ValueError(f"unknown interval: {interval}")

    _duckdb_upsert(table, row)


# ============================================================
# BULK INSERT
# ============================================================

def insert_summary_bulk(
    session,
    interval: str,
    rows: Iterable[dict],
) -> int:

    table = SUMMARY_TABLE_MAP.get(interval)
    if table is None:
        raise ValueError(f"unknown interval: {interval}")

    count = 0

    for row in rows:
        _duckdb_upsert(table, row)
        count += 1

    return count


# ============================================================
# UPSERT
# ============================================================

def upsert_summary(
    session,
    interval: str,
    row: dict,
) -> bool:

    table = SUMMARY_TABLE_MAP.get(interval)
    if table is None:
        raise ValueError(f"unknown interval: {interval}")

    try:
        _duckdb_upsert(table, row)
        return True

    except Exception as e:
        logger.error(
            "❌ upsert_summary(%s) 失敗: %s",
            interval,
            e,
            exc_info=True,
        )
        return False


# ============================================================
# BULK UPSERT
# ============================================================

def upsert_summary_bulk(
    session,
    interval: str,
    rows: Iterable[dict],
) -> int:

    table = SUMMARY_TABLE_MAP.get(interval)
    if table is None:
        raise ValueError(f"unknown interval: {interval}")

    count = 0

    for row in rows:
        try:
            _duckdb_upsert(table, row)
            count += 1
        except Exception as e:
            logger.error(
                "❌ upsert_summary_bulk(%s) row失敗: %s",
                interval,
                e,
                exc_info=True,
            )

    return count