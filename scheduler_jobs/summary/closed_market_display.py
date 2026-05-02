#====================================================================================================
# scheduler_jobs/summary/closed_market_display.py
#====================================================================================================
# ============================================================
# File   : scheduler_jobs/summary/closed_market_display.py
# Version: PRODUCTION-STABLE-CLOSED-MARKET-DISPLAY-V1.1-EMPTY-REBUILD
# ------------------------------------------------------------
# 【概要】
#   時間外 / 昼休み / 休場日の PUSHサマリー表示を担当する。
#
# 【主な機能】
#   - 市場時間内の最新確定サマリーをDBから読み込む
#   - Yahoo補完後のDB更新を次回表示に反映
#   - DBが空の場合はPUSH fallbackを試す
#   - DB/fallback が空の場合、表示対象slotで PUSH runner 再計算を試す
#   - 保存 / 表示は safe_io に委譲
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd
from sqlalchemy import text

from database.session import summary_engine
from .dependencies import resolve_push_summary_runner
from .display_prepare import normalize_df, latest_dt_str, clamp_future_rows
from .fallback_loader import fallback_push_summary_df, filter_push_like_rows
from .output_normalizer import normalize_runner_output
from .quality_guards import looks_uncomputed_push_df
from .safe_io import save_summary_safe, display_push_summary_safe
from .runner_utils import log_df_state, call_runner_with_optional_now
from .time_utils import now_naive, resolve_display_slot

logger = logging.getLogger(__name__)


_INTERVAL_TABLE_MAP = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}


def load_latest_market_hours_summary(interval: int) -> pd.DataFrame:
    """
    時間外表示用:
    市場時間内の最新確定サマリーを DB から毎回取得する。
    引け後 Yahoo 補完で DB が更新された場合も、次回表示へ反映される。
    """
    table = _INTERVAL_TABLE_MAP.get(int(interval))
    if not table:
        return pd.DataFrame()

    if summary_engine is None:
        logger.warning("[summary.runners] summary_engine unavailable interval=%s", interval)
        return pd.DataFrame()

    try:
        sql = text(f"""
            WITH latest_dt AS (
                SELECT MAX(datetime) AS max_dt
                FROM {table}
                WHERE datetime IS NOT NULL
                  AND time(datetime) >= '09:00:00'
                  AND (
                        time(datetime) <= '11:30:00'
                     OR time(datetime) >= '12:30:00'
                  )
                  AND time(datetime) <= '15:30:00'
            )
            SELECT *
            FROM {table}
            WHERE datetime = (SELECT max_dt FROM latest_dt)
        """)

        with summary_engine.connect() as conn:
            df = pd.read_sql(sql, conn)

        df = normalize_df(df)
        df = clamp_future_rows(df, interval=interval, now=now_naive())

        logger.info(
            "[summary.runners] latest persisted summary interval=%s rows=%d latest_dt=%s",
            interval,
            len(df),
            latest_dt_str(df),
        )
        return df

    except Exception:
        logger.exception(
            "[summary.runners] latest persisted summary load failed interval=%s table=%s",
            interval,
            table,
        )
        return pd.DataFrame()


def _resolve_closed_market_rebuild_now(now: dt.datetime) -> dt.datetime:
    """
    昼休み/時間外に「本来表示すべき確定slot」を runner に渡す。

    例:
      12:29 -> 11:30
      16:00 -> 15:30

    resolve_display_slot の戻り値は版によって tuple/date/datetime が揺れるため、
    互換的に吸収する。
    """
    base = (now or now_naive()).replace(microsecond=0)

    try:
        slot = resolve_display_slot(base)
        if isinstance(slot, tuple) and len(slot) >= 2 and isinstance(slot[1], dt.datetime):
            return slot[1].replace(microsecond=0)
        if isinstance(slot, dt.datetime):
            return slot.replace(microsecond=0)
    except Exception:
        logger.debug(
            "[summary.runners] resolve closed-market rebuild slot failed now=%s",
            base,
            exc_info=True,
        )

    return base


def rebuild_closed_market_push_summary(
    interval: int,
    now: Optional[dt.datetime],
) -> pd.DataFrame:
    """
    persisted/fallback が空のときだけ使う救済。

    通常の PUSH summary runner を、昼休みなら 11:30、引け後なら 15:30 などの
    表示対象slotで1回だけ呼び、計算できた場合は保存して返す。
    ここでは AI entry は起動しない。
    """
    interval = int(interval)
    now = (now or now_naive()).replace(microsecond=0)
    rebuild_now = _resolve_closed_market_rebuild_now(now)

    runner = resolve_push_summary_runner()
    if not callable(runner):
        logger.warning(
            "[summary.runners] closed-market rebuild skipped interval=%s reason=push_runner_unavailable now=%s rebuild_now=%s",
            interval,
            now,
            rebuild_now,
        )
        return pd.DataFrame()

    try:
        logger.warning(
            "[summary.runners] closed-market persisted/fallback empty -> trying one-shot rebuild interval=%s now=%s rebuild_now=%s runner=%s",
            interval,
            now,
            rebuild_now,
            getattr(runner, "__name__", repr(runner)),
        )

        result = call_runner_with_optional_now(
            runner,
            interval=interval,
            now=rebuild_now,
            display=False,
            run_entry=False,
        )
        df, meta = normalize_runner_output(result)

        df = normalize_df(df)
        df = filter_push_like_rows(df)
        df = clamp_future_rows(df, interval=interval, now=rebuild_now)
        log_df_state("closed-market rebuild", interval, df)

        if df.empty:
            logger.warning(
                "[summary.runners] closed-market rebuild empty interval=%s now=%s rebuild_now=%s meta=%s",
                interval,
                now,
                rebuild_now,
                meta,
            )
            return df

        if looks_uncomputed_push_df(df):
            logger.warning(
                "[summary.runners] closed-market rebuild skipped interval=%s reason=uncomputed_push latest_dt=%s",
                interval,
                latest_dt_str(df),
            )
            return pd.DataFrame()

        save_summary_safe(df, interval, source="push")
        logger.info(
            "[summary.runners] closed-market rebuild saved interval=%s rows=%d latest_dt=%s",
            interval,
            len(df),
            latest_dt_str(df),
        )
        return df

    except Exception:
        logger.exception(
            "[summary.runners] closed-market rebuild failed interval=%s now=%s rebuild_now=%s",
            interval,
            now,
            rebuild_now,
        )
        return pd.DataFrame()


def display_closed_market_push_summary(
    interval: int,
    now: Optional[dt.datetime],
) -> pd.DataFrame:
    """
    時間外の PUSH サマリー表示。
    DB保存済みの最新確定足を優先し、なければ PUSH fallback、最後に1回だけ再計算を試す。
    """
    now = (now or now_naive()).replace(microsecond=0)

    df = load_latest_market_hours_summary(interval)
    log_df_state("closed-market persisted", interval, df)

    if df.empty:
        logger.warning(
            "[summary.runners] closed-market persisted empty interval=%s -> trying push fallback",
            interval,
        )

        df = fallback_push_summary_df(interval, now=now)
        df = normalize_df(df)
        df = filter_push_like_rows(df)
        df = clamp_future_rows(df, interval=interval, now=now)

        log_df_state("closed-market push fallback", interval, df)

    if df.empty:
        df = rebuild_closed_market_push_summary(interval=interval, now=now)
        df = normalize_df(df)
        df = filter_push_like_rows(df)
        df = clamp_future_rows(df, interval=interval, now=_resolve_closed_market_rebuild_now(now))
        log_df_state("closed-market rebuild after normalize", interval, df)

    if df.empty:
        logger.warning(
            "[summary.runners] closed-market push display skipped interval=%s reason=no_persisted_or_fallback_or_rebuild_rows now=%s",
            interval,
            now,
        )
        return df

    save_summary_safe(df, interval, source="push")
    display_push_summary_safe(df, interval, now=now)

    return df

