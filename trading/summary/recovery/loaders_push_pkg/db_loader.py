# ============================================================
# File   : trading/summary/recovery/loaders_push_pkg/db_loader.py
# Ver    : PRODUCTION-STABLE-REV4.0-LOADERS-PUSH-DB-LOADER
# ------------------------------------------------------------
# 【概要】
#   PUSH DB loader
#
# 【主な機能】
#   ✔ 複数日 PUSH DB 読込
#   ✔ stream_data / push_data / ticks / push 自動検出
#   ✔ 実在カラムのみで WHERE 生成
#   ✔ start_dt / end_dt filter
#   ✔ symbol filter
#   ✔ future tick guard
#   ✔ market session filter
#   ✔ normalize_push_df 適用
#
# 【重要】
#   - tick_time 固定SQLは禁止
#   - DBスキーマ差異を吸収する
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from typing import Iterable, Optional

import pandas as pd

from trading.summary.recovery.loaders_common import (
    log_df_date_breakdown,
    now_naive,
)

from .filters import (
    filter_future_ticks,
    filter_market_session_ticks,
)
from .normalizer import (
    apply_symbol_filter_df,
    normalize_push_df,
    normalize_symbols,
)
from .path_resolver import (
    detect_push_table_name,
    resolve_push_db_path,
)
from .sql_helpers import (
    build_db_where_for_push,
    fetch_push_table_columns,
    quote_ident,
)
from .timezone import to_tz_naive_timestamp

logger = logging.getLogger(__name__)


def load_push_df_for_dates(
    target_dates: Iterable[dt.date],
    *,
    now: Optional[dt.datetime] = None,
    drop_future_ticks: bool = True,
    market_hours_only: bool = False,
    start_dt: Optional[pd.Timestamp] = None,
    end_dt: Optional[pd.Timestamp] = None,
    symbols: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    target_dates_list = list(target_dates or [])

    start_dt_safe = (
        to_tz_naive_timestamp(start_dt, label="load_push_df_for_dates.start_dt")
        if start_dt is not None
        else None
    )
    end_dt_safe = (
        to_tz_naive_timestamp(end_dt, label="load_push_df_for_dates.end_dt")
        if end_dt is not None
        else None
    )

    for trade_date in target_dates_list:
        path = resolve_push_db_path(trade_date)
        if not os.path.exists(path):
            logger.warning(
                "[summary.recovery.loaders_push.db_loader] push db not found path=%s",
                path,
            )
            continue

        try:
            with sqlite3.connect(path) as conn:
                table_name = detect_push_table_name(conn)
                if not table_name:
                    logger.warning(
                        "[summary.recovery.loaders_push.db_loader] no push table detected path=%s",
                        path,
                    )
                    continue

                columns = fetch_push_table_columns(conn, table_name)
                if not columns:
                    logger.warning(
                        "[summary.recovery.loaders_push.db_loader] no columns detected table=%s path=%s",
                        table_name,
                        path,
                    )
                    continue

                where_sql, params = build_db_where_for_push(
                    columns=columns,
                    start_dt=start_dt_safe,
                    end_dt=end_dt_safe,
                    symbols=symbols,
                )

                sql = f"SELECT * FROM {quote_ident(table_name)}{where_sql}"

                logger.info(
                    "[summary.recovery.loaders_push.db_loader] load db table=%s path=%s columns=%s where=%s params_keys=%s",
                    table_name,
                    path,
                    sorted(columns),
                    where_sql.strip() or "-",
                    sorted(params.keys()),
                )

                one = pd.read_sql_query(sql, conn, params=params)
                if one is not None and not one.empty:
                    frames.append(one)

        except Exception:
            logger.exception(
                "[summary.recovery.loaders_push.db_loader] load push db failed path=%s",
                path,
            )

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out = normalize_push_df(out)
    out = apply_symbol_filter_df(out, symbols)

    if drop_future_ticks and not out.empty:
        out = filter_future_ticks(
            out,
            datetime_col="tick_time",
            now_dt=(
                to_tz_naive_timestamp(now, label="load_push_df_for_dates.now")
                if now is not None
                else now_naive()
            ),
            tolerance_minutes=2,
            label="load_push_df_for_dates.concat",
        )

    if market_hours_only and not out.empty:
        out = filter_market_session_ticks(
            out,
            datetime_col="tick_time",
            label="load_push_df_for_dates.concat",
        )

    logger.info(
        "[summary.recovery.loaders_push.db_loader] load_push_df_for_dates done target_dates=%s rows=%d symbols=%d start_dt=%s end_dt=%s requested_symbols=%d",
        [str(x) for x in target_dates_list],
        len(out),
        int(out["symbol"].nunique()) if not out.empty and "symbol" in out.columns else 0,
        start_dt_safe,
        end_dt_safe,
        len(normalize_symbols(symbols)),
    )

    log_df_date_breakdown(out, label="load_push_df_for_dates")
    return out


__all__ = [
    "load_push_df_for_dates",
]