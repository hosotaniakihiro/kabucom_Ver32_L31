# ============================================================
# File   : scheduler_jobs/summary/time_locked_runner.py
# Version: PRODUCTION-STABLE-TIME-LOCKED-SUMMARY-RUNNER-V1.0
# ------------------------------------------------------------
# 【概要】
#   毎時0分起点の 1min / 3min / 5min 定時実行を管理する。
#
# 【主な機能】
#   - market session 内: PUSH / RANKING 実行
#   - market session 外: PUSH の保存済み最新サマリー表示
#   - 1min 毎分、3min :00/:03、5min :00/:05
#   - 時間外は ranking / entry を止める
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from .display_prepare import latest_dt_str
from .runner_core import job_summary, job_ranking_summary
from .time_utils import (
    now_naive,
    resolve_target_intervals,
    is_market_session,
)

logger = logging.getLogger(__name__)


def closed_market_display_targets(now: dt.datetime) -> list[int]:
    """
    時間外 / 昼休み / 休場日でも表示する対象 interval を作る。

    要件:
      - 1分足: 毎分
      - 3分足: 毎時0分起点で 00,03,06,...,57
      - 5分足: 毎時0分起点で 00,05,10,...,55

    resolve_target_intervals(now) が market session 外で [] を返しても、
    保存済み最新サマリー表示へ到達させるための救済。
    """
    try:
        n = (now or now_naive()).replace(second=0, microsecond=0)
    except Exception:
        n = now_naive().replace(second=0, microsecond=0)

    targets = [1]

    try:
        if int(n.minute) % 3 == 0:
            targets.append(3)
    except Exception:
        pass

    try:
        if int(n.minute) % 5 == 0:
            targets.append(5)
    except Exception:
        pass

    return sorted(list(dict.fromkeys(targets)))


def run_time_locked_summary_jobs(
    *,
    now: Optional[dt.datetime] = None,
    run_push: bool = True,
    run_ranking: bool = True,
    display: bool = True,
    run_entry: bool = True,
) -> dict[str, dict[int, pd.DataFrame]]:
    """
    毎時0分起点の定時サマリー実行。

    market session 内:
      - resolve_target_intervals(now) に従い PUSH計算 / RANKING計算 / 表示

    market session 外 / 昼休み / 休場日:
      - resolve_target_intervals(now) が [] でも早期returnしない
      - 1m/3m/5m の周期に従い job_summary() を呼ぶ
      - job_summary() 内の display_closed_market_push_summary() に到達させる
      - RANKING は時間外では原則 skip
      - entry pipeline も時間外では skip
    """
    now = (now or now_naive()).replace(microsecond=0)
    in_session = is_market_session(now)

    targets = resolve_target_intervals(now)
    original_targets = list(targets or [])

    if not targets and not in_session:
        targets = closed_market_display_targets(now)
        logger.warning(
            "[summary.runners] time-locked targets rescued for closed-market display "
            "now=%s original_targets=%s rescued_targets=%s in_session=%s",
            now,
            original_targets,
            targets,
            in_session,
        )

    logger.info(
        "[summary.runners] time-locked tick now=%s targets=%s original_targets=%s "
        "run_push=%s run_ranking=%s display=%s run_entry=%s in_session=%s",
        now,
        targets,
        original_targets,
        run_push,
        run_ranking,
        display,
        run_entry,
        in_session,
    )

    out: dict[str, dict[int, pd.DataFrame]] = {
        "push": {},
        "ranking": {},
    }

    if not targets:
        logger.info(
            "[summary.runners] time-locked tick skipped now=%s reason=no_target_intervals in_session=%s",
            now,
            in_session,
        )
        return out

    for interval in targets:
        interval = int(interval)

        if run_push:
            try:
                logger.info(
                    "[summary.runners] time-locked push begin interval=%s now=%s in_session=%s display=%s",
                    interval,
                    now,
                    in_session,
                    display,
                )

                out["push"][interval] = job_summary(
                    interval,
                    display=display,
                    now=now,
                    run_entry=(bool(run_entry) and bool(in_session)),
                )

                logger.info(
                    "[summary.runners] time-locked push end interval=%s rows=%d latest_dt=%s in_session=%s",
                    interval,
                    len(out["push"][interval]) if isinstance(out["push"][interval], pd.DataFrame) else 0,
                    latest_dt_str(out["push"][interval]) if isinstance(out["push"][interval], pd.DataFrame) else None,
                    in_session,
                )

            except Exception:
                logger.exception(
                    "[summary.runners] time-locked push job failed interval=%s now=%s in_session=%s",
                    interval,
                    now,
                    in_session,
                )
                out["push"][interval] = pd.DataFrame()

        else:
            logger.info(
                "[summary.runners] time-locked push skipped interval=%s now=%s reason=run_push_false",
                interval,
                now,
            )

        if run_ranking and in_session:
            try:
                logger.info(
                    "[summary.runners] time-locked ranking begin interval=%s now=%s",
                    interval,
                    now,
                )

                out["ranking"][interval] = job_ranking_summary(
                    interval,
                    display=display,
                    now=now,
                    run_entry=False,
                )

                logger.info(
                    "[summary.runners] time-locked ranking end interval=%s rows=%d latest_dt=%s",
                    interval,
                    len(out["ranking"][interval]) if isinstance(out["ranking"][interval], pd.DataFrame) else 0,
                    latest_dt_str(out["ranking"][interval]) if isinstance(out["ranking"][interval], pd.DataFrame) else None,
                )

            except Exception:
                logger.exception(
                    "[summary.runners] time-locked ranking job failed interval=%s now=%s",
                    interval,
                    now,
                )
                out["ranking"][interval] = pd.DataFrame()

        elif run_ranking and not in_session:
            logger.info(
                "[summary.runners] time-locked ranking skipped interval=%s now=%s reason=closed_market_or_lunch",
                interval,
                now,
            )

        else:
            logger.info(
                "[summary.runners] time-locked ranking skipped interval=%s now=%s reason=run_ranking_false",
                interval,
                now,
            )

    return out