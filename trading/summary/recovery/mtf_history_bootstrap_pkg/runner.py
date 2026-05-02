# ============================================================
# File   : trading/summary/recovery/mtf_history_bootstrap_pkg/runner.py
# Version: PRODUCTION-STABLE-REV1.1-RUNNER-LOCK-SAFE
# ------------------------------------------------------------
# 【概要】
#   MTF history bootstrap のメイン orchestration
#
# 【REV1.1 修正点】
#   ✔ interval=1 の大量DB保存をデフォルト停止
#   ✔ 1min は計算・cache更新・表示用には使う
#   ✔ DB保存対象はデフォルトで 3min / 5min のみ
#   ✔ 1分足ロード上限を constants.DEFAULT_HISTORY_BARS_1M=390 に準拠
#   ✔ DB locked でも起動継続
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Optional

import pandas as pd

from .constants import (
    DEFAULT_INTERVALS,
    DEFAULT_HISTORY_BARS_1M,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_PERSIST_INTERVALS,
    SAVE_BOOTSTRAP_1MIN_HISTORY,
)
from .datetime_guard import (
    runtime_cutoff_now,
    drop_future_datetime_rows,
)
from .loader import load_1m_summary_history
from .resampler import rebuild_higher_tf_from_1m_history
from .indicators_scoring import apply_indicators_scoring_ready
from .persistence import save_summary, update_recovery_cache
from .cache import set_global_cache

logger = logging.getLogger(__name__)


def _as_int_tuple(values: Iterable[int]) -> tuple[int, ...]:
    out: list[int] = []
    for v in values:
        try:
            iv = int(v)
        except Exception:
            continue
        if iv not in out:
            out.append(iv)
    return tuple(out)


def run_mtf_history_bootstrap(
    *,
    intervals: Iterable[int] = DEFAULT_INTERVALS,
    max_rows_per_symbol_1m: int = DEFAULT_HISTORY_BARS_1M,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    symbols: Optional[Iterable[str]] = None,
    persist: bool = True,
    update_cache: bool = True,
    persist_intervals: Iterable[int] = DEFAULT_PERSIST_INTERVALS,
    allow_1m_history_persist: bool = SAVE_BOOTSTRAP_1MIN_HISTORY,
) -> dict[int, pd.DataFrame]:
    """
    起動時に呼ぶメイン関数。

    interval=1:
      stock_summary_1min 履歴を最小限ロードし、indicator/scoring を再計算する。
      ただし、デフォルトでは DB へ大量再保存しない。
      global_data cache 更新・表示用途には使う。

    interval=3/5:
      1min 履歴から resample して indicator/scoring を再計算する。
      デフォルトで DB 保存対象。

    Returns:
      interval -> full history dataframe
    """
    intervals_tuple = _as_int_tuple(intervals)
    persist_intervals_tuple = _as_int_tuple(persist_intervals)

    result: dict[int, pd.DataFrame] = {}

    try:
        max_rows_per_symbol_1m = int(max_rows_per_symbol_1m)
    except Exception:
        max_rows_per_symbol_1m = DEFAULT_HISTORY_BARS_1M

    # 450分を超えない安全上限
    if max_rows_per_symbol_1m > 390:
        logger.warning(
            "[MTF HISTORY BOOTSTRAP] max_rows_per_symbol_1m capped %s -> 390 reason=avoid_large_startup_load",
            max_rows_per_symbol_1m,
        )
        max_rows_per_symbol_1m = 390

    logger.info(
        "[MTF HISTORY BOOTSTRAP] start intervals=%s max_rows_per_symbol_1m=%s lookback_days=%s "
        "persist=%s persist_intervals=%s allow_1m_history_persist=%s update_cache=%s cutoff=%s",
        intervals_tuple,
        max_rows_per_symbol_1m,
        lookback_days,
        persist,
        persist_intervals_tuple,
        allow_1m_history_persist,
        update_cache,
        runtime_cutoff_now(),
    )

    df_1m = load_1m_summary_history(
        symbols=symbols,
        max_rows_per_symbol=max_rows_per_symbol_1m,
        lookback_days=lookback_days,
    )

    df_1m = drop_future_datetime_rows(df_1m, interval=1, label="run_loaded_1m")

    if df_1m.empty:
        logger.warning("[MTF HISTORY BOOTSTRAP] skipped: 1m history empty")
        return result

    for interval in intervals_tuple:
        try:
            if interval == 1:
                rebuilt = rebuild_higher_tf_from_1m_history(df_1m, interval=1)
            elif interval in (3, 5):
                rebuilt = rebuild_higher_tf_from_1m_history(df_1m, interval=interval)
            else:
                logger.warning("[MTF HISTORY BOOTSTRAP] unsupported interval=%s -> skip", interval)
                continue

            rebuilt = drop_future_datetime_rows(rebuilt, interval=int(interval), label=f"run_rebuilt_{interval}")

            if rebuilt.empty:
                logger.warning("[MTF HISTORY BOOTSTRAP] rebuilt empty interval=%s", interval)
                continue

            transformed = apply_indicators_scoring_ready(rebuilt, interval=interval)
            transformed = drop_future_datetime_rows(
                transformed,
                interval=int(interval),
                label=f"run_transformed_{interval}",
            )

            if transformed.empty:
                logger.warning("[MTF HISTORY BOOTSTRAP] transformed empty interval=%s", interval)
                continue

            should_persist = (
                bool(persist)
                and int(interval) in persist_intervals_tuple
            )

            if should_persist:
                save_summary(
                    transformed,
                    interval=interval,
                    allow_1m_history=allow_1m_history_persist,
                )
            else:
                logger.info(
                    "[MTF HISTORY BOOTSTRAP] persist skipped interval=%s rows=%s "
                    "persist=%s persist_intervals=%s reason=%s",
                    interval,
                    len(transformed),
                    persist,
                    persist_intervals_tuple,
                    "1min_history_persist_disabled"
                    if int(interval) == 1
                    else "interval_not_in_persist_intervals",
                )

            if update_cache:
                update_recovery_cache(transformed, interval=interval)
                set_global_cache(transformed, interval=interval)

            transformed = drop_future_datetime_rows(
                transformed,
                interval=int(interval),
                label=f"run_result_{interval}",
            )

            if transformed.empty:
                logger.warning("[MTF HISTORY BOOTSTRAP] result empty after final future guard interval=%s", interval)
                continue

            result[interval] = transformed

        except Exception:
            logger.exception("[MTF HISTORY BOOTSTRAP] interval failed interval=%s", interval)

    logger.info(
        "[MTF HISTORY BOOTSTRAP] done intervals=%s dt_max=%s",
        {int(k): int(len(v)) for k, v in result.items()},
        {
            int(k): str(v["datetime"].max())
            if isinstance(v, pd.DataFrame) and not v.empty and "datetime" in v.columns
            else None
            for k, v in result.items()
        },
    )

    return result


__all__ = [
    "run_mtf_history_bootstrap",
]