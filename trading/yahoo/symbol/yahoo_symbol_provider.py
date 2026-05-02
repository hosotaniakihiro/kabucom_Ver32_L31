# ============================================================
# File   : trading/yahoo/symbol/yahoo_symbol_provider.py
# Version: Ver6.0-PRODUCTION-YAHOO-SYMBOL-PROVIDER-RANKING-RAW-SNAPSHOT-FIX
# ------------------------------------------------------------
# ✔ Yahoo補完対象銘柄生成
# ✔ ranking_raw_1min を正本にする
# ✔ date / datetime が NULL でも snapshot_time / inserted_at で当日全銘柄を抽出
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


# ============================================================
# ranking symbols
# ============================================================

def get_latest_ranking_symbols() -> List[str]:
    """
    fallback: ranking_raw_1min の最新 snapshot_time / inserted_at から取得。
    """
    symbols: list[str] = []

    try:
        sql_latest = text(
            """
            SELECT COALESCE(MAX(snapshot_time), MAX(inserted_at), MAX(datetime)) AS latest_time
            FROM ranking_raw_1min
            WHERE symbol IS NOT NULL AND TRIM(CAST(symbol AS TEXT)) <> ''
            """
        )

        with session.Session_ranking() as s:
            latest_time = s.execute(sql_latest).scalar()
            if not latest_time:
                logger.warning("[YAHOO SYMBOL PROVIDER] latest ranking time not found")
                return []

            rows = s.execute(
                text(
                    """
                    SELECT DISTINCT CAST(symbol AS TEXT) AS symbol
                    FROM ranking_raw_1min
                    WHERE symbol IS NOT NULL
                      AND TRIM(CAST(symbol AS TEXT)) <> ''
                      AND (
                            snapshot_time = :latest_time
                         OR inserted_at = :latest_time
                         OR datetime = :latest_time
                      )
                    ORDER BY symbol
                    """
                ),
                {"latest_time": str(latest_time)},
            ).fetchall()

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
      ranking_raw_1min は date / datetime が NULL のケースがあるため、
      snapshot_time を最優先にし、inserted_at / datetime / date を保険にする。
    """
    symbols: list[str] = []

    try:
        if target_date is None:
            target_date = dt.date.today()

        params = _target_date_values(target_date)

        sql = text(
            """
            SELECT DISTINCT CAST(symbol AS TEXT) AS symbol
            FROM ranking_raw_1min
            WHERE symbol IS NOT NULL
              AND TRIM(CAST(symbol AS TEXT)) <> ''
              AND (
                    (snapshot_time >= :start_dt AND snapshot_time < :end_dt)
                 OR (snapshot_time >= :start_dt_us AND snapshot_time < :end_dt_us)
                 OR (inserted_at   >= :start_dt AND inserted_at   < :end_dt)
                 OR (inserted_at   >= :start_dt_us AND inserted_at   < :end_dt_us)
                 OR (datetime      >= :start_dt AND datetime      < :end_dt)
                 OR (datetime      >= :start_dt_us AND datetime      < :end_dt_us)
                 OR date = :date_hyphen
                 OR date = :date_compact
                 OR snapshot_time LIKE :like_hyphen
                 OR inserted_at   LIKE :like_hyphen
                 OR datetime      LIKE :like_hyphen
                 OR date          LIKE :like_hyphen
                 OR snapshot_time LIKE :like_compact
                 OR inserted_at   LIKE :like_compact
                 OR datetime      LIKE :like_compact
                 OR date          LIKE :like_compact
              )
            ORDER BY symbol
            """
        )

        with session.Session_ranking() as s:
            rows = s.execute(sql, params).fetchall()
            symbols = [r[0] for r in rows]

    except Exception:
        logger.exception("[YAHOO SYMBOL PROVIDER] today ranking fetch failed")
        return []

    out = _sanitize_symbols(symbols)

    logger.info(
        "[YAHOO SYMBOL PROVIDER] today ranking all symbols=%d date=%s source=ranking_raw_1min time_col=snapshot_time/inserted_at/datetime/date",
        len(out),
        target_date,
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
