# ============================================================
# File   : trading/summary/display/summary_display_engine.py
# Version: Ver7-PRODUCTION-SUMMARY-DISPLAY-ENGINE-DEBUG-PUSH
# ------------------------------------------------------------
# ✔ summary display engine
# ✔ push summary display
# ✔ ranking summary display
# ✔ 1m / 3m / 5m support
# ✔ safe runtime wrapper
# ✔ scheduler_jobs.summary.display bridge
# ✔ fallback display implementation
# ✔ price=1 decimal
# ✔ metrics=2 decimals
# ✔ production hardened
# ✔ global_data dict/object 両対応
# ✔ merged/summary/ranking key 複数候補対応
# ✔ DEBUG: global_data key hit/miss 可視化
# ✔ DEBUG: push_df / ranking_df rows / cols / route 可視化
# ✔ FIX: fallback score pick に score_buy を追加
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_INTERVALS: tuple[int, ...] = (1, 3, 5)


# ============================================================
# basic helpers
# ============================================================

def _ensure_df(df: Any) -> pd.DataFrame:
    try:
        if isinstance(df, pd.DataFrame):
            return df.copy()
        if df is None:
            return pd.DataFrame()
        return pd.DataFrame(df).copy()
    except Exception:
        logger.debug("[summary_display_engine] dataframe conversion failed", exc_info=True)
        return pd.DataFrame()


def _to_num(v: Any):
    try:
        return pd.to_numeric(pd.Series([v]), errors="coerce").iloc[0]
    except Exception:
        return None


def _fmt_price(v: Any, default: str = "-") -> str:
    try:
        x = _to_num(v)
        if pd.isna(x):
            return default
        return f"{float(x):.1f}"
    except Exception:
        return default


def _fmt_metric(v: Any, default: str = "-") -> str:
    try:
        x = _to_num(v)
        if pd.isna(x):
            return default
        return f"{float(x):.2f}"
    except Exception:
        return default


def _fmt_text(v: Any, default: str = "-") -> str:
    try:
        if v is None:
            return default
        if pd.isna(v):
            return default
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "null", "<na>"}:
            return default
        return s
    except Exception:
        return default


def _fmt_symbol(v: Any, width: int = 6) -> str:
    s = _fmt_text(v, "-").replace(".0", "")
    return f"{s:<{width}}"


def _fmt_name(v: Any, width: int = 30) -> str:
    s = _fmt_text(v, "-")
    if len(s) > width:
        s = s[: max(0, width - 1)] + "…"
    return f"{s:<{width}}"


def _first_existing_col(df: pd.DataFrame, names: Sequence[str]) -> Optional[str]:
    try:
        for c in names:
            if c in df.columns:
                return c
    except Exception:
        pass
    return None


def _pick_dt_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["datetime", "end_time", "time", "start_time", "snapshot_time"])


def _pick_symbol_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["symbol", "code"])


def _pick_name_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["symbolname", "name"])


def _pick_score_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(
        df,
        [
            "score",
            "display_score",
            "final_score",
            "score_total",
            "combined_score",
            "score_buy",
        ],
    )


def _pick_slope_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["slope", "slope_atr_scaled", "score_slope"])


def _pick_mtf_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["mtf", "score_mtf", "mtf_score", "mtf_alignment"])


def _pick_open_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["open", "open_price"])


def _pick_high_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["high", "high_price"])


def _pick_low_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["low", "low_price"])


def _pick_close_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(
        df,
        ["close", "close_price", "price", "current_price", "CurrentPrice", "last_price", "LastPrice"],
    )


def _pick_rsi_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["rsi"])


def _pick_macd_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["macd"])


def _pick_best_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["best_rank", "best", "rank", "best_rank_value"])


def _pick_hist_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["hist", "history_count", "hist_count", "history"])


def _pick_type_col(df: pd.DataFrame) -> Optional[str]:
    return _first_existing_col(df, ["type", "rank_type", "source_type", "ranking_type"])


def _row_count(df: pd.DataFrame) -> int:
    try:
        return 0 if df is None else int(len(df))
    except Exception:
        return 0


def _safe_cols(df: pd.DataFrame) -> list[str]:
    try:
        return [] if df is None else list(df.columns)
    except Exception:
        return []


def _latest_dt_value(df: pd.DataFrame):
    try:
        if df is None or df.empty:
            return "-"
        dt_col = _pick_dt_col(df)
        if not dt_col or dt_col not in df.columns:
            return "-"
        s = pd.to_datetime(df[dt_col], errors="coerce").dropna()
        if s.empty:
            return "-"
        return s.max()
    except Exception:
        return "-"


# ============================================================
# normalize / shape
# ============================================================

def _normalize_df(df: Any) -> pd.DataFrame:
    out = _ensure_df(df)
    if out.empty:
        return out

    try:
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(".0", "", regex=False)

        dt_col = _pick_dt_col(out)
        if dt_col:
            out[dt_col] = pd.to_datetime(out[dt_col], errors="coerce")
            if dt_col != "datetime":
                out["datetime"] = out[dt_col]

        name_col = _pick_name_col(out)
        if name_col:
            out[name_col] = out[name_col].fillna("").astype(str).str.strip()
            if "symbol" in out.columns:
                bad = out[name_col].isin(["", "-", "None", "nan", "<NA>"])
                out.loc[bad, name_col] = out.loc[bad, "symbol"].astype(str)
        elif "symbol" in out.columns:
            out["symbolname"] = out["symbol"].astype(str)

        for c in [
            "score", "display_score", "final_score", "score_total", "combined_score", "score_buy",
            "slope", "slope_atr_scaled", "score_slope",
            "mtf", "score_mtf", "mtf_score", "mtf_alignment",
            "rsi", "macd", "signal",
            "open", "open_price", "high", "high_price", "low", "low_price",
            "close", "close_price", "price", "current_price", "CurrentPrice",
            "last_price", "LastPrice",
            "best_rank", "best", "rank", "best_rank_value",
            "hist", "history_count", "hist_count", "history",
        ]:
            if c in out.columns:
                try:
                    out[c] = pd.to_numeric(out[c], errors="coerce")
                except Exception:
                    pass

        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[summary_display_engine] normalize failed", exc_info=True)
        return out


def _latest_slice(df: pd.DataFrame) -> pd.DataFrame:
    out = _normalize_df(df)
    if out.empty:
        return out

    dt_col = _pick_dt_col(out)
    if not dt_col:
        return out

    try:
        latest = out[dt_col].dropna().max()
        if pd.isna(latest):
            return out
        out = out.loc[out[dt_col] == latest].copy()
        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[summary_display_engine] latest slice failed", exc_info=True)
        return out


def _dedupe_by_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = _normalize_df(df)
    if out.empty:
        return out

    sym_col = _pick_symbol_col(out)
    dt_col = _pick_dt_col(out)
    if not sym_col:
        return out

    try:
        if dt_col:
            out = out.sort_values([sym_col, dt_col], kind="stable")
        else:
            out = out.sort_values([sym_col], kind="stable")
        out = out.drop_duplicates(subset=[sym_col], keep="last")
        return out.reset_index(drop=True)
    except Exception:
        logger.debug("[summary_display_engine] dedupe failed", exc_info=True)
        return out


def _latest_dt_text(df: pd.DataFrame) -> str:
    out = _normalize_df(df)
    if out.empty:
        return "-"
    dt_col = _pick_dt_col(out)
    if not dt_col:
        return "-"
    try:
        latest = out[dt_col].dropna().max()
        if pd.isna(latest):
            return "-"
        return latest.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


# ============================================================
# resolver
# ============================================================

def _resolve_global_data():
    try:
        from global_state import global_data
        return global_data
    except Exception:
        logger.debug("[summary_display_engine] global_data import failed", exc_info=True)
        return None


def _resolve_external_display_functions():
    try:
        from scheduler_jobs.summary.display import display_push_summary, display_ranking_summary
        logger.info("[summary_display_engine] external display import success")
        return display_push_summary, display_ranking_summary
    except Exception:
        logger.debug("[summary_display_engine] external display import failed", exc_info=True)
        return None, None


# ============================================================
# global_data access
# ============================================================

def _get_from_global_data(keys: Sequence[str]) -> Any:
    gd = _resolve_global_data()
    if gd is None:
        logger.info("[summary_display_engine] global_data unavailable")
        return None

    for key in keys:
        try:
            if hasattr(gd, key):
                value = getattr(gd, key)
                if value is not None:
                    logger.info(
                        "[summary_display_engine] global_data hit attr key=%s type=%s",
                        key,
                        type(value).__name__,
                    )
                    return value
        except Exception:
            pass

        try:
            if isinstance(gd, dict) and key in gd:
                value = gd.get(key)
                if value is not None:
                    logger.info(
                        "[summary_display_engine] global_data hit dict key=%s type=%s",
                        key,
                        type(value).__name__,
                    )
                    return value
        except Exception:
            pass

    logger.info("[summary_display_engine] global_data miss keys=%s", list(keys))
    return None


def _push_keys(interval: int) -> list[str]:
    tf = int(interval)
    return [
        f"summary_{tf}min_df",
        f"summary_df_{tf}min",
        f"latest_summary_{tf}min",
        f"push_summary_{tf}min",
        f"push_summary_{tf}min_df",
        f"merged_summary_{tf}min",
        f"merged_summary_{tf}min_df",
        f"summary_{tf}m",
        f"summary_df_{tf}m",
    ]


def _ranking_keys(interval: int) -> list[str]:
    tf = int(interval)
    return [
        f"ranking_summary_{tf}min_df",
        f"ranking_summary_{tf}min",
        f"latest_ranking_summary_{tf}min",
        f"ranking_df_{tf}min",
        f"ranking_summary_{tf}m",
        f"ranking_summary_df_{tf}m",
    ]


def _get_push_summary_df(interval: int) -> pd.DataFrame:
    out = _normalize_df(_get_from_global_data(_push_keys(interval)))
    logger.info(
        "[summary_display_engine] push_df resolved interval=%s rows=%s latest=%s cols=%s",
        interval,
        _row_count(out),
        _latest_dt_value(out),
        _safe_cols(out),
    )
    return out


def _get_ranking_summary_df(interval: int) -> pd.DataFrame:
    out = _normalize_df(_get_from_global_data(_ranking_keys(interval)))
    logger.info(
        "[summary_display_engine] ranking_df resolved interval=%s rows=%s latest=%s cols=%s",
        interval,
        _row_count(out),
        _latest_dt_value(out),
        _safe_cols(out),
    )
    return out


# ============================================================
# fallback display
# ============================================================

def _log_header(prefix: str, interval: int, df: pd.DataFrame) -> None:
    logger.info("")
    logger.info("=== ⏱ 最新 %smin %s｜%s ===", int(interval), prefix, _latest_dt_text(df))


def _pick_overview_row(df: pd.DataFrame) -> Optional[pd.Series]:
    out = _latest_slice(df)
    out = _dedupe_by_symbol(out)
    if out.empty:
        return None

    score_col = _pick_score_col(out)
    dt_col = _pick_dt_col(out)

    try:
        sort_cols = []
        ascending = []
        if dt_col:
            sort_cols.append(dt_col)
            ascending.append(False)
        if score_col:
            sort_cols.append(score_col)
            ascending.append(False)
        if sort_cols:
            out = out.sort_values(sort_cols, ascending=ascending, kind="stable")
        return out.iloc[0]
    except Exception:
        logger.debug("[summary_display_engine] pick overview row failed", exc_info=True)
        try:
            return out.iloc[0]
        except Exception:
            return None


def _log_overview(df: pd.DataFrame) -> None:
    row = _pick_overview_row(df)
    if row is None:
        return

    work = _normalize_df(df)
    sym_col = _pick_symbol_col(work)
    name_col = _pick_name_col(work)
    open_col = _pick_open_col(work)
    high_col = _pick_high_col(work)
    low_col = _pick_low_col(work)
    close_col = _pick_close_col(work)

    logger.info(
        "%s %s 始:%s 高:%s 安:%s 終:%s",
        _fmt_symbol(row.get(sym_col) if sym_col else "-", 6),
        _fmt_name(row.get(name_col) if name_col else row.get(sym_col) if sym_col else "-", 28).rstrip(),
        _fmt_price(row.get(open_col) if open_col else None),
        _fmt_price(row.get(high_col) if high_col else None),
        _fmt_price(row.get(low_col) if low_col else None),
        _fmt_price(row.get(close_col) if close_col else None),
    )


def _score_series(df: pd.DataFrame) -> pd.Series:
    score_col = _pick_score_col(df)
    if not score_col:
        logger.info("[summary_display_engine] fallback score column missing cols=%s", _safe_cols(df))
        return pd.Series(dtype="float64")
    try:
        s = pd.to_numeric(df[score_col], errors="coerce")
        logger.info(
            "[summary_display_engine] fallback score column=%s pos=%s neg=%s nonnull=%s",
            score_col,
            int((s.fillna(0) > 0).sum()),
            int((s.fillna(0) < 0).sum()),
            int(s.notna().sum()),
        )
        return s
    except Exception:
        return pd.Series(dtype="float64")


def _sort_buy(df: pd.DataFrame) -> pd.DataFrame:
    out = _normalize_df(df)
    if out.empty:
        return out

    score_col = _pick_score_col(out)
    dt_col = _pick_dt_col(out)
    sym_col = _pick_symbol_col(out)

    try:
        sort_cols = []
        asc = []
        if score_col:
            sort_cols.append(score_col)
            asc.append(False)
        if dt_col:
            sort_cols.append(dt_col)
            asc.append(False)
        if sym_col:
            sort_cols.append(sym_col)
            asc.append(True)
        if sort_cols:
            out = out.sort_values(sort_cols, ascending=asc, kind="stable")
    except Exception:
        logger.debug("[summary_display_engine] sort_buy failed", exc_info=True)

    return out.reset_index(drop=True)


def _sort_sell(df: pd.DataFrame) -> pd.DataFrame:
    out = _normalize_df(df)
    if out.empty:
        return out

    score_col = _pick_score_col(out)
    dt_col = _pick_dt_col(out)
    sym_col = _pick_symbol_col(out)

    try:
        sort_cols = []
        asc = []
        if score_col:
            sort_cols.append(score_col)
            asc.append(True)
        if dt_col:
            sort_cols.append(dt_col)
            asc.append(False)
        if sym_col:
            sort_cols.append(sym_col)
            asc.append(True)
        if sort_cols:
            out = out.sort_values(sort_cols, ascending=asc, kind="stable")
    except Exception:
        logger.debug("[summary_display_engine] sort_sell failed", exc_info=True)

    return out.reset_index(drop=True)


def _log_push_rows(df: pd.DataFrame, interval: int) -> None:
    out = _latest_slice(df)
    out = _dedupe_by_symbol(out)
    if out.empty:
        logger.info(" (no data)")
        return

    score_s = _score_series(out)
    buy = out.loc[score_s.fillna(0) > 0].copy()
    sell = out.loc[score_s.fillna(0) < 0].copy()

    logger.info(
        "[summary_display_engine] fallback push interval=%s latest_rows=%s buy_rows=%s sell_rows=%s",
        interval,
        _row_count(out),
        _row_count(buy),
        _row_count(sell),
    )

    buy = _sort_buy(buy).head(10)
    sell = _sort_sell(sell).head(10)

    logger.info("")
    logger.info("========== 📊 SUMMARY TOP10 (%smin) ==========", int(interval))
    logger.info("🔵 BUY TOP10（score / slope / mtf / rsi / macd）")

    if buy.empty:
        logger.info(" (no buy candidates)")
    else:
        sym_col = _pick_symbol_col(buy)
        name_col = _pick_name_col(buy)
        score_col = _pick_score_col(buy)
        slope_col = _pick_slope_col(buy)
        mtf_col = _pick_mtf_col(buy)
        rsi_col = _pick_rsi_col(buy)
        macd_col = _pick_macd_col(buy)

        for i, (_, row) in enumerate(buy.iterrows(), start=1):
            logger.info(
                "%2d. ⚪ %s %s score=%7s slope=%6s mtf=%6s rsi=%6s macd=%7s",
                i,
                _fmt_symbol(row.get(sym_col) if sym_col else "-", 6),
                _fmt_name(row.get(name_col) if name_col else row.get(sym_col) if sym_col else "-", 30),
                _fmt_metric(row.get(score_col) if score_col else None),
                _fmt_metric(row.get(slope_col) if slope_col else None),
                _fmt_metric(row.get(mtf_col) if mtf_col else None),
                _fmt_metric(row.get(rsi_col) if rsi_col else None),
                _fmt_metric(row.get(macd_col) if macd_col else None),
            )

    logger.info("🔴 SELL TOP10（下落圧が強い）")
    if sell.empty:
        logger.info(" (no sell candidates)")
    else:
        sym_col = _pick_symbol_col(sell)
        name_col = _pick_name_col(sell)
        score_col = _pick_score_col(sell)
        slope_col = _pick_slope_col(sell)
        mtf_col = _pick_mtf_col(sell)
        rsi_col = _pick_rsi_col(sell)
        macd_col = _pick_macd_col(sell)

        for i, (_, row) in enumerate(sell.iterrows(), start=1):
            logger.info(
                "%2d. 🔴 %s %s score=%7s slope=%6s mtf=%6s rsi=%6s macd=%7s",
                i,
                _fmt_symbol(row.get(sym_col) if sym_col else "-", 6),
                _fmt_name(row.get(name_col) if name_col else row.get(sym_col) if sym_col else "-", 30),
                _fmt_metric(row.get(score_col) if score_col else None),
                _fmt_metric(row.get(slope_col) if slope_col else None),
                _fmt_metric(row.get(mtf_col) if mtf_col else None),
                _fmt_metric(row.get(rsi_col) if rsi_col else None),
                _fmt_metric(row.get(macd_col) if macd_col else None),
            )


def _log_ranking_rows(df: pd.DataFrame, interval: int) -> None:
    out = _latest_slice(df)
    out = _dedupe_by_symbol(out)
    if out.empty:
        logger.info(" (no data)")
        return

    score_s = _score_series(out)
    buy = out.loc[score_s.fillna(0) > 0].copy()
    sell = out.loc[score_s.fillna(0) < 0].copy()

    logger.info(
        "[summary_display_engine] fallback ranking interval=%s latest_rows=%s buy_rows=%s sell_rows=%s",
        interval,
        _row_count(out),
        _row_count(buy),
        _row_count(sell),
    )

    buy = _sort_buy(buy).head(10)
    sell = _sort_sell(sell).head(10)

    logger.info("")
    logger.info("========== 📊 RANKING SUMMARY TOP10 (%smin) ==========", int(interval))
    logger.info("🔵 BUY TOP10（score / slope / mtf / rsi / macd / best_rank / hist / type）")

    if buy.empty:
        logger.info(" (no buy candidates)")
    else:
        sym_col = _pick_symbol_col(buy)
        name_col = _pick_name_col(buy)
        score_col = _pick_score_col(buy)
        slope_col = _pick_slope_col(buy)
        mtf_col = _pick_mtf_col(buy)
        rsi_col = _pick_rsi_col(buy)
        macd_col = _pick_macd_col(buy)
        best_col = _pick_best_col(buy)
        hist_col = _pick_hist_col(buy)
        type_col = _pick_type_col(buy)

        for i, (_, row) in enumerate(buy.iterrows(), start=1):
            logger.info(
                "%2d. ⚪ %s %s score=%7s slope=%6s mtf=%6s rsi=%6s macd=%7s best=%3s hist=%3s type=%s",
                i,
                _fmt_symbol(row.get(sym_col) if sym_col else "-", 6),
                _fmt_name(row.get(name_col) if name_col else row.get(sym_col) if sym_col else "-", 30),
                _fmt_metric(row.get(score_col) if score_col else None),
                _fmt_metric(row.get(slope_col) if slope_col else None),
                _fmt_metric(row.get(mtf_col) if mtf_col else None),
                _fmt_metric(row.get(rsi_col) if rsi_col else None),
                _fmt_metric(row.get(macd_col) if macd_col else None),
                _fmt_text(row.get(best_col) if best_col else None),
                _fmt_text(row.get(hist_col) if hist_col else None),
                _fmt_text(row.get(type_col) if type_col else None),
            )

    logger.info("🔴 SELL TOP10（下落圧が強い）")
    if sell.empty:
        logger.info(" (no sell candidates)")
    else:
        sym_col = _pick_symbol_col(sell)
        name_col = _pick_name_col(sell)
        score_col = _pick_score_col(sell)
        slope_col = _pick_slope_col(sell)
        mtf_col = _pick_mtf_col(sell)
        rsi_col = _pick_rsi_col(sell)
        macd_col = _pick_macd_col(sell)
        best_col = _pick_best_col(sell)
        hist_col = _pick_hist_col(sell)
        type_col = _pick_type_col(sell)

        for i, (_, row) in enumerate(sell.iterrows(), start=1):
            logger.info(
                "%2d. 🔴 %s %s score=%7s slope=%6s mtf=%6s rsi=%6s macd=%7s best=%3s hist=%3s type=%s",
                i,
                _fmt_symbol(row.get(sym_col) if sym_col else "-", 6),
                _fmt_name(row.get(name_col) if name_col else row.get(sym_col) if sym_col else "-", 30),
                _fmt_metric(row.get(score_col) if score_col else None),
                _fmt_metric(row.get(slope_col) if slope_col else None),
                _fmt_metric(row.get(mtf_col) if mtf_col else None),
                _fmt_metric(row.get(rsi_col) if rsi_col else None),
                _fmt_metric(row.get(macd_col) if macd_col else None),
                _fmt_text(row.get(best_col) if best_col else None),
                _fmt_text(row.get(hist_col) if hist_col else None),
                _fmt_text(row.get(type_col) if type_col else None),
            )


def _fallback_display_push(df: pd.DataFrame, interval: int) -> None:
    out = _normalize_df(df)
    if out.empty:
        logger.info("[summary_display_engine] push summary empty interval=%s", interval)
        return
    logger.info("[summary_display_engine] push display route=fallback interval=%s", interval)
    _log_header("サマリー", interval, out)
    _log_overview(out)
    _log_push_rows(out, interval)


def _fallback_display_ranking(df: pd.DataFrame, interval: int) -> None:
    out = _normalize_df(df)
    if out.empty:
        logger.info("[summary_display_engine] ranking summary empty interval=%s", interval)
        return
    logger.info("[summary_display_engine] ranking display route=fallback interval=%s", interval)
    _log_header("ランキングサマリー", interval, out)
    _log_overview(out)
    _log_ranking_rows(out, interval)


# ============================================================
# public api
# ============================================================

def run_summary_display(
    intervals: Sequence[int] | None = None,
    *,
    show_push: bool = True,
    show_ranking: bool = True,
) -> bool:
    try:
        target_intervals = tuple(int(x) for x in (intervals or DEFAULT_INTERVALS))
        logger.info(
            "[summary_display_engine] run start intervals=%s show_push=%s show_ranking=%s",
            target_intervals,
            show_push,
            show_ranking,
        )

        ext_push, ext_rank = _resolve_external_display_functions()

        for interval in target_intervals:
            if show_push:
                try:
                    push_df = _get_push_summary_df(interval)
                    if not push_df.empty:
                        if callable(ext_push):
                            logger.info("[summary_display_engine] push display route=external interval=%s", interval)
                            ext_push(push_df, interval=interval)
                        else:
                            _fallback_display_push(push_df, interval)
                    else:
                        logger.info(
                            "[summary_display_engine] push display skipped interval=%s reason=empty_df",
                            interval,
                        )
                except Exception:
                    logger.exception("[summary_display_engine] push display failed interval=%s", interval)

            if show_ranking:
                try:
                    ranking_df = _get_ranking_summary_df(interval)
                    if not ranking_df.empty:
                        if callable(ext_rank):
                            logger.info("[summary_display_engine] ranking display route=external interval=%s", interval)
                            ext_rank(ranking_df, interval=interval)
                        else:
                            _fallback_display_ranking(ranking_df, interval)
                    else:
                        logger.info(
                            "[summary_display_engine] ranking display skipped interval=%s reason=empty_df",
                            interval,
                        )
                except Exception:
                    logger.exception("[summary_display_engine] ranking display failed interval=%s", interval)

        logger.info("[summary_display_engine] run complete")
        return True

    except Exception:
        logger.exception("[summary_display_engine] run failed")
        return False


__all__ = [
    "run_summary_display",
]