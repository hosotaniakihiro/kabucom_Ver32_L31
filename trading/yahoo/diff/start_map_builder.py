# ============================================================
# File   : trading/yahoo/diff/start_map_builder.py
# Version: Ver1.0-PRODUCTION-YAHOO-START-MAP-BUILDER
# ------------------------------------------------------------
# ✔ 定期: yahoo_1min latest ベース差分
# ✔ 起動時: summary 3min / 5min latest ベース差分
# ✔ 最長でも前々営業日 09:00 まで
# ✔ production hardened
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os

import pandas as pd
from sqlalchemy import create_engine, text

from utils.business_day_utils import get_prev_prev_business_day
from trading.yahoo.storage.yahoo_1min_bootstrap import get_intraday_db_path

logger = logging.getLogger(__name__)

LOOKBACK_BUFFER_MINUTES = 2
DEFAULT_SESSION_START = dt.time(9, 0)
SUMMARY_LOOKBACK_3MIN_MINUTES = 30
SUMMARY_LOOKBACK_5MIN_MINUTES = 60
STARTUP_SKIP_IF_FRESH_WITHIN_MINUTES = 1


def load_yahoo_1min_last_datetimes(target_date: dt.date) -> dict[str, dt.datetime]:
    db_path = get_intraday_db_path(target_date)
    engine = None

    try:
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 5},
            pool_pre_ping=True,
        )

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table' AND name='yahoo_1min'
                    """
                )
            ).fetchone()

            if not row:
                return {}

            rows = conn.execute(
                text(
                    """
                    SELECT symbol, MAX(datetime) AS last_dt
                    FROM yahoo_1min
                    GROUP BY symbol
                    """
                )
            ).fetchall()

        out: dict[str, dt.datetime] = {}
        for sym, last_dt in rows:
            try:
                if sym is None or last_dt is None:
                    continue
                sym = str(sym).strip()
                ts = pd.to_datetime(last_dt, errors="coerce")
                if not sym or pd.isna(ts):
                    continue
                out[sym] = ts.to_pydatetime()
            except Exception:
                continue

        return out

    except Exception:
        logger.exception("[YAHOO START MAP] load yahoo_1min latest failed")
        return {}

    finally:
        try:
            if engine is not None:
                engine.dispose()
        except Exception:
            pass


def build_periodic_symbol_start_map(
    symbols: list[str],
    target_date: dt.date,
    end_dt: dt.datetime,
) -> dict[str, dt.datetime]:
    last_map = load_yahoo_1min_last_datetimes(target_date)
    default_start = dt.datetime.combine(target_date, DEFAULT_SESSION_START)

    out: dict[str, dt.datetime] = {}

    for sym in symbols:
        last_dt = last_map.get(sym)

        if last_dt is None:
            start_dt = default_start
        else:
            start_dt = last_dt - dt.timedelta(minutes=LOOKBACK_BUFFER_MINUTES)

        if start_dt >= end_dt:
            start_dt = end_dt - dt.timedelta(minutes=LOOKBACK_BUFFER_MINUTES)

        if start_dt < default_start:
            start_dt = default_start

        out[sym] = start_dt

    return out


def _summary_db_candidates(target_date: dt.date) -> list[str]:
    ymd = target_date.strftime("%Y%m%d")
    return [
        fr"\\192.168.0.22\AutoStockBuyAndSell\summary\summary{ymd}.db",
        fr"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary\summary{ymd}.db",
    ]


def _resolve_existing_summary_db_path(target_date: dt.date) -> str | None:
    for path in _summary_db_candidates(target_date):
        try:
            if os.path.exists(path):
                return path
        except Exception:
            continue
    return None


def _load_summary_last_datetimes(
    target_date: dt.date,
    interval: int,
) -> dict[str, dt.datetime]:
    db_path = _resolve_existing_summary_db_path(target_date)
    engine = None

    if not db_path:
        logger.info(
            "[YAHOO START MAP] summary db not found target_date=%s interval=%s",
            target_date,
            interval,
        )
        return {}

    table = f"stock_summary_{int(interval)}min"

    try:
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 5},
            pool_pre_ping=True,
        )

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table' AND name=:table_name
                    """
                ),
                {"table_name": table},
            ).fetchone()

            if not row:
                return {}

            rows = conn.execute(
                text(
                    f"""
                    SELECT symbol, MAX(datetime) AS last_dt
                    FROM {table}
                    GROUP BY symbol
                    """
                )
            ).fetchall()

        out: dict[str, dt.datetime] = {}
        for sym, last_dt in rows:
            try:
                if sym is None or last_dt is None:
                    continue
                sym = str(sym).strip()
                ts = pd.to_datetime(last_dt, errors="coerce")
                if not sym or pd.isna(ts):
                    continue
                out[sym] = ts.to_pydatetime()
            except Exception:
                continue

        return out

    except Exception:
        logger.exception(
            "[YAHOO START MAP] load summary latest failed interval=%s db=%s",
            interval,
            db_path,
        )
        return {}

    finally:
        try:
            if engine is not None:
                engine.dispose()
        except Exception:
            pass


def _calc_start_from_summary_latest(
    *,
    latest_3m: dt.datetime | None,
    latest_5m: dt.datetime | None,
    today: dt.date,
    hard_floor: dt.datetime,
    end_dt: dt.datetime,
) -> dt.datetime:
    session_start = dt.datetime.combine(today, DEFAULT_SESSION_START)
    candidates: list[dt.datetime] = []

    if latest_3m is not None:
        candidates.append(
            latest_3m
            - dt.timedelta(minutes=SUMMARY_LOOKBACK_3MIN_MINUTES)
            - dt.timedelta(minutes=LOOKBACK_BUFFER_MINUTES)
        )

    if latest_5m is not None:
        candidates.append(
            latest_5m
            - dt.timedelta(minutes=SUMMARY_LOOKBACK_5MIN_MINUTES)
            - dt.timedelta(minutes=LOOKBACK_BUFFER_MINUTES)
        )

    if candidates:
        start_dt = min(candidates)
    else:
        start_dt = hard_floor

    if start_dt < hard_floor:
        start_dt = hard_floor

    if start_dt >= end_dt:
        start_dt = end_dt - dt.timedelta(minutes=LOOKBACK_BUFFER_MINUTES)

    if start_dt < hard_floor:
        start_dt = hard_floor

    if start_dt.date() == today and start_dt < session_start:
        start_dt = session_start

    return start_dt


def build_startup_symbol_start_map(
    symbols: list[str],
    target_date: dt.date,
    end_dt: dt.datetime,
) -> dict[str, dt.datetime]:
    latest_3m_map = _load_summary_last_datetimes(target_date, interval=3)
    latest_5m_map = _load_summary_last_datetimes(target_date, interval=5)

    # Yahoo補完対象は「当日ランキングに登場した銘柄」。
    # 新規登場銘柄はマーケット開始 09:00 から取得する。
    # 以前の prev-prev business day まで戻す方式は、無駄なダウンロードと計算が多く、
    # 当日summary補完の目的にも合わない。
    hard_floor = dt.datetime.combine(target_date, DEFAULT_SESSION_START)

    out: dict[str, dt.datetime] = {}

    for sym in symbols:
        latest_3m = latest_3m_map.get(sym)
        latest_5m = latest_5m_map.get(sym)

        start_dt = _calc_start_from_summary_latest(
            latest_3m=latest_3m,
            latest_5m=latest_5m,
            today=target_date,
            hard_floor=hard_floor,
            end_dt=end_dt,
        )

        newest = None
        if latest_3m is not None and latest_5m is not None:
            newest = max(latest_3m, latest_5m)
        elif latest_3m is not None:
            newest = latest_3m
        elif latest_5m is not None:
            newest = latest_5m

        if newest is not None:
            age_min = (end_dt - newest).total_seconds() / 60.0
            if age_min <= STARTUP_SKIP_IF_FRESH_WITHIN_MINUTES:
                tiny_start = end_dt - dt.timedelta(minutes=LOOKBACK_BUFFER_MINUTES)
                if tiny_start > start_dt:
                    start_dt = tiny_start

        out[sym] = start_dt

    return out


__all__ = [
    "build_periodic_symbol_start_map",
    "build_startup_symbol_start_map",
]