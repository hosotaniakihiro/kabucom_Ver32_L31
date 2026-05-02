



#====================================================================================================
# scheduler_jobs/summary/fallback_loader.py
#====================================================================================================
# ============================================================
# File   : scheduler_jobs/summary/fallback_loader.py
# Ver    : PRODUCTION-STABLE-SUMMARY-FALLBACK-LOADER-V2.2-CLOSED-MARKET-RECOVERY
#          -NO-RECOVERY-FALLBACK-FOR-1M-PUSH
#          -EXPECTED-SLOT-AWARE
#          -NOW-PASSTHROUGH
#          -STALE-FALLBACK-SUPPRESSED
# ------------------------------------------------------------
# ✔ DB / cache fallback
# ✔ push-like source filter
# ✔ best candidate selection
# ✔ expected_slot 以下の最新 slot に整列
# ✔ now を scheduler 側から伝搬
# ✔ 古い fallback を安易に採用しない
# ✔ 1分 PUSH fallback では市場中の stale を抑制
# ✔ 昼休み/時間外は recovery 系 source も候補に含める
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .display_prepare import normalize_df, extract_latest_timestamp, latest_dt_str, symbols_count
from .quality_guards import looks_uncomputed_push_df, looks_uncomputed_ranking_df
from .time_utils import (
    now_naive,
    today_date,
    is_future_timestamp,
    is_today_timestamp,
    is_fresh_timestamp,
    age_minutes,
    floor_to_interval,
    is_market_session,
)

logger = logging.getLogger(__name__)

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:
        class _FallbackGlobalData:
            pass
        global_data = _FallbackGlobalData()


def safe_getattr(obj, name: str, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _primary_dt_col(df: pd.DataFrame) -> Optional[str]:
    for c in ("datetime", "end_time", "time", "start_time", "snapshot_time"):
        if c in df.columns:
            return c
    return None


def _slot_aligned_latest_rows(
    df: pd.DataFrame,
    *,
    interval: int,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    """
    fallback 候補を expected_slot 以下の最新 slot に揃える。
    """
    x = normalize_df(df)
    if x.empty:
        return x

    col = _primary_dt_col(x)
    if not col:
        return x

    now = (now or now_naive()).replace(tzinfo=None, microsecond=0)

    try:
        s = pd.to_datetime(x[col], errors="coerce")
        try:
            s = s.dt.tz_localize(None)
        except Exception:
            pass

        x = x.loc[s.notna()].copy()
        if x.empty:
            return x

        s = pd.to_datetime(x[col], errors="coerce")
        try:
            s = s.dt.tz_localize(None)
        except Exception:
            pass

        x["_dt"] = s
        expected_slot = pd.Timestamp(floor_to_interval(now, interval))

        try:
            x["_slot"] = x["_dt"].dt.floor(f"{int(interval)}min")
        except Exception:
            x["_slot"] = x["_dt"]

        past = x.loc[x["_slot"] <= expected_slot].copy()
        if past.empty:
            logger.warning(
                "[summary.fallback_loader] slot align no past rows interval=%s expected_slot=%s latest_dt=%s",
                interval,
                expected_slot,
                latest_dt_str(x),
            )
            return pd.DataFrame()

        chosen_slot = past["_slot"].max()
        out = past.loc[past["_slot"] == chosen_slot].copy()

        logger.info(
            "[summary.fallback_loader] slot aligned interval=%s expected_slot=%s chosen_slot=%s rows=%s symbols=%s",
            interval,
            expected_slot,
            chosen_slot,
            len(out),
            symbols_count(out),
        )

        return out.drop(columns=["_dt", "_slot"], errors="ignore").reset_index(drop=True)

    except Exception:
        logger.exception("[summary.fallback_loader] slot align failed interval=%s", interval)
        return x.reset_index(drop=True)


def select_best_candidate(
    candidates: list[tuple[str, pd.DataFrame]],
    *,
    interval: int,
    for_ranking: bool = False,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    usable: list[tuple[str, pd.DataFrame, pd.Timestamp, bool, float]] = []
    now = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    expected_slot = floor_to_interval(now, interval)

    for name, src in candidates:
        df = normalize_df(src)
        if df.empty:
            logger.info(
                "[summary.fallback_loader] candidate empty name=%s interval=%s",
                name,
                interval,
            )
            continue

        df = _slot_aligned_latest_rows(df, interval=interval, now=now)
        if df.empty:
            logger.warning(
                "[summary.fallback_loader] candidate empty after slot align name=%s interval=%s expected_slot=%s",
                name,
                interval,
                expected_slot,
            )
            continue

        ts = extract_latest_timestamp(df)
        if ts is None:
            logger.warning(
                "[summary.fallback_loader] candidate has no timestamp name=%s interval=%s",
                name,
                interval,
            )
            continue

        if is_future_timestamp(ts, interval=interval, now=now):
            logger.warning(
                "[summary.fallback_loader] fallback candidate skipped by future-ts name=%s interval=%s latest_dt=%s",
                name,
                interval,
                str(ts),
            )
            continue

        if not is_today_timestamp(ts, now=now):
            logger.warning(
                "[summary.fallback_loader] fallback candidate skipped by non-today name=%s interval=%s latest_dt=%s today=%s",
                name,
                interval,
                str(ts),
                today_date(now=now),
            )
            continue

        if for_ranking:
            if looks_uncomputed_ranking_df(df):
                logger.warning(
                    "[summary.fallback_loader] fallback candidate skipped by uncomputed-ranking name=%s interval=%s latest_dt=%s",
                    name,
                    interval,
                    str(ts),
                )
                continue
        else:
            if looks_uncomputed_push_df(df):
                logger.warning(
                    "[summary.fallback_loader] fallback candidate skipped by uncomputed-push name=%s interval=%s latest_dt=%s",
                    name,
                    interval,
                    str(ts),
                )
                continue

        fresh = is_fresh_timestamp(ts, interval, for_ranking=for_ranking, now=now)
        age = age_minutes(ts, now=now)
        age = float(age) if age is not None else 999999.0

        logger.info(
            "[summary.fallback_loader] candidate usable-check name=%s interval=%s rows=%s symbols=%s latest_dt=%s age_min=%.2f fresh=%s expected_slot=%s",
            name,
            interval,
            len(df),
            symbols_count(df),
            str(ts),
            age,
            fresh,
            expected_slot,
        )

        usable.append((name, df, ts, fresh, age))

    if not usable:
        logger.warning(
            "[summary.fallback_loader] no usable fallback candidates interval=%s for_ranking=%s expected_slot=%s",
            interval,
            for_ranking,
            expected_slot,
        )
        return pd.DataFrame()

    fresh_only = [x for x in usable if x[3]]

    # 1分 PUSH は市場中だけ stale fallback を表示しない。
    # 昼休み/時間外は 11:30 / 15:30 の確定済みサマリーを表示・再保存したいので usable を許可する。
    if interval <= 1 and not for_ranking and is_market_session(now):
        pool = fresh_only
        if not pool:
            logger.warning(
                "[summary.fallback_loader] suppressed stale push fallback during market session interval=%s expected_slot=%s",
                interval,
                expected_slot,
            )
            return pd.DataFrame()
    else:
        pool = fresh_only if fresh_only else usable

    pool.sort(key=lambda x: (x[2], len(x[1])), reverse=True)

    chosen_name, chosen_df, chosen_ts, chosen_fresh, chosen_age = pool[0]

    logger.info(
        "[summary.fallback_loader] fallback chosen name=%s interval=%s rows=%s symbols=%s latest_dt=%s age_min=%.2f fresh=%s expected_slot=%s",
        chosen_name,
        interval,
        len(chosen_df),
        symbols_count(chosen_df),
        str(chosen_ts),
        chosen_age,
        chosen_fresh,
        expected_slot,
    )
    return chosen_df.reset_index(drop=True)


def today_summary_db_path(*, now: Optional[dt.datetime] = None) -> Optional[Path]:
    candidates: list[str] = []

    for attr in ("summary_db_path", "current_summary_db_path", "resolved_summary_db_path"):
        try:
            v = safe_getattr(global_data, attr, None)
            if isinstance(v, (str, Path)) and str(v).strip():
                candidates.append(str(v))
        except Exception:
            pass

    base_date = today_date(now=now)
    candidates.append(
        rf"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary\summary{base_date:%Y%m%d}.db"
    )

    for raw in candidates:
        try:
            p = Path(raw)
            if p.exists():
                return p
        except Exception:
            pass

    return None


def summary_table_name(interval: int) -> str:
    return f"stock_summary_{int(interval)}min"


def load_latest_summary_from_db(
    interval: int,
    *,
    limit_rows: int = 20000,
    source_filter: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    db_path = today_summary_db_path(now=now)
    if db_path is None:
        return pd.DataFrame()

    table = summary_table_name(interval)

    sql = f"""
    SELECT *
    FROM {table}
    WHERE 1=1
    """
    params: list[Any] = []

    if source_filter:
        sql += " AND source = ? "
        params.append(source_filter)

    sql += """
    ORDER BY datetime DESC
    LIMIT ?
    """
    params.append(int(limit_rows))

    try:
        with sqlite3.connect(str(db_path)) as conn:
            df = pd.read_sql_query(sql, conn, params=params)
    except Exception:
        logger.debug(
            "[summary.fallback_loader] db fallback load failed interval=%s table=%s source=%s path=%s",
            interval,
            table,
            source_filter,
            db_path,
            exc_info=True,
        )
        return pd.DataFrame()

    df = normalize_df(df)
    if df.empty:
        return df

    df = _slot_aligned_latest_rows(df, interval=interval, now=now)

    logger.info(
        "[summary.fallback_loader] db fallback loaded interval=%s source=%s rows=%s symbols=%s latest_dt=%s path=%s",
        interval,
        source_filter or "*",
        len(df),
        symbols_count(df),
        latest_dt_str(df),
        db_path,
    )
    return df


def filter_push_like_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    1分定時表示に古い recovery 系が混ざると遅延表示の原因になるため、
    summary_recovery_push はここでは採用しない。
    """
    df = normalize_df(df)
    if df.empty or "source" not in df.columns:
        return df

    try:
        src = df["source"].astype(str)
        mask = (
            src.str.contains("push_stream", case=False, na=False)
            | src.str.contains("yahoo_pipeline", case=False, na=False)
            | src.str.contains("incremental", case=False, na=False)
            | src.str.contains("summary_recovery", case=False, na=False)
            | src.str.contains("resample", case=False, na=False)
        )
        out = df.loc[mask].copy()
        logger.info(
            "[summary.fallback_loader] push-like filter rows=%s -> %s source_dist=%s",
            len(df),
            len(out),
            {} if out.empty else out["source"].astype(str).value_counts().head(10).to_dict(),
        )
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[summary.fallback_loader] push-like filter failed")
        return df


def fallback_push_summary_df(
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    interval = int(interval)
    now = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    candidates: list[tuple[str, Any]] = []

    # 通常保存 source + recovery / resample source も候補にする。
    # select_best_candidate 側で future / non-today / stale を評価する。
    db_sources = (
        "push_stream",
        "yahoo_pipeline",
        "incremental",
        "summary_recovery_push_1m",
        "summary_recovery_resample_3m",
        "summary_recovery_resample_5m",
        "summary_recovery",
    )

    for src in db_sources:
        try:
            candidates.append(
                (
                    f"db.stock_summary_{interval}min[{src}]",
                    load_latest_summary_from_db(interval, source_filter=src, now=now),
                )
            )
        except Exception:
            logger.debug(
                "[summary.fallback_loader] db push-source fallback failed interval=%s src=%s",
                interval,
                src,
                exc_info=True,
            )

    for attr in (
        f"push_summary_{interval}min",
        f"push_summary_{interval}",
        f"latest_push_summary_{interval}min",
        f"latest_push_summary_{interval}",
    ):
        try:
            candidates.append((f"global_data.{attr}", safe_getattr(global_data, attr, None)))
        except Exception:
            pass

    for attr in ("push_summary_by_interval", "latest_push_summary_by_interval"):
        try:
            d = safe_getattr(global_data, attr, None)
            if isinstance(d, dict):
                candidates.append((f"global_data.{attr}[{interval}]", d.get(interval)))
        except Exception:
            pass

    normalized_candidates: list[tuple[str, pd.DataFrame]] = []
    for name, src in candidates:
        df = normalize_df(src)
        df = filter_push_like_rows(df)
        df = _slot_aligned_latest_rows(df, interval=interval, now=now)
        if not df.empty:
            normalized_candidates.append((name, df))

    df = select_best_candidate(
        normalized_candidates,
        interval=interval,
        for_ranking=False,
        now=now,
    )
    if not df.empty:
        return df

    logger.warning(
        "[summary.fallback_loader] fallback push summary empty interval=%s now=%s",
        interval,
        now,
    )
    return pd.DataFrame()


def fallback_ranking_summary_df(
    interval: int,
    *,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    interval = int(interval)
    now = (now or now_naive()).replace(tzinfo=None, microsecond=0)
    candidates: list[tuple[str, Any]] = []

    try:
        from trading.ranking.ranking_summary_engine import get_latest_ranking_summary  # type: ignore
        candidates.append(
            (f"ranking_cache.get_latest_ranking_summary({interval})", get_latest_ranking_summary(interval))
        )
    except Exception:
        logger.debug("[summary.fallback_loader] ranking cache fallback failed interval=%s", interval, exc_info=True)

    for attr in (
        "latest_ranking_summary_by_interval",
        "ranking_summary_by_interval",
        "ranking_summary_cache",
    ):
        try:
            d = safe_getattr(global_data, attr, None)
            if isinstance(d, dict):
                candidates.append((f"global_data.{attr}[{interval}]", d.get(interval)))
        except Exception:
            pass

    for attr in (f"latest_ranking_summary_{interval}m", f"ranking_summary_{interval}m"):
        try:
            candidates.append((f"global_data.{attr}", safe_getattr(global_data, attr, None)))
        except Exception:
            pass

    normalized_candidates: list[tuple[str, pd.DataFrame]] = []
    for name, src in candidates:
        df = normalize_df(src)
        df = _slot_aligned_latest_rows(df, interval=interval, now=now)
        if not df.empty:
            normalized_candidates.append((name, df))

    df = select_best_candidate(
        normalized_candidates,
        interval=interval,
        for_ranking=True,
        now=now,
    )
    if not df.empty:
        return df

    logger.warning(
        "[summary.fallback_loader] fallback ranking summary empty interval=%s now=%s",
        interval,
        now,
    )
    return pd.DataFrame()

