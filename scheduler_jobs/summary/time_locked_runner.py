# ============================================================
# File   : scheduler_jobs/summary/time_locked_runner.py
# Version: PRODUCTION-STABLE-TIME-LOCKED-SUMMARY-RUNNER-V1.1-PUSH-ALL-INTERVAL-DISPLAY
# ------------------------------------------------------------
# 【概要】
#   毎時0分起点の 1min / 3min / 5min 定時実行を管理する。
#
# 【主な機能】
#   - market session 内: PUSH / RANKING 実行
#   - market session 外: PUSH の保存済み最新サマリー表示
#   - RANKING は従来通り 1min 毎分、3min :00/:03、5min :00/:05
#   - PUSH由来サマリーは、Discord/画面確認用に 1min / 3min / 5min を毎回表示する
#   - 時間外は ranking / entry を止める
#
# 【重要 V1.1】
#   ユーザー要望:
#     「PUSH由来のランキングは1分足、3分足、5分足を表示させて」
#
#   対応:
#     - 親tickが1分ごとに動くたび、PUSH側は [1, 3, 5] を対象にする
#     - RANKING側は従来通り interval boundary のみ実行する
#     - 無効化したい場合は環境変数 SUMMARY_PUSH_DISPLAY_ALL_INTERVALS=0
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
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


def _env_flag(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
        if raw in ("0", "false", "no", "off", "disable", "disabled"):
            return False
    except Exception:
        pass
    return bool(default)


def _push_display_all_intervals_enabled() -> bool:
    """
    PUSH由来サマリーは毎分 1m/3m/5m を表示する。

    3m/5mの計算自体が重い場合だけ、
    SUMMARY_PUSH_DISPLAY_ALL_INTERVALS=0 にすると従来の時間境界実行へ戻せる。
    """
    return _env_flag("SUMMARY_PUSH_DISPLAY_ALL_INTERVALS", default=True)


def _all_summary_intervals() -> list[int]:
    return [1, 3, 5]


def closed_market_display_targets(now: dt.datetime) -> list[int]:
    """
    時間外 / 昼休み / 休場日でも表示する対象 interval を作る。

    V1.1:
      PUSH表示は常に 1m/3m/5m を見たいので、既定では [1,3,5] を返す。
      従来周期へ戻す場合のみ SUMMARY_PUSH_DISPLAY_ALL_INTERVALS=0。
    """
    try:
        n = (now or now_naive()).replace(second=0, microsecond=0)
    except Exception:
        n = now_naive().replace(second=0, microsecond=0)

    if _push_display_all_intervals_enabled():
        return _all_summary_intervals()

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


def _push_targets_from_time_locked_targets(targets: list[int]) -> list[int]:
    """
    PUSH側だけは毎分 1m/3m/5m を表示対象にする。
    RANKING側は resolve_target_intervals の結果をそのまま使う。
    """
    if _push_display_all_intervals_enabled():
        return _all_summary_intervals()
    return sorted(list(dict.fromkeys(int(x) for x in (targets or []))))


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
      - PUSH: 既定で 1m / 3m / 5m を毎回計算・表示
      - RANKING: resolve_target_intervals(now) に従い、従来通り時間境界のみ計算・表示

    market session 外 / 昼休み / 休場日:
      - PUSH: 保存済み最新サマリー表示へ到達させる
      - RANKING は時間外では原則 skip
      - entry pipeline も時間外では skip
    """
    now = (now or now_naive()).replace(microsecond=0)
    in_session = is_market_session(now)

    ranking_targets = resolve_target_intervals(now)
    original_targets = list(ranking_targets or [])

    if not ranking_targets and not in_session:
        ranking_targets = closed_market_display_targets(now)
        logger.warning(
            "[summary.runners] time-locked targets rescued for closed-market display "
            "now=%s original_targets=%s rescued_targets=%s in_session=%s",
            now,
            original_targets,
            ranking_targets,
            in_session,
        )

    push_targets = _push_targets_from_time_locked_targets(ranking_targets)

    logger.info(
        "[summary.runners] time-locked tick now=%s push_targets=%s ranking_targets=%s original_targets=%s "
        "push_all_intervals=%s run_push=%s run_ranking=%s display=%s run_entry=%s in_session=%s",
        now,
        push_targets,
        ranking_targets,
        original_targets,
        _push_display_all_intervals_enabled(),
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

    if not push_targets and not ranking_targets:
        logger.info(
            "[summary.runners] time-locked tick skipped now=%s reason=no_target_intervals in_session=%s",
            now,
            in_session,
        )
        return out

    if run_push:
        for interval in push_targets:
            interval = int(interval)
            try:
                logger.info(
                    "[summary.runners] time-locked push begin interval=%s now=%s in_session=%s display=%s push_all_intervals=%s",
                    interval,
                    now,
                    in_session,
                    display,
                    _push_display_all_intervals_enabled(),
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
            "[summary.runners] time-locked push skipped now=%s reason=run_push_false push_targets=%s",
            now,
            push_targets,
        )

    if run_ranking and in_session:
        for interval in sorted(list(dict.fromkeys(int(x) for x in (ranking_targets or [])))):
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
            "[summary.runners] time-locked ranking skipped now=%s targets=%s reason=closed_market_or_lunch",
            now,
            ranking_targets,
        )

    else:
        logger.info(
            "[summary.runners] time-locked ranking skipped now=%s targets=%s reason=run_ranking_false",
            now,
            ranking_targets,
        )

    return out
