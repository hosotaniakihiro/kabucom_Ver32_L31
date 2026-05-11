# ============================================================
# File   : trading/yahoo/symbol/yahoo_symbol_provider.py
# Version: Ver6.1-PRODUCTION-YAHOO-SYMBOL-PROVIDER-SCHEMA-SAFE
# ------------------------------------------------------------
# ✔ Yahoo補完対象銘柄生成
# ✔ ranking_raw_1min を正本にする
# ✔ ranking_raw_1min の実在列だけを使って当日全銘柄を抽出
# ✔ date / datetime / inserted_at / snapshot_time の列欠損に耐える
# ✔ max_symbols デフォルト上限撤廃
# ✔ runtime/backfill status では対象を絞らない
# ✔ scheduler安全
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, List

from sqlalchemy import text

from database import session

logger = logging.getLogger(__name__)

# 終日補完ではランキング登場全銘柄が対象。デフォルト上限は設けない。
MAX_YAHOO_FETCH: int | None = None

RANKING_RAW_TABLE = "ranking_raw_1min"
TIME_COLUMNS_PRIORITY = ("snapshot_time", "inserted_at", "datetime", "date")


# ============================================================
# sanitize
# ============================================================

def _sanitize_symbols(symbols: Iterable[object]) -> List[str]:
    if not symbols:
        return []

    clean: list[str] = []
    seen: set[str] = set()

    for s in symbols:
        if s is None:
            continue

        sym = str(s).strip()
        if not sym:
            continue

        sym = sym.replace(".T", "").replace(".JP", "").strip()
        if sym.endswith(".0"):
            sym = sym[:-2]

        if not sym or sym in seen:
            continue

        seen.add(sym)
        clean.append(sym)

    return clean


def _target_date_values(target_date: dt.date) -> dict[str, str]:
    start_dt = dt.datetime.combine(target_date, dt.time.min)
    end_dt = start_dt + dt.timedelta(days=1)
    return {
        "start_dt": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "end_dt": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "start_dt_us": start_dt.strftime("%Y-%m-%d %H:%M:%S.000000"),
        "end_dt_us": end_dt.strftime("%Y-%m-%d %H:%M:%S.000000"),
        "date_hyphen": target_date.strftime("%Y-%m-%d"),
        "date_compact": target_date.strftime("%Y%m%d"),
        "like_hyphen": target_date.strftime("%Y-%m-%d") + "%",
        "like_compact": target_date.strftime("%Y%m%d") + "%",
    }


def _get_table_columns(s, table_name: str = RANKING_RAW_TABLE) -> set[str]:
    """
    SQLiteの実テーブル列を取得する。

    重要:
      過去版DB/当日DBで ranking_raw_1min の列構成が揺れるため、
      SQL内で存在しない列を固定参照しない。
    """
    try:
        rows = s.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        return {str(r[1]) for r in rows if len(r) > 1 and r[1] is not None}
    except Exception:
        logger.exception("[YAHOO SYMBOL PROVIDER] table_info failed table=%s", table_name)
        return set()


def _existing_time_columns(cols: set[str]) -> list[str]:
    return [c for c in TIME_COLUMNS_PRIORITY if c in cols]


def _build_latest_time_sql(time_cols: list[str]) -> text | None:
    if not time_cols:
        return None

    max_expr = ", ".join([f"MAX({c})" for c in time_cols])
    return text(
        f"""
        SELECT COALESCE({max_expr}) AS latest_time
        FROM {RANKING_RAW_TABLE}
        WHERE symbol IS NOT NULL
          AND TRIM(CAST(symbol AS TEXT)) <> ''
        """
    )


def _build_latest_symbol_sql(time_cols: list[str]) -> text | None:
    if not time_cols:
        return None

    conditions = " OR ".join([f"{c} = :latest_time" for c in time_cols])
    return text(
        f"""
        SELECT DISTINCT CAST(symbol AS TEXT) AS symbol
        FROM {RANKING_RAW_TABLE}
        WHERE symbol IS NOT NULL
          AND TRIM(CAST(symbol AS TEXT)) <> ''
          AND ({conditions})
        ORDER BY symbol
        """
    )


def _build_today_symbol_sql(time_cols: list[str]) -> text | None:
    """
    実在する日時列だけを使って、当日登場銘柄抽出SQLを組み立てる。

    snapshot_time/inserted_at/datetime は日時文字列として範囲比較とLIKEを使う。
    date は YYYY-MM-DD / YYYYMMDD の完全一致とLIKEを使う。
    """
    conditions: list[str] = []

    for col in time_cols:
        if col == "date":
            conditions.append("date = :date_hyphen")
            conditions.append("date = :date_compact")
            conditions.append("date LIKE :like_hyphen")
            conditions.append("date LIKE :like_compact")
            continue

        conditions.append(f"({col} >= :start_dt AND {col} < :end_dt)")
        conditions.append(f"({col} >= :start_dt_us AND {col} < :end_dt_us)")
        conditions.append(f"{col} LIKE :like_hyphen")
        conditions.append(f"{col} LIKE :like_compact")

    if not conditions:
        return None

    where_time = "\n                 OR ".join(conditions)

    return text(
        f"""
        SELECT DISTINCT CAST(symbol AS TEXT) AS symbol
        FROM {RANKING_RAW_TABLE}
        WHERE symbol IS NOT NULL
          AND TRIM(CAST(symbol AS TEXT)) <> ''
          AND (
                {where_time}
          )
        ORDER BY symbol
        """
    )


# ============================================================
# ranking symbols
# ============================================================

def get_latest_ranking_symbols() -> List[str]:
    """
    fallback: ranking_raw_1min の最新 snapshot_time / inserted_at / datetime / date から取得。

    DBによって存在列が違うため、PRAGMA table_info で確認してからSQLを組み立てる。
    """
    symbols: list[str] = []

    try:
        with session.Session_ranking() as s:
            cols = _get_table_columns(s)

            if "symbol" not in cols:
                logger.warning(
                    "[YAHOO SYMBOL PROVIDER] %s has no symbol column cols=%s",
                    RANKING_RAW_TABLE,
                    sorted(cols),
                )
                return []

            time_cols = _existing_time_columns(cols)
            if not time_cols:
                logger.warning(
                    "[YAHOO SYMBOL PROVIDER] latest ranking time columns not found cols=%s",
                    sorted(cols),
                )
                return []

            sql_latest = _build_latest_time_sql(time_cols)
            if sql_latest is None:
                return []

            latest_time = s.execute(sql_latest).scalar()
            if not latest_time:
                logger.warning("[YAHOO SYMBOL PROVIDER] latest ranking time not found")
                return []

            sql_symbols = _build_latest_symbol_sql(time_cols)
            if sql_symbols is None:
                return []

            rows = s.execute(sql_symbols, {"latest_time": str(latest_time)}).fetchall()
            symbols = [r[0] for r in rows]

    except Exception:
        logger.exception("[YAHOO SYMBOL PROVIDER] latest ranking fetch failed")

    out = _sanitize_symbols(symbols)
    logger.info("[YAHOO SYMBOL PROVIDER] latest ranking symbols=%d", len(out))
    return out


def get_today_ranking_symbols_all(
    target_date: dt.date | None = None,
) -> List[str]:
    """
    その日に ranking_raw_1min に1回でも登場した銘柄を全件返す。

    重要:
      ranking_raw_1min はDB作成時期により date / datetime / inserted_at / snapshot_time
      の列構成が揺れる。存在しない列をSQLに含めると SQLite が
      "no such column" で落ちるため、実在列だけを使ってWHEREを作る。
    """
    symbols: list[str] = []

    try:
        if target_date is None:
            target_date = dt.date.today()

        params = _target_date_values(target_date)

        with session.Session_ranking() as s:
            cols = _get_table_columns(s)

            if "symbol" not in cols:
                logger.warning(
                    "[YAHOO SYMBOL PROVIDER] %s has no symbol column cols=%s",
                    RANKING_RAW_TABLE,
                    sorted(cols),
                )
                return []

            time_cols = _existing_time_columns(cols)
            if not time_cols:
                logger.warning(
                    "[YAHOO SYMBOL PROVIDER] usable date/time columns not found cols=%s",
                    sorted(cols),
                )
                return []

            sql = _build_today_symbol_sql(time_cols)
            if sql is None:
                return []

            rows = s.execute(sql, params).fetchall()
            symbols = [r[0] for r in rows]

    except Exception:
        logger.exception("[YAHOO SYMBOL PROVIDER] today ranking fetch failed")
        return []

    out = _sanitize_symbols(symbols)

    logger.info(
        "[YAHOO SYMBOL PROVIDER] today ranking all symbols=%d date=%s source=%s time_cols=%s",
        len(out),
        target_date,
        RANKING_RAW_TABLE,
        "/".join(_existing_time_columns(set(TIME_COLUMNS_PRIORITY))),
    )
    return out


def get_ranking_symbols_for_yahoo(
    *,
    include_today_all_rankings: bool = True,
    target_date: dt.date | None = None,
) -> List[str]:
    try:
        if include_today_all_rankings:
            today_symbols = get_today_ranking_symbols_all(target_date=target_date)
            if today_symbols:
                return today_symbols

        return get_latest_ranking_symbols()

    except Exception:
        logger.exception("[YAHOO SYMBOL PROVIDER] ranking symbol resolver failed")
        return []


# ============================================================
# main provider
# ============================================================

def get_yahoo_target_symbols(
    *,
    max_symbols: int | None = MAX_YAHOO_FETCH,
    include_today_all_rankings: bool = True,
    target_date: dt.date | None = None,
    include_active: bool = False,
    include_light: bool = False,
    include_universe: bool = False,
) -> List[str]:
    """
    Yahoo補完対象銘柄を返す。

    ranking_raw_1min の当日全銘柄を正本にする。
    include_active/include_light/include_universe は互換引数で未使用。
    """
    try:
        ranking = get_ranking_symbols_for_yahoo(
            include_today_all_rankings=include_today_all_rankings,
            target_date=target_date,
        )

        symbols = _sanitize_symbols(ranking)

        if max_symbols is not None:
            try:
                n = int(max_symbols)
                if n > 0:
                    symbols = symbols[:n]
            except Exception:
                pass

        logger.info(
            "[YAHOO SYMBOL PROVIDER] symbols=%d ranking=%d include_today_all_rankings=%s max_symbols=%s target_date=%s mode=ranking_raw_all_day",
            len(symbols),
            len(ranking),
            include_today_all_rankings,
            max_symbols,
            target_date,
        )

        return symbols

    except Exception:
        logger.exception("[YAHOO SYMBOL PROVIDER] symbol build failed")
        return []


__all__ = [
    "MAX_YAHOO_FETCH",
    "get_latest_ranking_symbols",
    "get_today_ranking_symbols_all",
    "get_ranking_symbols_for_yahoo",
    "get_yahoo_target_symbols",
]
