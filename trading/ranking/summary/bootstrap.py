# ============================================================
# File   : trading/ranking/summary/bootstrap.py
# Version: Ver2.0-PRODUCTION-RANKING-SUMMARY-BOOTSTRAP
#          -MODULAR-STARTUP-ORCHESTRATOR
# ------------------------------------------------------------
# 【概要】
#   ランキング由来サマリーの起動時 bootstrap 入口
#
# 【目的】
#   システム再起動時に、
#     1. ranking_summary DB の既存最新 datetime を確認
#     2. ranking_snapshot_1min DB を必要範囲だけ読み込み
#     3. ranking snapshot から擬似OHLCVを作成
#     4. PUSH由来 summary DB を補助データとして読み込み
#     5. MA5 / MA25 / MA75 / RSI / MACD / ATR / VWAP / slope 等を計算
#     6. score列を補完
#     7. ranking_summary_1min / 3min / 5min にUPSERT
#     8. global_data にランキング由来サマリーを反映
#
# 【重要方針】
#   - PUSH由来 summary DB は読むだけ
#   - ranking snapshot DB は読むだけ
#   - ranking summary DB に保存する
#   - ranking snapshot由来のOHLCはすべて同値
#     open = high = low = close = snapshot price
#   - scheduler 起動前に1回実行する
# ============================================================

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import pandas as pd

from trading.ranking.summary.bootstrap_cache import set_global_cache
from trading.ranking.summary.bootstrap_config import (
    DEFAULT_BASE_DIR,
    DEFAULT_INTERVALS,
    DEFAULT_LOOKBACK_MINUTES,
    RankingSummaryBootstrapResult,
    build_bootstrap_paths,
)
from trading.ranking.summary.bootstrap_db import (
    determine_load_from,
    get_latest_by_interval,
)
from trading.ranking.summary.bootstrap_fill import merge_push_summary_fill
from trading.ranking.summary.bootstrap_loader import (
    load_push_summary,
    load_ranking_snapshot,
)
from trading.ranking.summary.bootstrap_ohlcv import (
    build_pseudo_ohlcv_1min_from_snapshot,
)
from trading.ranking.summary.bootstrap_resample import resample_ranking_summary
from trading.ranking.summary.bootstrap_saver import (
    filter_after_latest,
    normalize_for_save,
    save_ranking_summary,
)
from trading.ranking.summary.bootstrap_score import ensure_score_columns
from trading.ranking.summary.bootstrap_technical import apply_technical_indicators

logger = logging.getLogger(__name__)


def build_interval_summary(
    base_1m: pd.DataFrame,
    *,
    interval: int,
    push_summary_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if base_1m is None or base_1m.empty:
        return pd.DataFrame()

    if int(interval) == 1:
        x = base_1m.copy()
    else:
        x = resample_ranking_summary(base_1m, interval=int(interval))

    if x.empty:
        return pd.DataFrame()

    if push_summary_df is not None and not push_summary_df.empty:
        x = merge_push_summary_fill(x, push_summary_df, interval=int(interval))

    x = apply_technical_indicators(x, interval=int(interval))
    x = ensure_score_columns(x)

    # ランキング由来OHLC同値を最終保証
    if "close" in x.columns:
        close = pd.to_numeric(x["close"], errors="coerce")
        x["open"] = close
        x["high"] = close
        x["low"] = close
        x["close"] = close

    x["interval"] = int(interval)
    x["source"] = "ranking_snapshot"
    x["updated_at"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

    x = normalize_for_save(x, interval=int(interval))

    logger.info(
        "[RANKING SUMMARY BOOTSTRAP] interval built interval=%s rows=%d symbols=%d dt_min=%s dt_max=%s",
        interval,
        len(x),
        x["symbol"].nunique() if "symbol" in x.columns and not x.empty else 0,
        x["datetime"].min() if "datetime" in x.columns and not x.empty else None,
        x["datetime"].max() if "datetime" in x.columns and not x.empty else None,
    )

    return x


def bootstrap_ranking_summary_on_startup(
    *,
    base_dir: str = DEFAULT_BASE_DIR,
    yyyymmdd: str | None = None,
    ranking_db_path: str | None = None,
    summary_db_path: str | None = None,
    ranking_summary_db_path: str | None = None,
    intervals: Iterable[int] = DEFAULT_INTERVALS,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    save: bool = True,
    update_global_cache: bool = True,
    force_rebuild_from: str | pd.Timestamp | None = None,
) -> RankingSummaryBootstrapResult:
    started = time.time()

    intervals_tuple = tuple(int(i) for i in intervals)

    paths = build_bootstrap_paths(
        base_dir=base_dir,
        yyyymmdd=yyyymmdd,
        ranking_db_path_override=ranking_db_path,
        summary_db_path_override=summary_db_path,
        ranking_summary_db_path_override=ranking_summary_db_path,
    )

    logger.info(
        "[RANKING SUMMARY BOOTSTRAP] start ymd=%s intervals=%s ranking_db=%s summary_db=%s ranking_summary_db=%s lookback=%s",
        paths.yyyymmdd,
        intervals_tuple,
        paths.ranking_db_path,
        paths.summary_db_path,
        paths.ranking_summary_db_path,
        lookback_minutes,
    )

    try:
        latest_by_interval = get_latest_by_interval(
            paths.ranking_summary_db_path,
            intervals_tuple,
        )

        if force_rebuild_from is not None:
            load_from = pd.to_datetime(force_rebuild_from, errors="coerce")
            if pd.isna(load_from):
                load_from = determine_load_from(
                    latest_by_interval,
                    lookback_minutes=int(lookback_minutes),
                )
        else:
            load_from = determine_load_from(
                latest_by_interval,
                lookback_minutes=int(lookback_minutes),
            )

        logger.info(
            "[RANKING SUMMARY BOOTSTRAP] latest_by_interval=%s load_from=%s",
            {k: str(v) if v is not None else None for k, v in latest_by_interval.items()},
            load_from,
        )

        snapshot = load_ranking_snapshot(
            paths.ranking_db_path,
            start_dt=load_from,
            end_dt=None,
        )

        if snapshot is None or snapshot.empty:
            msg = "ranking snapshot empty"
            logger.warning("[RANKING SUMMARY BOOTSTRAP] %s", msg)
            return RankingSummaryBootstrapResult(
                ok=False,
                intervals={i: 0 for i in intervals_tuple},
                db_path=paths.ranking_summary_db_path,
                snapshot_rows=0,
                message=msg,
            )

        base_1m = build_pseudo_ohlcv_1min_from_snapshot(snapshot)

        if base_1m.empty:
            msg = "pseudo OHLCV base empty"
            logger.warning("[RANKING SUMMARY BOOTSTRAP] %s", msg)
            return RankingSummaryBootstrapResult(
                ok=False,
                intervals={i: 0 for i in intervals_tuple},
                db_path=paths.ranking_summary_db_path,
                snapshot_rows=len(snapshot),
                message=msg,
            )

        symbols = sorted(base_1m["symbol"].astype(str).dropna().unique().tolist())

        saved_by_interval: dict[int, int] = {}

        for interval in intervals_tuple:
            try:
                push_df = load_push_summary(
                    paths.summary_db_path,
                    interval=interval,
                    symbols=symbols,
                    start_dt=load_from,
                    end_dt=None,
                )

                interval_df = build_interval_summary(
                    base_1m,
                    interval=interval,
                    push_summary_df=push_df,
                )

                if interval_df.empty:
                    saved_by_interval[interval] = 0
                    logger.warning(
                        "[RANKING SUMMARY BOOTSTRAP] interval empty interval=%s",
                        interval,
                    )
                    continue

                latest_dt = latest_by_interval.get(interval)
                save_df = filter_after_latest(interval_df, latest_dt)

                if save:
                    saved = save_ranking_summary(
                        paths.ranking_summary_db_path,
                        save_df,
                        interval=interval,
                    )
                else:
                    saved = len(save_df)

                saved_by_interval[interval] = int(saved)

                if update_global_cache:
                    # 表示用には保存対象だけでなく、今回構築した履歴付き全体を反映
                    set_global_cache(interval, interval_df)

                logger.info(
                    "[RANKING SUMMARY BOOTSTRAP] interval done interval=%s built=%d save_target=%d saved=%d latest_dt=%s",
                    interval,
                    len(interval_df),
                    len(save_df),
                    saved,
                    latest_dt,
                )

            except Exception:
                logger.exception(
                    "[RANKING SUMMARY BOOTSTRAP] interval failed interval=%s",
                    interval,
                )
                saved_by_interval[interval] = 0

        elapsed = time.time() - started

        logger.info(
            "[RANKING SUMMARY BOOTSTRAP] done ok=1 snapshot_rows=%d saved=%s elapsed=%.2fs",
            len(snapshot),
            saved_by_interval,
            elapsed,
        )

        return RankingSummaryBootstrapResult(
            ok=True,
            intervals=saved_by_interval,
            db_path=paths.ranking_summary_db_path,
            snapshot_rows=len(snapshot),
            message=f"done elapsed={elapsed:.2f}s",
        )

    except Exception as e:
        logger.exception("[RANKING SUMMARY BOOTSTRAP] failed")
        return RankingSummaryBootstrapResult(
            ok=False,
            intervals={i: 0 for i in intervals_tuple},
            db_path=paths.ranking_summary_db_path,
            snapshot_rows=0,
            message=str(e),
        )


def run_startup_ranking_summary_bootstrap(**kwargs: Any) -> RankingSummaryBootstrapResult:
    return bootstrap_ranking_summary_on_startup(**kwargs)


def bootstrap(**kwargs: Any) -> RankingSummaryBootstrapResult:
    return bootstrap_ranking_summary_on_startup(**kwargs)


__all__ = [
    "build_interval_summary",
    "bootstrap_ranking_summary_on_startup",
    "run_startup_ranking_summary_bootstrap",
    "bootstrap",
]