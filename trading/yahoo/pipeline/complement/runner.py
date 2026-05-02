# ============================================================
# File   : trading/yahoo/pipeline/complement/runner.py
# Version: PRODUCTION-STABLE-REV4.2-YAHOO-COMPLEMENT-RUNNER
#          -CALC-FULL-WARMUP-SAVE-DIFF-SPLIT
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完パイプラインのメイン処理
#
# 【主な機能】
#   - 後方互換API:
#       run_yahoo_summary_pipeline
#       run_yahoo_mtf_summary_pipeline
#
#   - 差分上書きAPI:
#       run_yahoo_complement_pipeline
#       run_yahoo_complement_once
#
# 【処理順】
#   1. Yahoo 1分足DataFrame正規化
#   2. Yahoo source の latest datetime 取得
#   3. warmup 分だけ過去を残す
#   4. 直近未確定帯を除外
#   5. 1m/3m/5mを生成
#   6. indicator + scoring
#   7. 保存直前だけ latest_yahoo_dt から overlap 分だけ戻して差分抽出
#   8. UPSERTでPUSH由来サマリーをYahoo由来サマリーに上書き
#
# 【REV4.2 修正】
#   - 計算用DataFrameと保存用DataFrameを完全分離
#   - indicator/scoring は必ず warmup込みの全履歴で実行
#   - 差分抽出 filter_diff_rows() は保存直前だけに限定
#   - cache更新用DataFrameは保存差分ではなく、計算済み最新completed-ish候補を使う
#   - 3分/5分で hist本数が少ない場合の診断ログを追加
#   - 5分足が2本だけになって slope/rsi/macd が出ない問題の切り分けを強化
#
# 【重要】
#   - df_yahoo がそもそも直近数本しか含まない場合、runner側だけでは
#     5分足のMACD/RSI計算に必要な履歴は作れない
#   - その場合は呼び出し側で Yahoo 1分足を warmup 分以上渡す必要がある
#   - 本runnerでは、渡されたdf_yahooの中で warmup込み計算 → 差分保存を保証する
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Iterable, Optional

import pandas as pd

from .constants import (
    DEFAULT_INTERVALS,
    DEFAULT_TOUCH_RECENT_MINUTES,
    DEFAULT_WARMUP_MINUTES,
    DEFAULT_OVERLAP_MINUTES_BY_INTERVAL,
    yahoo_source_for_interval,
)
from .db import (
    today_yyyymmdd,
    get_summary_db_path,
    get_latest_yahoo_summary_datetime,
    get_latest_datetimes_report,
)
from .normalize import (
    safe_df,
    normalize_yahoo_1min_df,
    drop_recent_rows,
    numeric_series,
)
from .diff import (
    calc_fetch_start,
    filter_from_fetch_start,
    filter_diff_rows,
)
from .resample import build_interval_frame
from .compute import compute_summary_frame
from .save import (
    finalize_and_save,
    finalize_for_upsert_if_possible,
    save_summary_df,
    update_global_cache_if_possible,
)

logger = logging.getLogger(__name__)


# ============================================================
# helpers
# ============================================================

def _clean_intervals(intervals: Iterable[int]) -> list[int]:
    cleaned: list[int] = []
    for x in intervals:
        try:
            v = int(x)
            if v > 0 and v not in cleaned:
                cleaned.append(v)
        except Exception:
            continue

    return cleaned or [1]


def _safe_ts(v) -> Optional[pd.Timestamp]:
    try:
        if v is None:
            return None
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None
        ts = pd.Timestamp(ts)
        try:
            ts = ts.tz_localize(None)
        except Exception:
            try:
                ts = ts.tz_convert(None)
            except Exception:
                pass
        return ts
    except Exception:
        return None


def _log_frame_profile(label: str, df: pd.DataFrame, *, interval: Optional[int] = None) -> None:
    try:
        out = safe_df(df)
        if out.empty:
            logger.info("[YAHOO RUNNER] %s interval=%s empty", label, interval)
            return

        if "datetime" in out.columns:
            dt_s = pd.to_datetime(out["datetime"], errors="coerce")
            dt_min = dt_s.min()
            dt_max = dt_s.max()
        else:
            dt_min = None
            dt_max = None

        logger.info(
            "[YAHOO RUNNER] %s interval=%s rows=%s symbols=%s dt_min=%s dt_max=%s cols=%s",
            label,
            interval,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            dt_min,
            dt_max,
            len(out.columns),
        )
    except Exception:
        logger.debug("[YAHOO RUNNER] frame profile failed label=%s", label, exc_info=True)


def _log_history_profile(df: pd.DataFrame, *, interval: int, label: str) -> None:
    """
    symbolごとの履歴本数を診断する。
    5分足で hist_max=2 のような状態をすぐ見つけるためのログ。
    """
    try:
        out = safe_df(df)
        if out.empty or "symbol" not in out.columns:
            logger.info("[YAHOO RUNNER] hist profile %s interval=%s empty", label, interval)
            return

        counts = out.groupby("symbol", sort=False).size()
        if counts.empty:
            logger.info("[YAHOO RUNNER] hist profile %s interval=%s no counts", label, interval)
            return

        min_slope = 3
        min_rsi = 5
        min_macd = 8

        logger.info(
            "[YAHOO RUNNER] hist profile %s interval=%s symbols=%s hist_min=%s hist_median=%.1f hist_max=%s ge_slope=%s ge_rsi=%s ge_macd=%s",
            label,
            interval,
            int(counts.shape[0]),
            int(counts.min()),
            float(counts.median()),
            int(counts.max()),
            int((counts >= min_slope).sum()),
            int((counts >= min_rsi).sum()),
            int((counts >= min_macd).sum()),
        )

        if int(counts.max()) < min_macd:
            logger.warning(
                "[YAHOO RUNNER] insufficient history for technicals interval=%s label=%s hist_max=%s need_slope=%s need_rsi=%s need_macd=%s; "
                "df_yahoo may contain only recent bars. Pass warmup history to Yahoo complement.",
                interval,
                label,
                int(counts.max()),
                min_slope,
                min_rsi,
                min_macd,
            )
    except Exception:
        logger.debug("[YAHOO RUNNER] history profile failed label=%s interval=%s", label, interval, exc_info=True)


def _latest_completedish_for_cache(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    """
    cache更新用候補を作る。

    保存差分だけでcache更新すると、3分/5分の履歴が短い場合に
    未成熟な行でmerged cacheを上書きしやすい。
    ここでは計算済み全体から、symbolごとの最新行を採用する。
    """
    out = safe_df(df)
    if out.empty:
        return out

    try:
        if "symbol" not in out.columns or "datetime" not in out.columns:
            return out

        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["symbol", "datetime"]).copy()
        if out.empty:
            return out

        # scoreが存在し、closeがある行を優先
        score_s = numeric_series(out, "score").fillna(0.0)
        close_s = numeric_series(out, "close")
        slope_s = numeric_series(out, "slope")
        rsi_s = numeric_series(out, "rsi")
        macd_s = numeric_series(out, "macd")
        mtf_s = numeric_series(out, "mtf")

        maturity = pd.Series(0, index=out.index, dtype="int64")
        maturity += close_s.notna().astype(int) * 10
        maturity += score_s.ne(0).astype(int) * 10
        maturity += slope_s.fillna(0).ne(0).astype(int) * 3
        maturity += rsi_s.notna().astype(int) * 3
        maturity += macd_s.fillna(0).ne(0).astype(int) * 3
        maturity += mtf_s.fillna(0).ne(0).astype(int) * 3

        out["_cache_maturity"] = maturity

        out = (
            out.sort_values(
                ["symbol", "datetime", "_cache_maturity"],
                ascending=[True, False, False],
                kind="stable",
            )
            .drop_duplicates(subset=["symbol"], keep="first")
            .drop(columns=["_cache_maturity"], errors="ignore")
            .reset_index(drop=True)
        )

        logger.info(
            "[YAHOO RUNNER] cache candidate built interval=%s rows=%s symbols=%s latest=%s",
            interval,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
        )

        return out
    except Exception:
        logger.exception("[YAHOO RUNNER] cache candidate build failed interval=%s", interval)
        return df


def _log_final_sample(df: pd.DataFrame, *, interval: int, label: str) -> None:
    try:
        out = safe_df(df)
        if out.empty:
            return

        cols = [
            "symbol",
            "symbolname",
            "datetime",
            "date",
            "time_range",
            "time",
            "start_time",
            "end_time",
            "source",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "ma5",
            "ma25",
            "ma75",
            "rsi",
            "macd",
            "hist",
            "score",
            "final_score",
            "display_score",
            "score_buy",
            "score_sell",
            "score_total",
            "slope",
            "slope_atr_scaled",
            "mtf",
            "score_mtf",
        ]
        cols = [c for c in cols if c in out.columns]

        logger.info(
            "[YAHOO RUNNER] %s sample interval=%s\n%s",
            label,
            interval,
            out[cols].head(20).to_string(),
        )
    except Exception:
        logger.debug("[YAHOO RUNNER] final sample log failed label=%s interval=%s", label, interval, exc_info=True)


# ============================================================
# core per-interval
# ============================================================

def _run_single_interval_pipeline(
    df_yahoo_1min: pd.DataFrame,
    *,
    interval: int,
    save: bool = True,
    touch_recent_minutes: int = DEFAULT_TOUCH_RECENT_MINUTES,
    latest_yahoo_dt: Optional[pd.Timestamp] = None,
    diff_only: bool = False,
    overlap_minutes_by_interval: Optional[dict[int, int]] = None,
    update_cache: bool = False,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    """
    1 interval分のYahoo補完処理。

    REV4.2の重要点:
      - interval_df は warmup込み全体
      - compute_summary_frame() も warmup込み全体
      - save_df だけを filter_diff_rows() で差分化
      - cache更新は calc_df_full から最新completed-ish候補を作って使う
    """
    try:
        interval = int(interval)
        source = yahoo_source_for_interval(interval)

        # ----------------------------------------------------
        # 1. 正規化
        # ----------------------------------------------------
        df_1m_calc = normalize_yahoo_1min_df(df_yahoo_1min)
        if df_1m_calc.empty:
            logger.debug("[YAHOO RUNNER] input empty interval=%s", interval)
            return pd.DataFrame()

        _log_frame_profile("normalized 1m calc input", df_1m_calc, interval=interval)

        # ----------------------------------------------------
        # 2. 直近未確定帯のみ除外
        #    ここでは差分抽出はしない。
        # ----------------------------------------------------
        df_1m_calc = drop_recent_rows(
            df_1m_calc,
            touch_recent_minutes,
            now=pd.Timestamp(now) if now else None,
        )
        if df_1m_calc.empty:
            logger.info("[YAHOO RUNNER] no rows after recent filter interval=%s", interval)
            return pd.DataFrame()

        _log_frame_profile("after recent filter 1m calc", df_1m_calc, interval=interval)
        _log_history_profile(df_1m_calc, interval=1, label=f"1m-before-resample-for-{interval}m")

        # ----------------------------------------------------
        # 3. warmup込みで interval frame 生成
        # ----------------------------------------------------
        interval_df_full = build_interval_frame(df_1m_calc, interval=interval)
        if interval_df_full.empty:
            logger.debug("[YAHOO RUNNER] interval frame empty interval=%s", interval)
            return pd.DataFrame()

        interval_df_full["source"] = source
        interval_df_full["interval"] = interval

        _log_frame_profile("interval frame full before compute", interval_df_full, interval=interval)
        _log_history_profile(interval_df_full, interval=interval, label="interval-full-before-compute")

        # ----------------------------------------------------
        # 4. warmup込み全体で indicator + scoring
        # ----------------------------------------------------
        calc_df_full = compute_summary_frame(interval_df_full, interval=interval)
        if calc_df_full.empty:
            return pd.DataFrame()

        calc_df_full["source"] = source
        calc_df_full["interval"] = interval
        calc_df_full["last_update"] = pd.Timestamp.now()

        _log_frame_profile("calc full after compute", calc_df_full, interval=interval)
        _log_history_profile(calc_df_full, interval=interval, label="calc-full-after-compute")

        # ----------------------------------------------------
        # 5. 保存用DataFrameだけ差分抽出
        # ----------------------------------------------------
        if diff_only:
            save_df = filter_diff_rows(
                calc_df_full,
                interval=interval,
                latest_yahoo_dt=latest_yahoo_dt,
                overlap_minutes_by_interval=overlap_minutes_by_interval,
            )
            if save_df.empty:
                logger.info(
                    "[YAHOO RUNNER] no diff rows interval=%s latest_yahoo_dt=%s",
                    interval,
                    latest_yahoo_dt,
                )

                # 保存差分がなくても、cache更新だけ必要なケースがあるため、
                # update_cache=Trueなら計算済み全体からcache候補を作る。
                if update_cache:
                    cache_df = _latest_completedish_for_cache(calc_df_full, interval=interval)
                    cache_df = finalize_for_upsert_if_possible(cache_df, interval=interval)
                    update_global_cache_if_possible(cache_df, interval=interval)

                return pd.DataFrame()
        else:
            save_df = calc_df_full.copy()

        save_df["source"] = source
        save_df["interval"] = interval
        save_df["last_update"] = pd.Timestamp.now()

        _log_frame_profile("save df before finalize", save_df, interval=interval)
        _log_history_profile(save_df, interval=interval, label="save-diff-before-finalize")
        _log_final_sample(save_df, interval=interval, label="save df before save")

        # ----------------------------------------------------
        # 6. 保存
        # ----------------------------------------------------
        finalized_save_df, saved_rows = finalize_and_save(
            save_df,
            interval=interval,
            save=save,
            update_cache=False,  # cacheは下でcalc_df_fullから更新する
        )

        # ----------------------------------------------------
        # 7. cache更新は保存差分ではなく計算済み全体から作る
        # ----------------------------------------------------
        if update_cache:
            try:
                cache_df = _latest_completedish_for_cache(calc_df_full, interval=interval)
                cache_df["source"] = source
                cache_df["interval"] = interval
                cache_df["last_update"] = pd.Timestamp.now()
                cache_df = finalize_for_upsert_if_possible(cache_df, interval=interval)
                update_global_cache_if_possible(cache_df, interval=interval)
            except Exception:
                logger.exception("[YAHOO RUNNER] cache update from full calc failed interval=%s", interval)

        # ----------------------------------------------------
        # 8. 最終ログ
        # ----------------------------------------------------
        try:
            logger.info(
                "[YAHOO RUNNER] processed interval=%d calc_rows=%d save_rows=%d saved=%d calc_symbols=%d save_symbols=%d latest_calc=%s latest_save=%s source=%s score_nonzero=%d final_nonzero=%d display_nonzero=%d",
                interval,
                len(calc_df_full),
                len(finalized_save_df),
                saved_rows,
                calc_df_full["symbol"].nunique() if "symbol" in calc_df_full.columns else 0,
                finalized_save_df["symbol"].nunique() if "symbol" in finalized_save_df.columns else 0,
                calc_df_full["datetime"].max() if "datetime" in calc_df_full.columns and not calc_df_full.empty else None,
                finalized_save_df["datetime"].max() if "datetime" in finalized_save_df.columns and not finalized_save_df.empty else None,
                source,
                int((numeric_series(calc_df_full, "score").fillna(0) != 0).sum()) if not calc_df_full.empty else 0,
                int((numeric_series(calc_df_full, "final_score").fillna(0) != 0).sum()) if not calc_df_full.empty else 0,
                int((numeric_series(calc_df_full, "display_score").fillna(0) != 0).sum()) if not calc_df_full.empty else 0,
            )
        except Exception:
            logger.debug("[YAHOO RUNNER] processed log failed", exc_info=True)

        return finalized_save_df

    except Exception:
        logger.exception("[YAHOO RUNNER] fatal error interval=%s", interval)
        return pd.DataFrame()


# ============================================================
# old-compatible APIs
# ============================================================

def run_yahoo_summary_pipeline(
    df_yahoo: pd.DataFrame,
    *,
    interval: int = 1,
    save: bool = True,
    touch_recent_minutes: int = DEFAULT_TOUCH_RECENT_MINUTES,
) -> pd.DataFrame:
    """
    単一 interval 用。
    既存互換API。

    後方互換のため diff_only=False。
    渡された df_yahoo 全体で計算し、保存する。
    """
    return _run_single_interval_pipeline(
        df_yahoo_1min=df_yahoo,
        interval=int(interval),
        save=save,
        touch_recent_minutes=touch_recent_minutes,
        diff_only=False,
        update_cache=False,
    )


def run_yahoo_mtf_summary_pipeline(
    df_yahoo: pd.DataFrame,
    *,
    intervals: Iterable[int] = DEFAULT_INTERVALS,
    save: bool = True,
    touch_recent_minutes: int = DEFAULT_TOUCH_RECENT_MINUTES,
) -> dict[int, pd.DataFrame]:
    """
    1分足 Yahoo データから 1分/3分/5分 summary をまとめて生成する。
    既存互換API。

    後方互換のため diff_only=False。
    """
    results: dict[int, pd.DataFrame] = {}

    try:
        cleaned = _clean_intervals(intervals)

        for interval in cleaned:
            results[interval] = _run_single_interval_pipeline(
                df_yahoo_1min=df_yahoo,
                interval=interval,
                save=save,
                touch_recent_minutes=touch_recent_minutes,
                diff_only=False,
                update_cache=False,
            )

        logger.info(
            "[YAHOO RUNNER] mtf complete intervals=%s nonempty=%s",
            cleaned,
            [k for k, v in results.items() if isinstance(v, pd.DataFrame) and not v.empty],
        )

        return results

    except Exception:
        logger.exception("[YAHOO RUNNER] mtf fatal error")
        return results


# ============================================================
# diff overwrite APIs
# ============================================================

def run_yahoo_complement_pipeline(
    df_yahoo: pd.DataFrame,
    *,
    intervals: Iterable[int] = DEFAULT_INTERVALS,
    save: bool = True,
    touch_recent_minutes: int = DEFAULT_TOUCH_RECENT_MINUTES,
    warmup_minutes: int = DEFAULT_WARMUP_MINUTES,
    overlap_minutes_by_interval: Optional[dict[int, int]] = None,
    base_dir: Optional[str] = None,
    summary_db_path: Optional[str] = None,
    date_yyyymmdd: Optional[str] = None,
    update_cache: bool = True,
    now: Optional[dt.datetime] = None,
) -> dict[int, pd.DataFrame]:
    """
    Yahoo補完差分上書きモード。

    重要:
      - Yahoo source の latest datetime を基準にする
      - PUSH source の latest datetime は差分判定には使わない
      - 同じ symbol+datetime はUPSERTでYahoo行が上書きする
      - indicator/scoringはwarmup込み全体で計算する
      - 差分抽出は保存直前のみ行う

    注意:
      - df_yahoo自体が直近数本しかない場合、5分足MACD/RSIに必要な履歴は作れない
      - その場合、呼び出し側のYahoo取得範囲を warmup_minutes 以上に広げる必要がある
    """
    started = time.time()
    now = now or dt.datetime.now()
    date_yyyymmdd = date_yyyymmdd or today_yyyymmdd(now)
    overlap_minutes_by_interval = overlap_minutes_by_interval or DEFAULT_OVERLAP_MINUTES_BY_INTERVAL.copy()

    results: dict[int, pd.DataFrame] = {}

    try:
        cleaned = _clean_intervals(intervals)

        raw_all = normalize_yahoo_1min_df(df_yahoo)
        if raw_all.empty:
            logger.warning("[YAHOO RUNNER] complement raw yahoo df empty")
            return results

        _log_frame_profile("raw all after normalize", raw_all, interval=1)
        _log_history_profile(raw_all, interval=1, label="raw-all-after-normalize")

        # ----------------------------------------------------
        # 1m latestから warmup 開始時刻を計算
        # ----------------------------------------------------
        latest_1m = get_latest_yahoo_summary_datetime(
            1,
            summary_db_path=summary_db_path,
            base_dir=base_dir,
            date_yyyymmdd=date_yyyymmdd,
        )

        fetch_start = calc_fetch_start(
            latest_1m,
            warmup_minutes=int(warmup_minutes),
            now=now,
        )

        before = len(raw_all)
        raw_calc = filter_from_fetch_start(raw_all, fetch_start=fetch_start)

        logger.info(
            "========== 🟦 YAHOO COMPLEMENT START intervals=%s date=%s raw_rows=%s -> calc_rows=%s latest_1m=%s fetch_start=%s warmup=%s db=%s ==========",
            cleaned,
            date_yyyymmdd,
            before,
            len(raw_calc),
            latest_1m,
            fetch_start,
            warmup_minutes,
            summary_db_path or get_summary_db_path(date_yyyymmdd=date_yyyymmdd, base_dir=base_dir),
        )

        if raw_calc.empty:
            logger.info("[YAHOO RUNNER] no rows after warmup fetch_start filter=%s", fetch_start)
            return results

        _log_history_profile(raw_calc, interval=1, label="raw-calc-after-fetch-start")

        # ----------------------------------------------------
        # intervalごとの latest_yahoo_dt を取得
        # ----------------------------------------------------
        latest_by_interval: dict[int, Optional[pd.Timestamp]] = {}

        for interval in cleaned:
            try:
                get_latest_datetimes_report(
                    interval,
                    summary_db_path=summary_db_path,
                    base_dir=base_dir,
                    date_yyyymmdd=date_yyyymmdd,
                )
            except Exception:
                pass

            latest_by_interval[interval] = get_latest_yahoo_summary_datetime(
                interval,
                summary_db_path=summary_db_path,
                base_dir=base_dir,
                date_yyyymmdd=date_yyyymmdd,
            )

        # ----------------------------------------------------
        # 各interval実行
        # ----------------------------------------------------
        for interval in cleaned:
            results[interval] = _run_single_interval_pipeline(
                df_yahoo_1min=raw_calc,
                interval=interval,
                save=save,
                touch_recent_minutes=touch_recent_minutes,
                latest_yahoo_dt=latest_by_interval.get(interval),
                diff_only=True,
                overlap_minutes_by_interval=overlap_minutes_by_interval,
                update_cache=update_cache,
                now=now,
            )

        logger.info(
            "========== ✅ YAHOO COMPLEMENT DONE elapsed=%.3fs rows=%s nonempty=%s ==========",
            time.time() - started,
            {k: len(v) for k, v in results.items() if isinstance(v, pd.DataFrame)},
            [k for k, v in results.items() if isinstance(v, pd.DataFrame) and not v.empty],
        )

        return results

    except Exception:
        logger.exception("[YAHOO RUNNER] complement fatal error")
        return results


def run_yahoo_complement_once(
    df_yahoo: pd.DataFrame,
    *,
    save: bool = True,
    touch_recent_minutes: int = DEFAULT_TOUCH_RECENT_MINUTES,
    warmup_minutes: int = DEFAULT_WARMUP_MINUTES,
    base_dir: Optional[str] = None,
    summary_db_path: Optional[str] = None,
    date_yyyymmdd: Optional[str] = None,
    update_cache: bool = True,
) -> dict[int, pd.DataFrame]:
    """
    scheduler から呼びやすい入口。
    Yahoo 1分足DataFrameを受け取り、1分/3分/5分を差分上書き保存する。
    """
    return run_yahoo_complement_pipeline(
        df_yahoo=df_yahoo,
        intervals=DEFAULT_INTERVALS,
        save=save,
        touch_recent_minutes=touch_recent_minutes,
        warmup_minutes=warmup_minutes,
        overlap_minutes_by_interval=DEFAULT_OVERLAP_MINUTES_BY_INTERVAL.copy(),
        base_dir=base_dir,
        summary_db_path=summary_db_path,
        date_yyyymmdd=date_yyyymmdd,
        update_cache=update_cache,
    )


__all__ = [
    "run_yahoo_summary_pipeline",
    "run_yahoo_mtf_summary_pipeline",
    "run_yahoo_complement_pipeline",
    "run_yahoo_complement_once",
]