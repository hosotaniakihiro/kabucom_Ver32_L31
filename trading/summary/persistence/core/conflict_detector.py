# ============================================================
# File   : trading/summary/persistence/core/conflict_detector.py
# Version: Ver1.5-PRODUCTION-CONFLICT-DETECTOR-INTERVAL-STRICT-FINAL
# ------------------------------------------------------------
# ✔ Ver1.4 完全保持（削除ゼロ）
# ✔ summary_saver_bulk から完全分離
# ✔ Ver21.1 ロジック互換維持
# ✔ UNIQUE index 自動検出
# ✔ 優先順位制御（最重要）
# ✔ SQLite / DuckDB 互換志向
# ✔ quoted table / index name 安全化
# ✔ non-unique index 混入耐性
# ✔ PRIMARY KEY / UNIQUE補助検出
# ✔ partial / autoindex / schema定義 補助対応
# ✔ 大小文字・空白・順不同に耐性
# ✔ table名ベース fallback 最適化
# ✔ DBから取得できた実UNIQUE候補を最優先で尊重
# ✔ stock_summary_1min は (symbol, datetime) 優先
# ✔ stock_summary_3min / 5min は (symbol, date, time_range) 優先
# ✔ summary系でも datetime固定優先を廃止
# ✔ fallback safe
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import re
from typing import Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ============================================================
# INTERNAL UTILS
# ============================================================

def _quote_ident(name: str) -> str:
    """
    SQLite向け identifier 安全化。
    テーブル名 / index名に空白や記号があっても扱えるようにする。
    """
    s = "" if name is None else str(name)
    s = s.replace('"', '""')
    return f'"{s}"'


def _normalize_key(key: Sequence[str]) -> Tuple[str, ...]:
    return tuple(
        str(k).strip().strip('"').strip("'").strip("`").lower()
        for k in key
        if str(k).strip()
    )


def _dedupe_preserve_order(items: Iterable[Tuple[str, ...]]) -> List[Tuple[str, ...]]:
    seen = set()
    out: List[Tuple[str, ...]] = []
    for item in items:
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _safe_lower_text(x) -> str:
    return "" if x is None else str(x).strip().lower()


def _normalize_colname(col: str) -> str:
    return str(col).strip().strip('"').strip("'").strip("`")


def _parse_interval_from_table_name(table: str) -> Optional[int]:
    """
    stock_summary_1min / 3min / 5min などから interval を推定。
    """
    try:
        m = re.search(r"(\d+)\s*min\b", str(table), flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
    except Exception:
        logger.exception("[CONFLICT] failed to parse interval from table")
    return None


def _is_summary_table(table: Optional[str]) -> bool:
    name = "" if table is None else str(table).strip().lower()
    return name.startswith("stock_summary_")


# ============================================================
# SQLITE FETCHERS
# ============================================================

def _fetch_sqlite_index_list(conn, table: str):
    """
    PRAGMA index_list(table) 生結果取得。
    戻り値各要素:
      {
        "name": str,
        "unique": int,
        "origin": str,
        "partial": int,
      }
    """
    out = []

    try:
        pragma_sql = f"PRAGMA index_list({_quote_ident(table)})"
        rows = conn.exec_driver_sql(pragma_sql).fetchall()

        for r in rows:
            # SQLite PRAGMA index_list:
            # seq, name, unique, origin, partial
            index_name = r[1] if len(r) > 1 else None
            is_unique = int(r[2]) if len(r) > 2 and r[2] is not None else 0
            origin = str(r[3]).strip() if len(r) > 3 and r[3] is not None else ""
            partial = int(r[4]) if len(r) > 4 and r[4] is not None else 0

            if not index_name:
                continue

            out.append(
                {
                    "name": str(index_name).strip(),
                    "unique": is_unique,
                    "origin": origin,
                    "partial": partial,
                }
            )

    except Exception:
        logger.exception("[CONFLICT] failed to fetch sqlite index_list")

    return out


def _fetch_sqlite_index_columns(conn, index_name: str) -> Tuple[str, ...]:
    """
    PRAGMA index_info(index_name) から index列順を取得。
    """
    cols: List[str] = []

    try:
        info_sql = f"PRAGMA index_info({_quote_ident(index_name)})"
        rows = conn.exec_driver_sql(info_sql).fetchall()

        for r in rows:
            # seqno, cid, name
            if len(r) > 2 and r[2] is not None:
                cols.append(_normalize_colname(r[2]))

    except Exception:
        logger.exception("[CONFLICT] failed to fetch sqlite index_info: %s", index_name)

    return tuple(cols)


def _fetch_sqlite_indexes(conn, table: str) -> List[Tuple[str, ...]]:
    """
    SQLiteから index 一覧を取得。
    UNIQUE index を最優先で拾う。
    """
    indexes: List[Tuple[str, ...]] = []

    try:
        raw_indexes = _fetch_sqlite_index_list(conn, table)

        if not raw_indexes:
            return indexes

        def _priority(x) -> Tuple[int, int, int, str]:
            is_unique = int(x.get("unique", 0))
            origin = _safe_lower_text(x.get("origin"))
            partial = int(x.get("partial", 0))
            name = str(x.get("name", ""))

            p_unique = 0 if is_unique else 1
            p_origin = 0 if origin in ("u", "pk") else 1
            p_partial = 0 if partial == 0 else 1

            return (p_unique, p_origin, p_partial, name)

        ordered = sorted(raw_indexes, key=_priority)

        for meta in ordered:
            if int(meta.get("unique", 0)) != 1:
                continue

            index_name = meta["name"]
            colnames = _fetch_sqlite_index_columns(conn, index_name)

            if colnames:
                indexes.append(colnames)

    except Exception:
        logger.exception("[CONFLICT] failed to fetch sqlite indexes")

    return _dedupe_preserve_order(indexes)


def _fetch_sqlite_unique_constraints_from_schema(conn, table: str) -> List[Tuple[str, ...]]:
    """
    index_list/index_info で拾えない場合に備えて、
    CREATE TABLE SQL から UNIQUE(...) を補助抽出する。
    """
    constraints: List[Tuple[str, ...]] = []

    try:
        sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name = :name",
            {"name": str(table)},
        ).scalar()

        if not sql:
            return constraints

        ddl_text = str(sql)

        for m in re.finditer(r"UNIQUE\s*\((.*?)\)", ddl_text, flags=re.IGNORECASE | re.DOTALL):
            inner = m.group(1)
            cols = []
            for part in inner.split(","):
                c = _normalize_colname(part)
                if c:
                    cols.append(c)
            if cols:
                constraints.append(tuple(cols))

    except Exception:
        logger.exception("[CONFLICT] failed to fetch sqlite unique constraints from schema")

    return _dedupe_preserve_order(constraints)


def _fetch_sqlite_primary_key(conn, table: str) -> List[Tuple[str, ...]]:
    """
    PRIMARY KEY も補助的に取得。
    conflict key の本命ではないが fallback の精度向上のため保持。
    """
    keys: List[Tuple[str, ...]] = []

    try:
        pragma_sql = f"PRAGMA table_info({_quote_ident(table)})"
        rows = conn.exec_driver_sql(pragma_sql).fetchall()

        pk_cols = []
        ordered = sorted(
            rows,
            key=lambda r: int(r[5]) if len(r) > 5 and r[5] is not None else 0
        )

        for r in ordered:
            pk_order = int(r[5]) if len(r) > 5 and r[5] is not None else 0
            if pk_order > 0:
                colname = r[1] if len(r) > 1 else None
                if colname:
                    pk_cols.append(_normalize_colname(colname))

        if pk_cols:
            keys.append(tuple(pk_cols))

    except Exception:
        logger.exception("[CONFLICT] failed to fetch sqlite primary key")

    return _dedupe_preserve_order(keys)


def _fetch_indexes(conn, table: str) -> List[Tuple[str, ...]]:
    """
    既存関数名互換維持。
    取得優先順位:
      1) SQLite index_list / index_info の UNIQUE
      2) CREATE TABLE の UNIQUE(...)
      3) PRIMARY KEY
    """
    indexes: List[Tuple[str, ...]] = []

    sqlite_indexes = _fetch_sqlite_indexes(conn, table)
    if sqlite_indexes:
        indexes.extend(sqlite_indexes)

    schema_uniques = _fetch_sqlite_unique_constraints_from_schema(conn, table)
    if schema_uniques:
        indexes.extend(schema_uniques)

    primary_keys = _fetch_sqlite_primary_key(conn, table)
    if primary_keys:
        indexes.extend(primary_keys)

    indexes = _dedupe_preserve_order(indexes)

    if indexes:
        logger.info("[CONFLICT] fetched candidate keys → %s", indexes)

    return indexes


# ============================================================
# MATCH HELPERS
# ============================================================

def _find_exact_match(
    raw_keys: Sequence[Tuple[str, ...]],
    normalized_keys: Sequence[Tuple[str, ...]],
    target: Tuple[str, ...],
):
    for raw, norm in zip(raw_keys, normalized_keys):
        if norm == target:
            logger.info("[CONFLICT] exact match → %s", raw)
            return ", ".join(raw), raw
    return None


def _find_unordered_match(
    raw_keys: Sequence[Tuple[str, ...]],
    normalized_keys: Sequence[Tuple[str, ...]],
    target: Tuple[str, ...],
):
    target_set = set(target)
    for raw, norm in zip(raw_keys, normalized_keys):
        if set(norm) == target_set and len(norm) == len(target):
            logger.warning("[CONFLICT] unordered match → %s", raw)
            return ", ".join(raw), raw
    return None


def _find_superset_match_prefer_shorter(
    raw_keys: Sequence[Tuple[str, ...]],
    normalized_keys: Sequence[Tuple[str, ...]],
    target: Tuple[str, ...],
):
    """
    例:
      UNIQUE(symbol, date, time_range, source)
      UNIQUE(symbol, datetime, source)
    のように target を含む拡張キーしか無い場合の補助。
    最短の superset を優先する。
    """
    target_set = set(target)
    cands = []

    for raw, norm in zip(raw_keys, normalized_keys):
        norm_set = set(norm)
        if target_set.issubset(norm_set):
            cands.append((len(norm), raw))

    if not cands:
        return None

    cands.sort(key=lambda x: x[0])
    chosen = cands[0][1]
    logger.warning("[CONFLICT] superset match → %s", chosen)
    return ", ".join(chosen), chosen


def _find_best_match_from_candidates(
    raw_keys: Sequence[Tuple[str, ...]],
    normalized_keys: Sequence[Tuple[str, ...]],
    targets: Sequence[Tuple[str, ...]],
):
    """
    targetsを順に exact → unordered → superset の順で評価して返す。
    """
    for target in targets:
        matched = _find_exact_match(raw_keys, normalized_keys, target)
        if matched:
            return matched

    for target in targets:
        matched = _find_unordered_match(raw_keys, normalized_keys, target)
        if matched:
            return matched

    for target in targets:
        matched = _find_superset_match_prefer_shorter(raw_keys, normalized_keys, target)
        if matched:
            return matched

    return None


def _targets_by_interval(table: str) -> List[Tuple[str, ...]]:
    """
    interval別の本命 target 順序。
    1min   : (symbol, datetime) 優先
    3/5min : (symbol, date, time_range) 優先
    """
    interval = _parse_interval_from_table_name(table)

    if interval == 1:
        return [
            ("symbol", "datetime"),
            ("symbol", "date", "time_range"),
        ]

    if interval in (3, 5):
        return [
            ("symbol", "date", "time_range"),
            ("symbol", "datetime"),
        ]

    if _is_summary_table(table):
        return [
            ("symbol", "datetime"),
            ("symbol", "date", "time_range"),
        ]

    return [
        ("symbol", "datetime"),
        ("symbol", "date", "time_range"),
    ]


# ============================================================
# DETECT CONFLICT KEY（最重要）
# ============================================================

def detect_conflict_key(conn, table: str):
    """
    return:
      ("symbol, datetime", ("symbol", "datetime"))
      ("symbol, date, time_range", ("symbol", "date", "time_range"))
    形式を維持
    """
    try:
        indexes = _fetch_indexes(conn, table)

        if not indexes:
            logger.warning("[CONFLICT] no index found → fallback")
            return _fallback(table)

        normalized = [_normalize_key(k) for k in indexes]

        # ----------------------------------------------------
        # DBから取れた実候補を interval別優先順位で判定
        # ----------------------------------------------------
        targets = _targets_by_interval(table)

        matched = _find_best_match_from_candidates(
            indexes,
            normalized,
            targets,
        )
        if matched:
            return matched

        logger.warning("[CONFLICT] no matching representative key → fallback")
        return _fallback(table)

    except Exception:
        logger.exception("[CONFLICT] detect failed")
        return _fallback(table)


# ============================================================
# FALLBACK
# ============================================================

def _fallback(table: Optional[str] = None):
    """
    最終fallback。
    1min   -> (symbol, datetime)
    3/5min -> (symbol, date, time_range)
    """
    interval = _parse_interval_from_table_name(table) if table else None

    if interval == 1:
        key = ("symbol", "datetime")
        return "symbol, datetime", key

    if interval in (3, 5):
        key = ("symbol", "date", "time_range")
        return "symbol, date, time_range", key

    if _is_summary_table(table):
        key = ("symbol", "datetime")
        return "symbol, datetime", key

    key = ("symbol", "date", "time_range")
    return "symbol, date, time_range", key