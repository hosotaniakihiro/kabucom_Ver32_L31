# ============================================================
# File   : database/sqlite/sql_builder.py
# Version: PRODUCTION-STABLE-REV1.0-SQLITE-SQL-BUILDER
# ------------------------------------------------------------
# Purpose:
#   SQLite identifier quote / generic upsert SQL builder
# ============================================================

from __future__ import annotations

from typing import Iterable, Sequence


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sqlite_quote_literal(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def extract_columns(rows: Sequence[dict]) -> list[str]:
    cols: list[str] = []
    seen = set()

    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                cols.append(k)

    return cols


def build_sqlite_upsert_sql(
    table_name: str,
    rows: Sequence[dict],
    *,
    conflict_cols: Iterable[str],
    exclude_update_cols: Iterable[str] = (),
) -> str:
    cols = extract_columns(rows)
    if not cols:
        raise ValueError("no columns for upsert")

    conflict_cols = tuple(conflict_cols)
    exclude_update_cols = set(exclude_update_cols) | set(conflict_cols)

    insert_cols = ", ".join(quote_ident(c) for c in cols)
    insert_vals = ", ".join(f":{c}" for c in cols)
    conflict = ", ".join(quote_ident(c) for c in conflict_cols)

    update_cols = [c for c in cols if c not in exclude_update_cols]
    update_set = ", ".join(f"{quote_ident(c)}=excluded.{quote_ident(c)}" for c in update_cols)

    if not update_set:
        first = conflict_cols[0] if conflict_cols else cols[0]
        update_set = f"{quote_ident(first)}={quote_ident(first)}"

    return (
        f"INSERT INTO {quote_ident(table_name)} ({insert_cols}) "
        f"VALUES ({insert_vals}) "
        f"ON CONFLICT ({conflict}) "
        f"DO UPDATE SET {update_set}"
    )


def build_insert_sql(table_name: str, rows: Sequence[dict]) -> str:
    cols = extract_columns(rows)
    if not cols:
        raise ValueError("no columns for insert")

    insert_cols = ", ".join(quote_ident(c) for c in cols)
    insert_vals = ", ".join(f":{c}" for c in cols)

    return f"INSERT INTO {quote_ident(table_name)} ({insert_cols}) VALUES ({insert_vals})"


def build_delete_by_columns_sql(table_name: str, key_cols: Iterable[str]) -> str:
    keys = tuple(key_cols)
    if not keys:
        raise ValueError("key_cols is empty")

    where = " AND ".join(f"{quote_ident(c)} = :{c}" for c in keys)
    return f"DELETE FROM {quote_ident(table_name)} WHERE {where}"
