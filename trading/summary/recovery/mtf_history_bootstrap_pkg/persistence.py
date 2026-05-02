# ============================================================
# File   : trading/summary/recovery/mtf_history_bootstrap_pkg/persistence.py
# Version: PRODUCTION-STABLE-REV1.1-PERSISTENCE-LOCK-SAFE
# ------------------------------------------------------------
# 【概要】
#   summary DB 保存 / recovery cache 更新
#
# 【REV1.1 修正点】
#   ✔ 起動時1分足履歴の大量UPSERTを原則禁止
#   ✔ source=mtf_history_bootstrap_1min_history の 1min 保存をskip
#   ✔ summary_saver_bulk(skip_if_busy=True) を優先
#   ✔ DB locked 時に起動を止めず warning で継続
#   ✔ recovery.persistence.upsert_summary_df は fallback 扱い
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

from .datetime_guard import drop_future_datetime_rows
from .constants import SAVE_BOOTSTRAP_1MIN_HISTORY

logger = logging.getLogger(__name__)


def _source_values(df: pd.DataFrame) -> set[str]:
    try:
        if df is None or df.empty or "source" not in df.columns:
            return set()
        return {
            str(x).strip()
            for x in df["source"].dropna().unique().tolist()
            if str(x).strip()
        }
    except Exception:
        return set()


def _is_bootstrap_1m_history(df: pd.DataFrame, *, interval: int) -> bool:
    """
    起動時に summary DB から読んだ1分足履歴を再保存しようとしているか判定する。
    """
    if int(interval) != 1:
        return False

    sources = _source_values(df)

    if not sources:
        return False

    for s in sources:
        if s.startswith("mtf_history_bootstrap_1min_history"):
            return True

    return False


def _is_locked_error(e: BaseException) -> bool:
    msg = str(e).lower()
    return (
        "database is locked" in msg
        or "database locked" in msg
        or "database is busy" in msg
        or "sqlite_busy" in msg
        or "operationalerror" in msg and "locked" in msg
    )


def save_summary(
    df: pd.DataFrame,
    *,
    interval: int,
    allow_1m_history: bool = SAVE_BOOTSTRAP_1MIN_HISTORY,
    prefer_bulk: bool = True,
) -> None:
    """
    summary DBへ保存する。

    重要:
      - 起動時の 1min full history は原則DB保存しない
      - 3min / 5min は保存する
      - bulk_upsert_summary(skip_if_busy=True) を優先してロック耐性を上げる
    """
    if df is None or df.empty:
        return

    interval = int(interval)

    df = drop_future_datetime_rows(df, interval=interval, label="before_save")
    if df.empty:
        logger.warning("[MTF HISTORY BOOTSTRAP] save skipped no valid datetime interval=%s", interval)
        return

    if interval == 1 and not bool(allow_1m_history):
        if _is_bootstrap_1m_history(df):
            logger.warning(
                "[MTF HISTORY BOOTSTRAP] skip 1min history upsert rows=%s "
                "reason=avoid_massive_startup_lock allow_1m_history=%s sources=%s",
                len(df),
                allow_1m_history,
                sorted(_source_values(df))[:5],
            )
            return

        logger.warning(
            "[MTF HISTORY BOOTSTRAP] skip interval=1 startup save rows=%s "
            "reason=1min_persist_disabled allow_1m_history=%s",
            len(df),
            allow_1m_history,
        )
        return

    # ------------------------------------------------------------
    # 1) lock-safe bulk saver を優先
    # ------------------------------------------------------------
    if prefer_bulk:
        try:
            from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary

            bulk_upsert_summary(df, interval=interval, skip_if_busy=True)
            logger.info(
                "[MTF HISTORY BOOTSTRAP] saved via summary_saver_bulk interval=%s rows=%s",
                interval,
                len(df),
            )
            return

        except Exception as e:
            if _is_locked_error(e):
                logger.warning(
                    "[MTF HISTORY BOOTSTRAP] save skipped because db locked interval=%s rows=%s err=%s",
                    interval,
                    len(df),
                    e,
                )
                return

            logger.warning(
                "[MTF HISTORY BOOTSTRAP] summary_saver_bulk failed interval=%s rows=%s -> fallback",
                interval,
                len(df),
                exc_info=True,
            )

    # ------------------------------------------------------------
    # 2) fallback: recovery.persistence
    # ------------------------------------------------------------
    try:
        from trading.summary.recovery.persistence import upsert_summary_df

        upsert_summary_df(df, interval=interval)
        logger.info(
            "[MTF HISTORY BOOTSTRAP] saved via recovery.persistence interval=%s rows=%s",
            interval,
            len(df),
        )
        return

    except Exception as e:
        if _is_locked_error(e):
            logger.warning(
                "[MTF HISTORY BOOTSTRAP] fallback save skipped because db locked interval=%s rows=%s err=%s",
                interval,
                len(df),
                e,
            )
            return

        logger.warning(
            "[MTF HISTORY BOOTSTRAP] summary save failed interval=%s rows=%s",
            interval,
            len(df),
            exc_info=True,
        )


def update_recovery_cache(df: pd.DataFrame, *, interval: int) -> None:
    if df is None or df.empty:
        return

    interval = int(interval)

    df = drop_future_datetime_rows(df, interval=interval, label="before_recovery_cache")
    if df.empty:
        logger.warning("[MTF HISTORY BOOTSTRAP] recovery cache skipped no valid datetime interval=%s", interval)
        return

    try:
        from trading.summary.recovery.persistence import update_global_cache

        update_global_cache(df, interval=interval)

    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] recovery cache update failed interval=%s", interval, exc_info=True)


__all__ = [
    "save_summary",
    "update_recovery_cache",
]