# ============================================================
# File   : trading/summary/recovery/loaders_ranking.py
# Ver    : PRODUCTION-STABLE-REV1.0-LOADERS-RANKING
# ------------------------------------------------------------
# ✔ ranking universe loaders
# ✔ global_data today ranking universe
# ✔ ranking DB date-based symbol restore
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Optional

import pandas as pd
from sqlalchemy import text

from global_state import global_data
from .loaders_common import coerce_date_set, normalize_symbols

logger = logging.getLogger(__name__)


def load_today_global_ranking_symbols() -> list[str]:
    """
    当日ランキング銘柄を global_data から取得する。
    """
    try:
        candidates = []

        for attr in (
            "ranking_universe_symbols",
            "global_ranking_universe_symbols",
            "today_ranking_universe_symbols",
        ):
            try:
                v = getattr(global_data, attr, None)
                if v:
                    candidates.append(v)
            except Exception:
                pass

        for attr in (
            "ranking_universe",
            "global_ranking_universe",
        ):
            try:
                v = getattr(global_data, attr, None)
                if isinstance(v, dict):
                    for key in ("symbols", "symbol_list", "items"):
                        if key in v and v[key]:
                            candidates.append(v[key])
                elif v:
                    candidates.append(v)
            except Exception:
                pass

        for fn_name in (
            "get_ranking_universe_symbols",
            "get_global_ranking_universe_symbols",
            "get_today_ranking_universe_symbols",
        ):
            try:
                fn = getattr(global_data, fn_name, None)
                if callable(fn):
                    v = fn()
                    if v:
                        candidates.append(v)
            except Exception:
                pass

        merged: list[str] = []
        for c in candidates:
            merged.extend(normalize_symbols(c))

        out = normalize_symbols(merged)
        logger.info(
            "[summary.recovery.loaders_ranking] load_today_global_ranking_symbols symbols=%d",
            len(out),
        )
        return out

    except Exception:
        logger.exception("[summary.recovery.loaders_ranking] load_today_global_ranking_symbols failed")
        return []


def load_ranking_symbols_for_dates(
    target_dates: Optional[Iterable] = None,
) -> list[str]:
    """
    ranking_snapshot_1min から target_dates の symbol 和集合を返す。
    """
    try:
        from database.session import get_ranking_engine

        engine = get_ranking_engine()
        if engine is None:
            logger.warning("[summary.recovery.loaders_ranking] load_ranking_symbols_for_dates ranking_engine unavailable")
            return []

        allowed_dates = sorted(coerce_date_set(target_dates))
        params = {}
        wheres = [
            "symbol IS NOT NULL",
            "TRIM(symbol) <> ''",
        ]

        if allowed_dates:
            date_parts = []
            for i, d in enumerate(allowed_dates):
                key = f"d{i}"
                date_parts.append("date(snapshot_time) = :" + key)
                params[key] = d
            wheres.append("(" + " OR ".join(date_parts) + ")")

        sql = f"""
        SELECT DISTINCT symbol
        FROM ranking_snapshot_1min
        WHERE {" AND ".join(wheres)}
        ORDER BY symbol
        """

        with engine.begin() as conn:
            df = pd.read_sql(text(sql), conn, params=params)

        symbols = normalize_symbols(df["symbol"].tolist() if "symbol" in df.columns else [])
        logger.info(
            "[summary.recovery.loaders_ranking] load_ranking_symbols_for_dates target_dates=%s symbols=%d",
            allowed_dates,
            len(symbols),
        )
        return symbols

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_ranking] load_ranking_symbols_for_dates failed target_dates=%s",
            list(target_dates) if target_dates is not None else None,
        )
        return []


def load_restore_target_symbols(
    *,
    target_dates: Optional[Iterable] = None,
    include_previous_day_from_db: bool = True,
) -> list[str]:
    """
    restore 対象銘柄を返す。
    - 当日: global ranking universe
    - 前日: ranking DB
    の union を想定。
    """
    try:
        dates = sorted(coerce_date_set(target_dates))
        today_symbols = load_today_global_ranking_symbols()

        prev_dates: list[str] = []
        if include_previous_day_from_db and len(dates) >= 2:
            prev_dates = [d for d in dates[:-1]]

        prev_symbols = load_ranking_symbols_for_dates(prev_dates) if prev_dates else []

        merged = normalize_symbols(list(today_symbols) + list(prev_symbols))

        logger.info(
            "[summary.recovery.loaders_ranking] load_restore_target_symbols today_symbols=%d prev_symbols=%d total=%d target_dates=%s",
            len(today_symbols),
            len(prev_symbols),
            len(merged),
            dates,
        )
        return merged

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_ranking] load_restore_target_symbols failed target_dates=%s",
            list(target_dates) if target_dates is not None else None,
        )
        return []


__all__ = [
    "load_today_global_ranking_symbols",
    "load_ranking_symbols_for_dates",
    "load_restore_target_symbols",
]