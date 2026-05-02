# ============================================================
# File   : scheduler_jobs/summary/safe_io.py
# Version: PRODUCTION-STABLE-SUMMARY-SAFE-IO-V1.2-USE-COMMON-LIQUIDITY-FILTER
# ------------------------------------------------------------
# 【概要】
#   summary DB保存 / PUSH表示 / RANKING表示の安全ラッパー。
#
# 【主な機能】
#   - 空DF保存禁止
#   - 空DF表示禁止
#   - 表示前後ログ
#   - TOP10表示前に共通流動性フィルタを適用
#   - trading/summary/filters/liquidity_filter.py を利用
#   - 例外を握りつぶし scheduler を防衛
#
# 【重要】
#   - DB保存前には流動性フィルタをかけない。
#   - 表示前だけ低出来高・低売買代金銘柄を除外する。
#   - 実エントリー側 runner.py にも同じ共通フィルタを入れるのが望ましい。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd

from .cache_writer import save_merged_summary
from .display_runner import display_push_summary, display_ranking_summary
from .time_utils import resolve_display_slot
from .runner_utils import df_rows, is_nonempty_df, log_df_state

from trading.summary.filters.liquidity_filter import (
    filter_liquid_summary_for_display,
    log_liquidity_profile,
)

logger = logging.getLogger(__name__)


# ============================================================
# 保存安全ラッパー
# ============================================================

def save_summary_safe(df: pd.DataFrame, interval: int, source: str) -> bool:
    """
    summary DB へ保存する安全ラッパー。

    重要:
      rows=0 の空DFは保存しない。
      rows=0 保存は障害解析を難しくし、
      「定時サマリーが動いたように見えるが中身がない」
      状態を作るため skip する。

    注意:
      ここでは流動性フィルタをかけない。
      DBには低出来高銘柄も含めて保存し、
      表示・AIエントリー側で除外する方が原因解析しやすい。
    """
    try:
        rows = df_rows(df)

        if not is_nonempty_df(df):
            logger.warning(
                "[summary.runners] save_summary skipped source=%s interval=%s reason=empty_df rows=%d",
                source,
                interval,
                rows,
            )
            return False

        logger.info(
            "[summary.runners] save_summary start source=%s interval=%s rows=%d",
            source,
            interval,
            rows,
        )

        save_merged_summary(df, interval, source=source)

        logger.info(
            "[summary.runners] save_summary success source=%s interval=%s rows=%d",
            source,
            interval,
            rows,
        )
        return True

    except Exception:
        logger.exception(
            "[summary.runners] save_summary failed source=%s interval=%s",
            source,
            interval,
        )
        return False


# ============================================================
# PUSHサマリー表示安全ラッパー
# ============================================================

def display_push_summary_safe(df: pd.DataFrame, interval: int, now: dt.datetime) -> bool:
    """
    PUSHサマリー表示の安全ラッパー。
    空DFは表示層へ渡さない。

    処理順:
      1. 入力DFが空なら skip
      2. 表示スロットを解決
      3. 表示前DF状態ログ
      4. 共通流動性フィルタを適用
      5. フィルタ後DFが空なら skip
      6. display_push_summary() へ渡す
    """
    try:
        if not is_nonempty_df(df):
            logger.warning(
                "[summary.runners] display_push_summary skipped interval=%s reason=empty_df now=%s",
                interval,
                now,
            )
            return False

        _, slot_dt = resolve_display_slot(interval=interval, now=now)

        logger.info(
            "[summary.runners] display_push_summary start interval=%s rows=%d now=%s slot=%s",
            interval,
            len(df),
            now,
            slot_dt,
        )

        log_df_state("display_push_input_before_liquidity", interval, df)

        log_liquidity_profile(
            df,
            interval=interval,
            source="PUSH",
            label="display_push_before_filter",
        )

        display_df = filter_liquid_summary_for_display(
            df,
            interval=interval,
            source="PUSH",
        )

        if not is_nonempty_df(display_df):
            logger.warning(
                "[summary.runners] display_push_summary skipped interval=%s "
                "reason=empty_after_liquidity_filter before_rows=%d now=%s slot=%s",
                interval,
                len(df),
                now,
                slot_dt,
            )
            return False

        log_df_state("display_push_input_after_liquidity", interval, display_df)

        log_liquidity_profile(
            display_df,
            interval=interval,
            source="PUSH",
            label="display_push_after_filter",
        )

        display_push_summary(display_df, interval, now=now)

        logger.info(
            "[summary.runners] display_push_summary success interval=%s before_rows=%d after_rows=%d now=%s slot=%s",
            interval,
            len(df),
            len(display_df),
            now,
            slot_dt,
        )
        return True

    except Exception:
        logger.exception(
            "[summary.runners] display_push_summary failed interval=%s now=%s",
            interval,
            now,
        )
        return False


# ============================================================
# RANKINGサマリー表示安全ラッパー
# ============================================================

def display_ranking_summary_safe(df: pd.DataFrame, interval: int, now: dt.datetime) -> bool:
    """
    RANKINGサマリー表示の安全ラッパー。
    空DFは表示層へ渡さない。

    処理順:
      1. 入力DFが空なら skip
      2. 表示スロットを解決
      3. 表示前DF状態ログ
      4. 共通流動性フィルタを適用
      5. フィルタ後DFが空なら skip
      6. display_ranking_summary() へ渡す
    """
    try:
        if not is_nonempty_df(df):
            logger.warning(
                "[summary.runners] display_ranking_summary skipped interval=%s reason=empty_df now=%s",
                interval,
                now,
            )
            return False

        _, slot_dt = resolve_display_slot(interval=interval, now=now)

        logger.info(
            "[summary.runners] display_ranking_summary start interval=%s rows=%d now=%s slot=%s",
            interval,
            len(df),
            now,
            slot_dt,
        )

        log_df_state("display_ranking_input_before_liquidity", interval, df)

        log_liquidity_profile(
            df,
            interval=interval,
            source="RANKING",
            label="display_ranking_before_filter",
        )

        display_df = filter_liquid_summary_for_display(
            df,
            interval=interval,
            source="RANKING",
        )

        if not is_nonempty_df(display_df):
            logger.warning(
                "[summary.runners] display_ranking_summary skipped interval=%s "
                "reason=empty_after_liquidity_filter before_rows=%d now=%s slot=%s",
                interval,
                len(df),
                now,
                slot_dt,
            )
            return False

        log_df_state("display_ranking_input_after_liquidity", interval, display_df)

        log_liquidity_profile(
            display_df,
            interval=interval,
            source="RANKING",
            label="display_ranking_after_filter",
        )

        display_ranking_summary(display_df, interval, now=now)

        logger.info(
            "[summary.runners] display_ranking_summary success interval=%s before_rows=%d after_rows=%d now=%s slot=%s",
            interval,
            len(df),
            len(display_df),
            now,
            slot_dt,
        )
        return True

    except Exception:
        logger.exception(
            "[summary.runners] display_ranking_summary failed interval=%s now=%s",
            interval,
            now,
        )
        return False