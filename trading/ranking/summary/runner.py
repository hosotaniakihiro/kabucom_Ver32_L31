# ============================================================
# File   : trading/ranking/summary/runner.py
# Ver    : PRODUCTION-STABLE-REV1.3-RANKING-SUMMARY-RUNNER-MODULAR
# ------------------------------------------------------------
# 【概要】
#   ランキング由来サマリー専用 runner
#
# 【重要方針】
#   - PUSH由来 summary とは完全分離
#   - stock_summary_* は読まない・書かない
#   - ranking_snapshot_1min.current_price / price を close として扱う
#   - Yahoo 1分足 close はランキング価格系列の補完として任意利用
#   - ranking_summary_1min / 3min / 5min に保存する
#   - cache_store に latest を必ず反映し、announce.py から取得できるようにする
#
# 【REV1.3】
#   - runner.py を薄い orchestrator に変更
#   - loader / normalize / yahoo_fill / resample / technicals_adapter /
#     score / cache / display に分割
#   - 既存API / scheduler互換APIを維持
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, Optional, Any

import pandas as pd

from trading.ranking.summary.cache import update_ranking_summary_cache
from trading.ranking.summary.constants import (
    DEFAULT_LOOKBACK_MINUTES,
    DEFAULT_TOP_N,
    SUPPORTED_INTERVALS,
)
from trading.ranking.summary.display import (
    announce_if_requested,
    display_ranking_summary_top10,
)
from trading.ranking.summary.loader import load_ranking_snapshot_1min
from trading.ranking.summary.resample import build_ranking_summary_base_df
from trading.ranking.summary.score import ensure_score_columns
from trading.ranking.summary.technicals_adapter import (
    call_external_technical,
    get_latest_rows,
)

logger = logging.getLogger(__name__)


try:
    from trading.ranking.summary.persistence import save_ranking_summary
except Exception:
    save_ranking_summary = None  # type: ignore
    logger.warning(
        "[RANKING SUMMARY RUNNER] persistence import failed -> persist disabled",
        exc_info=True,
    )


def run_ranking_summary_once(
    *,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    interval: int = 1,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    symbols: Optional[Iterable[str]] = None,
    ranking_db_path: Optional[str] = None,
    yahoo_db_path: Optional[str] = None,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = DEFAULT_TOP_N,
    use_discord: bool = True,
) -> pd.DataFrame:
    """
    指定 interval のランキング由来サマリーを1回作成する。

    処理順:
      1. ranking_snapshot_1min から base_df を作成
      2. 1min / 3min / 5min に整形
      3. build_ranking_summary_technical(base_df) で technical 指標付与
      4. score / score_buy / ranking_score を保証
      5. ranking_summary_* に保存
      6. latest cache を更新
      7. announce.py で PUSH風表示 / Discord通知
    """
    interval = int(interval)

    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(f"unsupported interval: {interval}")

    logger.info(
        "[RANKING SUMMARY RUNNER] start interval=%s lookback=%s use_yahoo_fill=%s persist=%s display=%s use_discord=%s",
        interval,
        lookback_minutes,
        use_yahoo_fill,
        persist,
        display,
        use_discord,
    )

    # --------------------------------------------------------
    # 1. technical 前の base_df を構築
    # --------------------------------------------------------
    base_df = build_ranking_summary_base_df(
        trade_date=trade_date,
        interval=interval,
        lookback_minutes=lookback_minutes,
        symbols=symbols,
        ranking_db_path=ranking_db_path,
        yahoo_db_path=yahoo_db_path,
        use_yahoo_fill=use_yahoo_fill,
    )

    if base_df is None or base_df.empty:
        logger.warning(
            "[RANKING SUMMARY RUNNER] base empty interval=%s",
            interval,
        )
        update_ranking_summary_cache(pd.DataFrame(), pd.DataFrame(), interval=interval)
        return pd.DataFrame()

    logger.info(
        "[RANKING SUMMARY RUNNER] base built interval=%s rows=%s symbols=%s "
        "dt_min=%s dt_max=%s",
        interval,
        len(base_df),
        base_df["symbol"].nunique() if "symbol" in base_df.columns else 0,
        base_df["datetime"].min() if "datetime" in base_df.columns else None,
        base_df["datetime"].max() if "datetime" in base_df.columns else None,
    )

    # --------------------------------------------------------
    # 2. technical 指標付与
    # --------------------------------------------------------
    df = call_external_technical(
        base_df,
        interval=interval,
        trade_date=trade_date,
        lookback_minutes=lookback_minutes,
        symbols=symbols,
        ranking_db_path=ranking_db_path,
        yahoo_db_path=yahoo_db_path,
        use_yahoo_fill=use_yahoo_fill,
    )

    if df is None:
        df = pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            logger.exception(
                "[RANKING SUMMARY RUNNER] technical result convert failed interval=%s",
                interval,
            )
            df = pd.DataFrame()

    if df.empty:
        logger.warning(
            "[RANKING SUMMARY RUNNER] built empty interval=%s -> use base_df fallback",
            interval,
        )
        df = base_df.copy()

    # --------------------------------------------------------
    # 3. 必須列保証
    # --------------------------------------------------------
    df = df.loc[:, ~pd.Index(df.columns).duplicated()].copy()
    df["interval"] = interval

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        try:
            df["datetime"] = df["datetime"].dt.tz_localize(None)
        except Exception:
            pass

    if "close" not in df.columns and "current_price" in df.columns:
        df["close"] = pd.to_numeric(df["current_price"], errors="coerce")

    if "current_price" not in df.columns and "close" in df.columns:
        df["current_price"] = pd.to_numeric(df["close"], errors="coerce")

    if "best_rank_position" not in df.columns:
        if "rank" in df.columns:
            df["best_rank_position"] = df["rank"]
        else:
            df["best_rank_position"] = pd.NA

    if "ranking_type" not in df.columns:
        if "category" in df.columns:
            df["ranking_type"] = df["category"].astype(str)
        else:
            df["ranking_type"] = "ranking"

    df = ensure_score_columns(df)

    if df.empty:
        logger.warning(
            "[RANKING SUMMARY RUNNER] final df empty interval=%s",
            interval,
        )
        update_ranking_summary_cache(pd.DataFrame(), pd.DataFrame(), interval=interval)
        return df

    # --------------------------------------------------------
    # 4. 保存
    # --------------------------------------------------------
    if persist:
        if callable(save_ranking_summary):
            try:
                saved = save_ranking_summary(
                    df,
                    interval=interval,
                    trade_date=trade_date,
                    db_path=ranking_db_path,
                )
                logger.info(
                    "[RANKING SUMMARY RUNNER] persist done interval=%s saved=%s",
                    interval,
                    saved,
                )
            except Exception:
                logger.exception(
                    "[RANKING SUMMARY RUNNER] persist failed interval=%s",
                    interval,
                )
        else:
            logger.warning(
                "[RANKING SUMMARY RUNNER] persist skipped interval=%s reason=save_ranking_summary_unavailable",
                interval,
            )

    # --------------------------------------------------------
    # 5. 最新行抽出
    # --------------------------------------------------------
    latest = get_latest_rows(df)
    latest = ensure_score_columns(latest)

    if latest is None:
        latest = pd.DataFrame()

    if not isinstance(latest, pd.DataFrame):
        try:
            latest = pd.DataFrame(latest)
        except Exception:
            latest = pd.DataFrame()

    logger.info(
        "[RANKING SUMMARY RUNNER] latest built interval=%s rows=%s symbols=%s dt_max=%s score_nonnull=%s",
        interval,
        len(latest),
        latest["symbol"].nunique() if "symbol" in latest.columns and not latest.empty else 0,
        latest["datetime"].max() if "datetime" in latest.columns and not latest.empty else None,
        int(latest["score_buy"].notna().sum()) if "score_buy" in latest.columns and not latest.empty else 0,
    )

    # --------------------------------------------------------
    # 6. cache 更新
    # --------------------------------------------------------
    update_ranking_summary_cache(df, latest, interval=interval)

    # --------------------------------------------------------
    # 7. 表示 / Discord
    # --------------------------------------------------------
    announced = announce_if_requested(
        interval=interval,
        display=display,
        top_n=top_n,
        use_discord=use_discord,
    )

    if display and not announced:
        display_ranking_summary_top10(
            latest,
            interval=interval,
            top_n=top_n,
        )

    logger.info(
        "[RANKING SUMMARY RUNNER] done interval=%s rows=%s latest_rows=%s "
        "rsi_non_null=%s macd_non_null=%s slope_non_null=%s",
        interval,
        len(df),
        len(latest),
        int(latest["rsi"].notna().sum()) if "rsi" in latest.columns and not latest.empty else 0,
        int(latest["macd"].notna().sum()) if "macd" in latest.columns and not latest.empty else 0,
        int(latest["slope"].notna().sum()) if "slope" in latest.columns and not latest.empty else 0,
    )

    return df


def run_ranking_summaries_all(
    *,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    intervals: tuple[int, ...] = (1, 3, 5),
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    symbols: Optional[Iterable[str]] = None,
    ranking_db_path: Optional[str] = None,
    yahoo_db_path: Optional[str] = None,
    use_yahoo_fill: bool = True,
    persist: bool = True,
    display: bool = True,
    top_n: int = DEFAULT_TOP_N,
    use_discord: bool = True,
) -> dict[int, pd.DataFrame]:
    """
    1min / 3min / 5min のランキング由来サマリーをまとめて作成する。
    """
    results: dict[int, pd.DataFrame] = {}

    for interval in intervals:
        try:
            results[int(interval)] = run_ranking_summary_once(
                trade_date=trade_date,
                interval=int(interval),
                lookback_minutes=lookback_minutes,
                symbols=symbols,
                ranking_db_path=ranking_db_path,
                yahoo_db_path=yahoo_db_path,
                use_yahoo_fill=use_yahoo_fill,
                persist=persist,
                display=display,
                top_n=top_n,
                use_discord=use_discord,
            )
        except Exception:
            logger.exception(
                "[RANKING SUMMARY RUNNER] failed interval=%s",
                interval,
            )
            results[int(interval)] = pd.DataFrame()

    logger.info(
        "[RANKING SUMMARY RUNNER] all done result_rows=%s",
        {k: len(v) if isinstance(v, pd.DataFrame) else 0 for k, v in results.items()},
    )

    return results


# ============================================================
# Scheduler-compatible aliases
# ============================================================

def job_ranking_summary_1m(**kwargs: Any) -> pd.DataFrame:
    return run_ranking_summary_once(interval=1, **kwargs)


def job_ranking_summary_3m(**kwargs: Any) -> pd.DataFrame:
    return run_ranking_summary_once(interval=3, **kwargs)


def job_ranking_summary_5m(**kwargs: Any) -> pd.DataFrame:
    return run_ranking_summary_once(interval=5, **kwargs)


def job_ranking_summary(**kwargs: Any) -> dict[int, pd.DataFrame]:
    return run_ranking_summaries_all(**kwargs)


def job_ranking_summary_all(**kwargs: Any) -> dict[int, pd.DataFrame]:
    return run_ranking_summaries_all(**kwargs)


__all__ = [
    "load_ranking_snapshot_1min",
    "build_ranking_summary_base_df",
    "display_ranking_summary_top10",
    "run_ranking_summary_once",
    "run_ranking_summaries_all",
    "job_ranking_summary_1m",
    "job_ranking_summary_3m",
    "job_ranking_summary_5m",
    "job_ranking_summary",
    "job_ranking_summary_all",
]