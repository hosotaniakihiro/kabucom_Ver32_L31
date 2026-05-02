# ============================================================
# File   : trading/ranking/summary/aggregation.py
# Version: Ver1.7-PRODUCTION-RANKING-SUMMARY-AGGREGATION
#          -MERGED-HISTORY-TECHNICAL-RECALC
#          -THIN-HISTORY-PROBE-ENHANCED
#          -FIX-1MIN-BUCKETIZE-LATEST
# ------------------------------------------------------------
# ranking summary 用
# - 1min merge
# - history trim
# - bar score
# - bucketize
# - latest extractor
# - merged higher timeframe technical recalc
# - thin history diagnostics
# ------------------------------------------------------------
# FIX:
# ✔ 3m/5m は merge 後に technicals を再計算
# ✔ hist_len / slope / rsi / macd を merged history 基準へ修正
# ✔ latest probe ログ維持
# ✔ 1分履歴不足を切り分ける薄い履歴ログ追加
# ✔ return 値は {1,3,5} の数値キーを維持
# ✔ NEW: 1min も bucketize 後の bar を latest として採用
# ✔ NEW: 1min の重複表示と all-zero 表示を抑制
# ============================================================

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

from trading.ranking.summary.cache_store import (
    _ensure_global_slots,
    get_ranking_summary,
    set_ranking_summary,
    get_latest_ranking_summary,
    set_latest_ranking_summary,
    set_ranking_summary_initialized,
    get_ranking_summary_status_meta,
    set_ranking_summary_status_meta,
)
from trading.ranking.summary.filters import (
    apply_ranking_summary_filters,
)
from trading.ranking.summary.announce import (
    announce_ranking_summary,
)
from trading.ranking.summary.snapshot_normalizer import (
    _drop_duplicate_columns,
    _normalize_snapshot_df,
    _sort_if_possible,
    _coerce_datetime_series,
)
from trading.ranking.summary.symbol_metadata import (
    _ensure_symbolname,
    _last_non_empty,
)
from trading.ranking.summary.technicals import (
    _apply_technical_indicators,
)

logger = logging.getLogger(__name__)

SUPPORTED_INTERVALS = (1, 3, 5)

MAX_ROWS_PER_SYMBOL_1M = 600
MAX_ROWS_PER_SYMBOL_HIGHER = 300

AGG_KEEP_ORDER = [
    "symbol",
    "symbolname",
    "market",
    "dominant_rank_type",
    "rank_count",
    "best_rank_position",
    "last_rank_position",
    "avg_rank_position",
    "first_price",
    "last_price",
    "max_price",
    "min_price",
    "last_volume",
    "sum_volume",
    "max_volume_speed",
    "last_change_rate",
    "source",
    "start_time",
    "end_time",
    "datetime",
    "interval",
    "interval_name",
    "date",
    "time",
    "time_range",
    "price_change_in_bar",
    "ranking_score",
    "score_buy",
    "score_sell",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ma5",
    "ma25",
    "ma75",
    "rsi",
    "macd",
    "signal",
    "hist",
    "atr",
    "vwap",
    "slope_atr_scaled",
    "slope",
    "hist_len",
    "technical_ready",
]


def _trim_history(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        lim = MAX_ROWS_PER_SYMBOL_1M if int(interval) == 1 else MAX_ROWS_PER_SYMBOL_HIGHER

        time_col = None
        if "snapshot_time" in df.columns:
            time_col = "snapshot_time"
        elif "end_time" in df.columns:
            time_col = "end_time"
        elif "datetime" in df.columns:
            time_col = "datetime"

        if not time_col or "symbol" not in df.columns:
            return df.copy()

        x = df.copy()
        x[time_col] = _coerce_datetime_series(x[time_col])
        x = x.dropna(subset=[time_col]).copy()
        x = _sort_if_possible(x, ["symbol", time_col])
        x = x.groupby("symbol", group_keys=False).tail(lim).reset_index(drop=True)
        return x

    except Exception:
        logger.exception("[RANKING SUMMARY] trim failed interval=%s", interval)
        return df.copy()


def _log_thin_history(df: pd.DataFrame, interval: int, time_col: str, stage: str) -> None:
    try:
        if df is None or df.empty:
            logger.warning(
                "[RANKING SUMMARY][%s THIN] interval=%s empty",
                stage,
                interval,
            )
            return

        if "symbol" not in df.columns or time_col not in df.columns:
            logger.warning(
                "[RANKING SUMMARY][%s THIN] interval=%s missing symbol/time_col time_col=%s cols=%s",
                stage,
                interval,
                time_col,
                list(df.columns),
            )
            return

        x = df.copy()
        x[time_col] = _coerce_datetime_series(x[time_col])
        x = x.dropna(subset=["symbol", time_col]).copy()
        if x.empty:
            logger.warning(
                "[RANKING SUMMARY][%s THIN] interval=%s no valid rows after datetime coercion",
                stage,
                interval,
            )
            return

        hist_counts = x.groupby("symbol")[time_col].count()
        thin_le1 = int((hist_counts <= 1).sum())
        thin_le2 = int((hist_counts <= 2).sum())
        thin_le3 = int((hist_counts <= 3).sum())

        logger.warning(
            "[RANKING SUMMARY][%s THIN] interval=%s symbols=%d thin_le1=%d thin_le2=%d thin_le3=%d min=%d median=%d max=%d",
            stage,
            interval,
            int(hist_counts.shape[0]),
            thin_le1,
            thin_le2,
            thin_le3,
            int(hist_counts.min()) if len(hist_counts) else 0,
            int(hist_counts.median()) if len(hist_counts) else 0,
            int(hist_counts.max()) if len(hist_counts) else 0,
        )

    except Exception:
        logger.exception("[RANKING SUMMARY] thin history log failed stage=%s interval=%s", stage, interval)


def _merge_1min_history(df_new: pd.DataFrame) -> pd.DataFrame:
    if df_new is None or df_new.empty:
        return get_ranking_summary(1)

    try:
        df_hist = get_ranking_summary(1)

        logger.info(
            "[RANKING SUMMARY][MERGE 1M BEFORE] new_rows=%d hist_rows_before=%d",
            len(df_new) if isinstance(df_new, pd.DataFrame) else 0,
            len(df_hist) if isinstance(df_hist, pd.DataFrame) else 0,
        )

        if df_hist is None or df_hist.empty:
            merged = df_new.copy()
        else:
            merged = pd.concat([df_hist, df_new], ignore_index=True, sort=False)

        merged = _normalize_snapshot_df(merged)

        before_dedup = len(merged)
        dedup_cols = [c for c in ["symbol", "snapshot_time", "rank_type", "market"] if c in merged.columns]
        if dedup_cols:
            merged = merged.drop_duplicates(subset=dedup_cols, keep="last")

        logger.info(
            "[RANKING SUMMARY][MERGE 1M DEDUP] before=%d after=%d dedup_cols=%s",
            before_dedup,
            len(merged),
            dedup_cols,
        )

        merged = _trim_history(merged, 1)
        merged = _ensure_symbolname(merged)

        try:
            hist_counts = merged.groupby("symbol")["snapshot_time"].count()
            logger.info(
                "[RANKING SUMMARY][MERGE 1M AFTER] rows=%d symbols=%d min=%d median=%d max=%d unique_times=%d",
                len(merged),
                int(hist_counts.shape[0]) if len(hist_counts) else 0,
                int(hist_counts.min()) if len(hist_counts) else 0,
                int(hist_counts.median()) if len(hist_counts) else 0,
                int(hist_counts.max()) if len(hist_counts) else 0,
                int(pd.to_datetime(merged["snapshot_time"], errors="coerce").nunique()) if "snapshot_time" in merged.columns else 0,
            )
        except Exception:
            logger.exception("[RANKING SUMMARY] history profile log failed")

        _log_thin_history(merged, interval=1, time_col="snapshot_time", stage="MERGE 1M")
        return merged

    except Exception:
        logger.exception("[RANKING SUMMARY] merge 1m history failed")
        return df_new.copy()


def _mode_or_first(series: pd.Series) -> Any:
    try:
        if series is None or series.empty:
            return None
        m = series.mode(dropna=True)
        if m is not None and len(m) > 0:
            return m.iloc[0]
        return series.iloc[0]
    except Exception:
        return None


def _build_bar_score(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()

    for c in (
        "rank_count",
        "best_rank_position",
        "avg_rank_position",
        "max_volume_speed",
        "last_change_rate",
    ):
        if c not in x.columns:
            x[c] = 0.0

    try:
        rank_count = pd.to_numeric(x["rank_count"], errors="coerce").fillna(0.0)
        best_pos = pd.to_numeric(x["best_rank_position"], errors="coerce").fillna(9999.0)
        avg_pos = pd.to_numeric(x["avg_rank_position"], errors="coerce").fillna(9999.0)
        max_vs = pd.to_numeric(x["max_volume_speed"], errors="coerce").fillna(0.0)
        last_cr = pd.to_numeric(x["last_change_rate"], errors="coerce").fillna(0.0)

        best_component = (101.0 - best_pos.clip(lower=1, upper=100)) * 2.5
        avg_component = (101.0 - avg_pos.clip(lower=1, upper=100)) * 1.2
        count_component = rank_count.clip(lower=0, upper=30) * 18.0
        vs_component = max_vs.clip(lower=0, upper=10_000_000).map(
            lambda v: math.log1p(float(v)) * 8.0 if pd.notna(v) else 0.0
        )
        chg_component = last_cr.clip(lower=-30, upper=30) * 4.0

        score = count_component + best_component + avg_component + vs_component + chg_component

        x["ranking_score"] = (
            pd.to_numeric(score, errors="coerce")
            .replace([float("inf"), float("-inf")], pd.NA)
            .fillna(0.0)
            .clip(-500.0, 500.0)
            .astype("float64")
        )
        x["score_buy"] = x["ranking_score"]
        x["score_sell"] = -x["ranking_score"]
        return x

    except Exception:
        logger.exception("[RANKING SUMMARY] build bar score failed")
        x["ranking_score"] = 0.0
        x["score_buy"] = 0.0
        x["score_sell"] = 0.0
        return x


def _reorder_keep_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    x = df.copy()
    front = [c for c in AGG_KEEP_ORDER if c in x.columns]
    rest = [c for c in x.columns if c not in front]
    return x[front + rest].copy()


def _apply_technicals_on_merged_history(
    df: pd.DataFrame,
    interval: int,
    time_col: str,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        x = df.copy()
        x = _ensure_symbolname(x)

        if time_col in x.columns:
            x[time_col] = _coerce_datetime_series(x[time_col])

        if "datetime" not in x.columns and time_col in x.columns:
            x["datetime"] = x[time_col]
        elif "datetime" in x.columns and time_col in x.columns:
            x["datetime"] = _coerce_datetime_series(x["datetime"]).fillna(_coerce_datetime_series(x[time_col]))

        x["interval"] = int(interval)
        x["interval_name"] = f"{int(interval)}min"

        x = _sort_if_possible(x, ["symbol", time_col if time_col in x.columns else "datetime"])
        x = _apply_technical_indicators(x)
        x = _ensure_symbolname(x)
        x = _reorder_keep_columns(x)

        try:
            hist_counts = (
                x.groupby("symbol")[time_col].count()
                if ("symbol" in x.columns and time_col in x.columns and not x.empty)
                else pd.Series(dtype="int64")
            )
            logger.info(
                "[RANKING SUMMARY] technical recalc interval=%s rows=%d symbols=%d hist_min=%d hist_median=%d hist_max=%d slope_nonnull=%d rsi_nonnull=%d macd_nonnull=%d",
                interval,
                len(x),
                int(hist_counts.shape[0]) if len(hist_counts) else 0,
                int(hist_counts.min()) if len(hist_counts) else 0,
                int(hist_counts.median()) if len(hist_counts) else 0,
                int(hist_counts.max()) if len(hist_counts) else 0,
                int(pd.to_numeric(x["slope"], errors="coerce").notna().sum()) if "slope" in x.columns else 0,
                int(pd.to_numeric(x["rsi"], errors="coerce").notna().sum()) if "rsi" in x.columns else 0,
                int(pd.to_numeric(x["macd"], errors="coerce").notna().sum()) if "macd" in x.columns else 0,
            )
        except Exception:
            logger.exception("[RANKING SUMMARY] technical recalc stats failed interval=%s", interval)

        return x.reset_index(drop=True)

    except Exception:
        logger.exception("[RANKING SUMMARY] technical recalc failed interval=%s", interval)
        return df.copy()


def _merge_timeframe_history(
    df_new: pd.DataFrame,
    interval: int,
    time_col: str = "end_time",
) -> pd.DataFrame:
    if df_new is None or df_new.empty:
        return get_ranking_summary(interval)

    try:
        df_hist = get_ranking_summary(interval)

        if df_hist is None or df_hist.empty:
            merged = df_new.copy()
        else:
            merged = pd.concat([df_hist, df_new], ignore_index=True, sort=False)

        merged = _ensure_symbolname(merged)

        if time_col in merged.columns:
            merged[time_col] = _coerce_datetime_series(merged[time_col])

        dedup_cols = [c for c in ["symbol", time_col] if c in merged.columns]
        if dedup_cols:
            before = len(merged)
            merged = merged.drop_duplicates(subset=dedup_cols, keep="last")
            logger.info(
                "[RANKING SUMMARY] merge timeframe dedup interval=%s before=%d after=%d cols=%s",
                interval,
                before,
                len(merged),
                dedup_cols,
            )

        merged = _trim_history(merged, interval)
        merged = _sort_if_possible(merged, ["symbol", time_col])

        _log_thin_history(merged, interval=interval, time_col=time_col, stage="MERGE TF PRE-TECH")
        merged = _apply_technicals_on_merged_history(merged, interval=interval, time_col=time_col)
        _log_thin_history(merged, interval=interval, time_col=time_col, stage="MERGE TF POST-TECH")

        try:
            hist_counts = (
                merged.groupby("symbol")[time_col].count()
                if ("symbol" in merged.columns and time_col in merged.columns and not merged.empty)
                else pd.Series(dtype="int64")
            )
            logger.info(
                "[RANKING SUMMARY] merge timeframe history interval=%s rows=%d symbols=%d hist_min=%d hist_median=%d hist_max=%d",
                interval,
                len(merged),
                int(hist_counts.shape[0]) if len(hist_counts) else 0,
                int(hist_counts.min()) if len(hist_counts) else 0,
                int(hist_counts.median()) if len(hist_counts) else 0,
                int(hist_counts.max()) if len(hist_counts) else 0,
            )
        except Exception:
            logger.exception("[RANKING SUMMARY] merge timeframe stats failed interval=%s", interval)

        return merged.reset_index(drop=True)

    except Exception:
        logger.exception("[RANKING SUMMARY] merge timeframe history failed interval=%s", interval)
        return df_new.copy()


def _bucketize(df_1m: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df_1m is None or df_1m.empty:
        return pd.DataFrame()

    if int(interval) not in SUPPORTED_INTERVALS:
        raise ValueError(f"unsupported interval={interval}")

    df = df_1m.copy()
    df = _drop_duplicate_columns(df)

    if "snapshot_time" not in df.columns:
        return pd.DataFrame()

    df["snapshot_time"] = _coerce_datetime_series(df["snapshot_time"])
    df = df.dropna(subset=["snapshot_time"]).copy()
    if df.empty:
        return pd.DataFrame()

    df = _ensure_symbolname(df)

    for c in ["price", "volume", "volume_speed", "change_rate", "rank_position"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").replace([float("inf"), float("-inf")], pd.NA)

    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    if "volume_speed" in df.columns:
        df["volume_speed"] = pd.to_numeric(df["volume_speed"], errors="coerce").fillna(0.0)
    if "change_rate" in df.columns:
        df["change_rate"] = pd.to_numeric(df["change_rate"], errors="coerce").fillna(0.0)

    logger.info(
        "[RANKING SUMMARY] bucketize input interval=%s rows=%d cols=%s",
        interval,
        len(df),
        list(df.columns),
    )

    if int(interval) == 1:
        df["t_floor"] = df["snapshot_time"].dt.floor("1min")
    else:
        df["t_floor"] = df["snapshot_time"].dt.floor(f"{int(interval)}min")

    grp = ["symbol", "t_floor"]

    try:
        out = (
            df.groupby(grp, as_index=False)
            .agg(
                symbolname=("symbolname", _last_non_empty),
                market=("market", _mode_or_first),
                dominant_rank_type=("rank_type", _mode_or_first),
                rank_count=("rank_type", "count"),
                best_rank_position=("rank_position", "min"),
                last_rank_position=("rank_position", "last"),
                avg_rank_position=("rank_position", "mean"),
                first_price=("price", "first"),
                last_price=("price", "last"),
                max_price=("price", "max"),
                min_price=("price", "min"),
                last_volume=("volume", "last"),
                sum_volume=("volume", "sum"),
                max_volume_speed=("volume_speed", "max"),
                last_change_rate=("change_rate", "last"),
                source=("source", _mode_or_first),
            )
        )
    except Exception:
        logger.exception("[RANKING SUMMARY] aggregate failed interval=%s", interval)
        return pd.DataFrame()

    if out.empty:
        return out

    out["start_time"] = out["t_floor"]
    out["end_time"] = out["t_floor"] + pd.Timedelta(minutes=int(interval))
    out["datetime"] = out["end_time"]
    out["interval"] = int(interval)
    out["interval_name"] = f"{int(interval)}min"
    out["date"] = out["end_time"].dt.normalize().dt.date
    out["time"] = out["end_time"].dt.time
    out["time_range"] = (
        out["start_time"].dt.strftime("%H:%M") + " - " + out["end_time"].dt.strftime("%H:%M")
    )
    out["price_change_in_bar"] = (
        pd.to_numeric(out["last_price"], errors="coerce").fillna(0.0)
        - pd.to_numeric(out["first_price"], errors="coerce").fillna(0.0)
    )

    # OHLCV aliases for technical/scoring/display
    out["open"] = pd.to_numeric(out["first_price"], errors="coerce")
    out["high"] = pd.to_numeric(out["max_price"], errors="coerce")
    out["low"] = pd.to_numeric(out["min_price"], errors="coerce")
    out["close"] = pd.to_numeric(out["last_price"], errors="coerce")
    out["volume"] = pd.to_numeric(out["sum_volume"], errors="coerce").fillna(0.0)

    out = _build_bar_score(out)
    out = _ensure_symbolname(out)
    out = _reorder_keep_columns(out)

    try:
        hist_probe = out.groupby("symbol")["end_time"].count()
        logger.info(
            "[RANKING SUMMARY] bucketize done interval=%s rows=%d symbols=%d hist_min=%d hist_median=%d hist_max=%d",
            interval,
            len(out),
            int(hist_probe.shape[0]) if len(hist_probe) else 0,
            int(hist_probe.min()) if len(hist_probe) else 0,
            int(hist_probe.median()) if len(hist_probe) else 0,
            int(hist_probe.max()) if len(hist_probe) else 0,
        )
    except Exception:
        logger.exception("[RANKING SUMMARY] bucketize stats log failed interval=%s", interval)

    out = _sort_if_possible(out, ["symbol", "end_time"])
    return out.reset_index(drop=True)


def _extract_latest_timeframe(df: pd.DataFrame, time_col: str = "end_time") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    if time_col not in df.columns:
        return pd.DataFrame()

    try:
        x = df.copy()
        x[time_col] = _coerce_datetime_series(x[time_col])
        x = x.dropna(subset=[time_col]).copy()
        if x.empty:
            return pd.DataFrame()

        latest_dt = x[time_col].max()
        x = x[x[time_col] == latest_dt].copy()

        sort_cols = []
        ascending = []

        if "symbol" in x.columns:
            sort_cols.append("symbol")
            ascending.append(True)

        if "ranking_score" in x.columns:
            sort_cols.append("ranking_score")
            ascending.append(False)

        sort_cols.append(time_col)
        ascending.append(True)

        if sort_cols:
            x = _sort_if_possible(x, sort_cols, ascending=ascending)

        if "symbol" in x.columns:
            x = (
                x.drop_duplicates(subset=["symbol"], keep="last")
                 .reset_index(drop=True)
            )

        x = _ensure_symbolname(x)
        return x

    except Exception:
        logger.exception("[RANKING SUMMARY] extract latest failed")
        return pd.DataFrame()


def _has_meaningful_rows(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        return False

    try:
        close_ok = (
            pd.to_numeric(df["close"], errors="coerce").fillna(0).gt(0)
            if "close" in df.columns else pd.Series(False, index=df.index)
        )
        score_ok = (
            pd.to_numeric(df["score_buy"], errors="coerce").fillna(0).ne(0)
            if "score_buy" in df.columns else pd.Series(False, index=df.index)
        )
        type_ok = (
            df["dominant_rank_type"].astype(str).str.strip().ne("")
            if "dominant_rank_type" in df.columns else pd.Series(False, index=df.index)
        )
        valid = df[close_ok | score_ok | type_ok]
        return not valid.empty
    except Exception:
        logger.exception("[RANKING SUMMARY] meaningful row check failed")
        return not df.empty


def _log_latest_probe(df_latest: pd.DataFrame, interval: int) -> None:
    try:
        if df_latest is None or df_latest.empty:
            logger.info("[RANKING SUMMARY] latest probe interval=%s empty", interval)
            return

        logger.info(
            "[RANKING SUMMARY] latest probe interval=%s rows=%d hist_nonnull=%d hist_gt1=%d slope_nonnull=%d rsi_nonnull=%d macd_nonnull=%d best_nonnull=%d",
            interval,
            len(df_latest),
            int(pd.to_numeric(df_latest["hist_len"], errors="coerce").notna().sum()) if "hist_len" in df_latest.columns else 0,
            int(pd.to_numeric(df_latest["hist_len"], errors="coerce").fillna(0).gt(1).sum()) if "hist_len" in df_latest.columns else 0,
            int(pd.to_numeric(df_latest["slope"], errors="coerce").notna().sum()) if "slope" in df_latest.columns else 0,
            int(pd.to_numeric(df_latest["rsi"], errors="coerce").notna().sum()) if "rsi" in df_latest.columns else 0,
            int(pd.to_numeric(df_latest["macd"], errors="coerce").notna().sum()) if "macd" in df_latest.columns else 0,
            int(pd.to_numeric(df_latest["best_rank_position"], errors="coerce").notna().sum()) if "best_rank_position" in df_latest.columns else 0,
        )
    except Exception:
        logger.exception("[RANKING SUMMARY] latest probe failed interval=%s", interval)


def update_ranking_summaries(
    snapshot_rows: Any,
    *,
    use_runtime_filter: bool = False,
    refresh_runtime_symbols: bool = False,
    announce_1m: bool = False,
    announce_3m: bool = False,
    announce_5m: bool = False,
    use_discord: bool = False,
) -> dict[int, pd.DataFrame]:
    _ensure_global_slots()

    try:
        from trading.ranking.summary.snapshot_normalizer import _to_dataframe

        df_new = _to_dataframe(snapshot_rows)
        df_new = _normalize_snapshot_df(df_new)

        if df_new.empty:
            logger.warning("[RANKING SUMMARY] update skipped: normalized snapshot empty")
            return {1: get_latest_ranking_summary(1), 3: get_latest_ranking_summary(3), 5: get_latest_ranking_summary(5)}

        df_new = apply_ranking_summary_filters(
            df_new,
            use_runtime_filter=use_runtime_filter,
            refresh_runtime_symbols=refresh_runtime_symbols,
        )

        if df_new.empty:
            logger.warning("[RANKING SUMMARY] update skipped: filtered snapshot empty")
            return {1: get_latest_ranking_summary(1), 3: get_latest_ranking_summary(3), 5: get_latest_ranking_summary(5)}

        # ----------------------------------------------------
        # 1min
        # ----------------------------------------------------
        merged_1m_raw = _merge_1min_history(df_new)
        agg_1m_new = _bucketize(merged_1m_raw, 1)
        agg_1m = _merge_timeframe_history(agg_1m_new, 1, time_col="end_time")

        if isinstance(agg_1m, pd.DataFrame) and not agg_1m.empty:
            set_ranking_summary(1, agg_1m)

        latest_1m = _extract_latest_timeframe(agg_1m, time_col="end_time")
        _log_latest_probe(latest_1m, 1)
        if _has_meaningful_rows(latest_1m):
            logger.info(
                "[RANKING SUMMARY][LATEST 1M BEFORE CACHE]\n%s",
                latest_1m[
                    [c for c in [
                        "symbol", "symbolname", "dominant_rank_type",
                        "ranking_score", "score_buy", "score_sell",
                        "best_rank_position", "hist_len", "slope", "rsi", "macd",
                        "end_time", "datetime"
                    ] if c in latest_1m.columns]
                ].head(30).to_string(index=False)
            )
            logger.info(
                "[RANKING SUMMARY][LATEST 1M BEFORE CACHE]\n%s",
                latest_1m[
                    [c for c in [
                        "symbol", "symbolname", "dominant_rank_type",
                        "ranking_score", "score_buy", "score_sell",
                        "best_rank_position", "hist_len", "slope", "rsi", "macd",
                        "end_time", "datetime"
                    ] if c in latest_1m.columns]
                ].head(30).to_string(index=False)
            )
            set_latest_ranking_summary(1, latest_1m)
        else:
            logger.warning("[RANKING SUMMARY] latest_1m empty/meaningless -> keep previous cache")
            latest_1m = get_latest_ranking_summary(1)

        # ----------------------------------------------------
        # 3min
        # ----------------------------------------------------
        agg_3m_new = _bucketize(merged_1m_raw, 3)
        agg_3m = _merge_timeframe_history(agg_3m_new, 3, time_col="end_time")
        if isinstance(agg_3m, pd.DataFrame) and not agg_3m.empty:
            set_ranking_summary(3, agg_3m)

        latest_3m = _extract_latest_timeframe(agg_3m, time_col="end_time")
        _log_latest_probe(latest_3m, 3)
        if _has_meaningful_rows(latest_3m):
            set_latest_ranking_summary(3, latest_3m)
        else:
            logger.warning("[RANKING SUMMARY] latest_3m empty/meaningless -> keep previous cache")
            latest_3m = get_latest_ranking_summary(3)

        # ----------------------------------------------------
        # 5min
        # ----------------------------------------------------
        agg_5m_new = _bucketize(merged_1m_raw, 5)
        agg_5m = _merge_timeframe_history(agg_5m_new, 5, time_col="end_time")
        if isinstance(agg_5m, pd.DataFrame) and not agg_5m.empty:
            set_ranking_summary(5, agg_5m)

        latest_5m = _extract_latest_timeframe(agg_5m, time_col="end_time")
        _log_latest_probe(latest_5m, 5)
        if _has_meaningful_rows(latest_5m):
            set_latest_ranking_summary(5, latest_5m)
        else:
            logger.warning("[RANKING SUMMARY] latest_5m empty/meaningless -> keep previous cache")
            latest_5m = get_latest_ranking_summary(5)

        set_ranking_summary_initialized(True)

        status_meta = get_ranking_summary_status_meta()
        status_meta.update(
            {
                "last_update_ok": True,
                "last_rows_1m": len(agg_1m) if isinstance(agg_1m, pd.DataFrame) else 0,
                "last_rows_3m": len(agg_3m) if isinstance(agg_3m, pd.DataFrame) else 0,
                "last_rows_5m": len(agg_5m) if isinstance(agg_5m, pd.DataFrame) else 0,
                "last_latest_rows_1m": len(latest_1m) if isinstance(latest_1m, pd.DataFrame) else 0,
                "last_latest_rows_3m": len(latest_3m) if isinstance(latest_3m, pd.DataFrame) else 0,
                "last_latest_rows_5m": len(latest_5m) if isinstance(latest_5m, pd.DataFrame) else 0,
            }
        )
        set_ranking_summary_status_meta(status_meta)

        if announce_1m:
            announce_ranking_summary(interval=1, use_discord=use_discord)
        if announce_3m:
            announce_ranking_summary(interval=3, use_discord=use_discord)
        if announce_5m:
            announce_ranking_summary(interval=5, use_discord=use_discord)

        return {1: latest_1m, 3: latest_3m, 5: latest_5m}

    except Exception:
        logger.exception("[RANKING SUMMARY] update failed")
        status_meta = get_ranking_summary_status_meta()
        status_meta["last_update_ok"] = False
        set_ranking_summary_status_meta(status_meta)
        return {1: get_latest_ranking_summary(1), 3: get_latest_ranking_summary(3), 5: get_latest_ranking_summary(5)}


def rebuild_ranking_summaries_from_dataframe(
    df_1m: pd.DataFrame,
    *,
    announce_1m: bool = False,
    announce_3m: bool = False,
    announce_5m: bool = False,
    use_discord: bool = False,
) -> dict[int, pd.DataFrame]:
    _ensure_global_slots()

    try:
        df_1m = _normalize_snapshot_df(df_1m)
        if df_1m.empty:
            logger.warning("[RANKING SUMMARY] rebuild skipped: normalized 1m empty")
            return {1: pd.DataFrame(), 3: pd.DataFrame(), 5: pd.DataFrame()}

        df_1m = _trim_history(df_1m, 1)

        # ----------------------------------------------------
        # 1min
        # ----------------------------------------------------
        agg_1m_new = _bucketize(df_1m, 1)
        agg_1m = _merge_timeframe_history(agg_1m_new, 1, time_col="end_time")
        if isinstance(agg_1m, pd.DataFrame) and not agg_1m.empty:
            set_ranking_summary(1, agg_1m)

        latest_1m = _extract_latest_timeframe(agg_1m, time_col="end_time")
        _log_latest_probe(latest_1m, 1)
        if _has_meaningful_rows(latest_1m):
            set_latest_ranking_summary(1, latest_1m)
        else:
            latest_1m = get_latest_ranking_summary(1)

        # ----------------------------------------------------
        # 3min
        # ----------------------------------------------------
        agg_3m_new = _bucketize(df_1m, 3)
        agg_3m = _merge_timeframe_history(agg_3m_new, 3, time_col="end_time")
        if isinstance(agg_3m, pd.DataFrame) and not agg_3m.empty:
            set_ranking_summary(3, agg_3m)

        latest_3m = _extract_latest_timeframe(agg_3m, time_col="end_time")
        _log_latest_probe(latest_3m, 3)
        if _has_meaningful_rows(latest_3m):
            set_latest_ranking_summary(3, latest_3m)
        else:
            latest_3m = get_latest_ranking_summary(3)

        # ----------------------------------------------------
        # 5min
        # ----------------------------------------------------
        agg_5m_new = _bucketize(df_1m, 5)
        agg_5m = _merge_timeframe_history(agg_5m_new, 5, time_col="end_time")
        if isinstance(agg_5m, pd.DataFrame) and not agg_5m.empty:
            set_ranking_summary(5, agg_5m)

        latest_5m = _extract_latest_timeframe(agg_5m, time_col="end_time")
        _log_latest_probe(latest_5m, 5)
        if _has_meaningful_rows(latest_5m):
            set_latest_ranking_summary(5, latest_5m)
        else:
            latest_5m = get_latest_ranking_summary(5)

        set_ranking_summary_initialized(True)

        status_meta = get_ranking_summary_status_meta()
        status_meta.update(
            {
                "last_rebuild_ok": True,
                "last_rows_1m": len(agg_1m) if isinstance(agg_1m, pd.DataFrame) else 0,
                "last_rows_3m": len(agg_3m) if isinstance(agg_3m, pd.DataFrame) else 0,
                "last_rows_5m": len(agg_5m) if isinstance(agg_5m, pd.DataFrame) else 0,
            }
        )
        set_ranking_summary_status_meta(status_meta)

        if announce_1m:
            announce_ranking_summary(interval=1, use_discord=use_discord)
        if announce_3m:
            announce_ranking_summary(interval=3, use_discord=use_discord)
        if announce_5m:
            announce_ranking_summary(interval=5, use_discord=use_discord)

        return {1: latest_1m, 3: latest_3m, 5: latest_5m}

    except Exception:
        logger.exception("[RANKING SUMMARY] rebuild failed")
        status_meta = get_ranking_summary_status_meta()
        status_meta["last_rebuild_ok"] = False
        set_ranking_summary_status_meta(status_meta)
        return {1: pd.DataFrame(), 3: pd.DataFrame(), 5: pd.DataFrame()}


__all__ = [
    "_trim_history",
    "_merge_1min_history",
    "_merge_timeframe_history",
    "_mode_or_first",
    "_build_bar_score",
    "_bucketize",
    "_extract_latest_timeframe",
    "_log_latest_probe",
    "update_ranking_summaries",
    "rebuild_ranking_summaries_from_dataframe",
]


