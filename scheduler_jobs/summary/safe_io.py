# ============================================================
# File   : scheduler_jobs/summary/safe_io.py
# Version: PRODUCTION-STABLE-SUMMARY-SAFE-IO-V1.6-RANKING-DISPLAY-CALC
# ------------------------------------------------------------
# 【概要】
#   summary DB保存 / PUSH表示 / RANKING表示の安全ラッパー。
#
# V1.6:
#   - RANKING表示前にも score / slope / mtf / macd / ranking_type を補完
#   - DB保存時だけ埋まって、Discord/画面表示では未入力になる問題を防止
#   - ranking_type / type / rank_types の別名を相互補完
#   - RANKINGでは 0 値も未計算扱いにして、算出値で補完する最終防衛を追加
#
# V1.5:
#   - DB保存直前に rsi/macd/signal/atr/slope/ma/score/mtf を最低限再計算
#   - runnerが返したDFに指標列が無い、またはNULL/空でもDB保存前に埋める
#   - OHLC alias / date / time / time_range / source / interval / updated_at も補完
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable

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


IMPORTANT_SAVE_COLUMNS = [
    "open_price", "high_price", "low_price", "close_price", "volume", "time_range",
    "rsi", "macd", "signal", "atr", "slope", "slope_atr_scaled",
    "ma5", "ma25", "ma75",
    "score", "score_buy", "score_sell", "score_total", "final_score", "display_score",
    "score_mtf", "mtf", "mtf_score",
    "ranking_score", "ranking_score_total", "ranking_type", "type", "rank_types", "rank",
]


def _is_ranking_source(source: str) -> bool:
    try:
        return str(source or "").strip().lower().startswith("ranking")
    except Exception:
        return False


def _as_num(s, default: float = 0.0) -> pd.Series:
    try:
        if isinstance(s, pd.Series):
            return pd.to_numeric(s, errors="coerce").fillna(default).astype("float64")
    except Exception:
        pass
    return pd.Series(dtype="float64")


def _clean_text_series(s: pd.Series) -> pd.Series:
    try:
        return (
            s.fillna("")
            .astype(str)
            .str.strip()
            .replace({"nan": "", "NaN": "", "None": "", "<NA>": "", "pd.NA": ""})
        )
    except Exception:
        return pd.Series("", index=getattr(s, "index", None), dtype="object")


def _first_text(out: pd.DataFrame, candidates: Iterable[str], default: str = "") -> pd.Series:
    base = pd.Series(default, index=out.index, dtype="object")
    for col in candidates:
        if col not in out.columns:
            continue
        try:
            s = out[col]
            if isinstance(s, pd.DataFrame):
                if s.shape[1] == 0:
                    continue
                s = s.iloc[:, 0]
            ss = _clean_text_series(s)
            mask = base.astype(str).str.len().eq(0) & ss.astype(str).str.len().gt(0)
            base.loc[mask] = ss.loc[mask]
        except Exception:
            continue
    return base


def _fill_missing_or_zero(out: pd.DataFrame, col: str, values, *, zero_is_missing: bool = False) -> None:
    try:
        if not isinstance(values, pd.Series):
            values = pd.Series(values, index=out.index)
        else:
            values = values.reindex(out.index)

        if col not in out.columns:
            out[col] = values
            return

        base = out[col]
        if isinstance(base, pd.DataFrame):
            base = base.iloc[:, 0] if base.shape[1] else pd.Series(index=out.index)

        mask = base.isna() | base.astype(str).str.strip().isin(["", "nan", "NaN", "None", "<NA>", "pd.NA"])
        if zero_is_missing:
            try:
                mask = mask | pd.to_numeric(base, errors="coerce").fillna(0).eq(0)
            except Exception:
                pass
        out[col] = base.where(~mask, values)
    except Exception:
        try:
            out[col] = values
        except Exception:
            logger.debug("[SUMMARY SAFE IO] fill failed col=%s", col, exc_info=True)


def _fill_ranking_type_aliases(out: pd.DataFrame) -> pd.DataFrame:
    """ranking_type / type / rank_types を相互補完する。"""
    try:
        if out is None or out.empty:
            return out
        rank_type = _first_text(
            out,
            [
                "ranking_type", "rank_type", "rank_types", "type", "Type",
                "ranking_category", "category", "ランキング種別", "ランキングタイプ",
            ],
            default="",
        )
        _fill_missing_or_zero(out, "ranking_type", rank_type)
        _fill_missing_or_zero(out, "rank_types", out["ranking_type"])
        _fill_missing_or_zero(out, "type", out["ranking_type"])
        return out
    except Exception:
        logger.debug("[SUMMARY SAFE IO] ranking type alias fill failed", exc_info=True)
        return out


def _normalize_datetime_and_basic_columns(df: pd.DataFrame, interval: int, source: str) -> pd.DataFrame:
    out = df.copy()
    interval = int(interval)

    if "datetime" not in out.columns:
        for c in ("dt", "timestamp", "end_time", "snapshot_time", "inserted_at", "created_at"):
            if c in out.columns:
                out["datetime"] = out[c]
                break

    if "datetime" in out.columns:
        dt_ser = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            dt_ser = dt_ser.dt.tz_localize(None)
        except Exception:
            pass
        out["datetime"] = dt_ser
        _fill_missing_or_zero(out, "date", dt_ser.dt.strftime("%Y-%m-%d"))
        _fill_missing_or_zero(out, "time", dt_ser.dt.strftime("%H:%M:%S"))
        start_txt = dt_ser.dt.strftime("%H:%M:%S")
        end_txt = (dt_ser + pd.to_timedelta(interval, unit="m") - pd.to_timedelta(1, unit="s")).dt.strftime("%H:%M:%S")
        _fill_missing_or_zero(out, "time_range", start_txt + "-" + end_txt)
        _fill_missing_or_zero(out, "start_time", start_txt)
        _fill_missing_or_zero(out, "end_time", end_txt)

    if "symbol" in out.columns:
        out["symbol"] = (
            out["symbol"].astype(str).str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .str.replace(r"\.T$", "", regex=True)
        )

    if "symbolname" not in out.columns:
        name = _first_text(out, ["symbolname_view", "name", "symbol_name", "銘柄名"], default="")
        if "symbol" in out.columns:
            name = name.where(name.astype(str).str.len().gt(0), out["symbol"].astype(str))
        out["symbolname"] = name
    if "symbolname_view" not in out.columns:
        out["symbolname_view"] = out["symbolname"]

    _fill_missing_or_zero(out, "source", source)
    _fill_missing_or_zero(out, "summary_source", str(source).lower())
    _fill_missing_or_zero(out, "interval", int(interval))
    _fill_missing_or_zero(out, "updated_at", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _fill_missing_or_zero(out, "last_update", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    alias_pairs = [("open", "open_price"), ("high", "high_price"), ("low", "low_price"), ("close", "close_price")]
    for a, b in alias_pairs:
        if a in out.columns and b not in out.columns:
            out[b] = out[a]
        elif b in out.columns and a not in out.columns:
            out[a] = out[b]
        elif a in out.columns and b in out.columns:
            _fill_missing_or_zero(out, b, out[a], zero_is_missing=True)
            _fill_missing_or_zero(out, a, out[b], zero_is_missing=True)

    if "close_price" not in out.columns:
        for c in ("close", "current_price", "price", "現在値"):
            if c in out.columns:
                out["close_price"] = out[c]
                break

    if "close_price" in out.columns:
        close = _as_num(out["close_price"], 0.0)
        out["close_price"] = close
        for c in ("price", "current_price"):
            _fill_missing_or_zero(out, c, close, zero_is_missing=True)
        for c in ("open_price", "high_price", "low_price", "open", "high", "low", "close"):
            _fill_missing_or_zero(out, c, close, zero_is_missing=True)

    if "volume" not in out.columns:
        for c in ("trading_volume", "vol", "出来高"):
            if c in out.columns:
                out["volume"] = out[c]
                break
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out["volume"] = _as_num(out["volume"], 0.0)

    if "turnover" not in out.columns:
        if "trading_value" in out.columns:
            out["turnover"] = _as_num(out["trading_value"], 0.0)
        elif "close_price" in out.columns:
            out["turnover"] = _as_num(out["close_price"], 0.0) * _as_num(out["volume"], 0.0)
        else:
            out["turnover"] = 0.0
    else:
        out["turnover"] = _as_num(out["turnover"], 0.0)
    _fill_missing_or_zero(out, "trading_value", out["turnover"], zero_is_missing=True)

    if "rank" not in out.columns:
        for c in ("rank_position", "ranking_rank", "best_rank_position", "順位"):
            if c in out.columns:
                out["rank"] = pd.to_numeric(out[c], errors="coerce")
                break
    if "rank" not in out.columns:
        out["rank"] = pd.NA

    if "vwap" not in out.columns:
        vol = _as_num(out.get("volume", pd.Series(index=out.index)), 0.0)
        turn = _as_num(out.get("turnover", pd.Series(index=out.index)), 0.0)
        close = _as_num(out.get("close_price", pd.Series(index=out.index)), 0.0)
        out["vwap"] = (turn / vol.replace(0, pd.NA)).fillna(close)

    out = _fill_ranking_type_aliases(out)
    return out


def _type_bias_for_buy(rank_type: pd.Series) -> pd.Series:
    try:
        txt = rank_type.fillna("").astype(str)
        buy_words = ("値上", "上昇", "買", "TICK", "出来高", "売買代金", "急騰")
        sell_words = ("値下", "下落", "売", "急落")
        buy = txt.apply(lambda x: any(w in x for w in buy_words)).astype(float)
        sell = txt.apply(lambda x: any(w in x for w in sell_words)).astype(float)
        return buy - sell
    except Exception:
        return pd.Series(0.0, index=rank_type.index, dtype="float64")


def _calculate_core_technical_columns(df: pd.DataFrame, interval: int, source: str) -> pd.DataFrame:
    """保存・表示前の最終防衛として主要テクニカルを補完する。"""
    if not is_nonempty_df(df):
        return df

    try:
        import numpy as np

        ranking_mode = _is_ranking_source(source)
        out = _normalize_datetime_and_basic_columns(df, interval, source)
        if "symbol" not in out.columns or "datetime" not in out.columns or "close_price" not in out.columns:
            logger.warning(
                "[SUMMARY SAVE CALC] skipped interval=%s source=%s reason=missing_required cols=%s",
                interval,
                source,
                list(out.columns),
            )
            return out

        out = out.dropna(subset=["symbol", "datetime"]).copy()
        if out.empty:
            return out
        out = out.sort_values(["symbol", "datetime"], kind="mergesort").copy()
        out = _fill_ranking_type_aliases(out)

        parts = []
        for _, g in out.groupby("symbol", sort=False):
            g = g.sort_values("datetime", kind="mergesort").copy()
            close = _as_num(g["close_price"], 0.0)
            high = _as_num(g.get("high_price", close), 0.0).replace(0, pd.NA).fillna(close)
            low = _as_num(g.get("low_price", close), 0.0).replace(0, pd.NA).fillna(close)

            ma5 = close.rolling(5, min_periods=1).mean().fillna(close)
            ma25 = close.rolling(25, min_periods=1).mean().fillna(close)
            ma75 = close.rolling(75, min_periods=1).mean().fillna(close)
            _fill_missing_or_zero(g, "ma5", ma5, zero_is_missing=True)
            _fill_missing_or_zero(g, "ma25", ma25, zero_is_missing=True)
            _fill_missing_or_zero(g, "ma75", ma75, zero_is_missing=True)

            ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean().fillna(close)
            ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean().fillna(close)
            macd = (ema12 - ema26).fillna(0.0)
            signal = macd.ewm(span=9, adjust=False, min_periods=1).mean().fillna(0.0)
            hist = (macd - signal).fillna(0.0)
            _fill_missing_or_zero(g, "ema12", ema12, zero_is_missing=True)
            _fill_missing_or_zero(g, "ema26", ema26, zero_is_missing=True)
            _fill_missing_or_zero(g, "macd", macd, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "signal", signal, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "hist", hist, zero_is_missing=ranking_mode)

            delta = close.diff().fillna(0.0)
            gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
            loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
            rs = gain / loss.replace(0, pd.NA)
            rsi = (100 - (100 / (1 + rs))).fillna(50.0).clip(0, 100)
            _fill_missing_or_zero(g, "rsi", rsi)

            prev_close = close.shift(1)
            tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1).fillna(0.0)
            atr = tr.rolling(14, min_periods=1).mean().fillna(0.0)
            _fill_missing_or_zero(g, "atr", atr)
            _fill_missing_or_zero(g, f"atr_{int(interval)}m", atr)

            pct1 = close.pct_change(1).replace([np.inf, -np.inf], pd.NA).fillna(0.0)
            pct3 = close.pct_change(3).replace([np.inf, -np.inf], pd.NA).fillna(0.0)
            pct5 = close.pct_change(5).replace([np.inf, -np.inf], pd.NA).fillna(0.0)
            slope = pct1 if ranking_mode else pct3
            atr_pct = (atr / close.replace(0, pd.NA)).replace([np.inf, -np.inf], pd.NA).fillna(0.0)
            slope_atr_scaled = (slope / atr_pct.replace(0, pd.NA)).replace([np.inf, -np.inf], pd.NA).fillna(0.0)
            mtf_calc = ((pct1 * 0.50) + (pct3 * 0.30) + (pct5 * 0.20)).fillna(0.0)
            _fill_missing_or_zero(g, "slope", slope, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "slope_atr_scaled", slope_atr_scaled, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, f"slope_atr_scaled_{int(interval)}m", slope_atr_scaled, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "score_slope", (slope * 100.0).clip(-3, 3).fillna(0.0), zero_is_missing=ranking_mode)

            ma_buy = ((ma5 > ma25).astype(float) + (ma25 > ma75).astype(float))
            ma_sell = ((ma5 < ma25).astype(float) + (ma25 < ma75).astype(float))
            macd_buy = (macd >= signal).astype(float)
            macd_sell = (macd < signal).astype(float)
            rsi_buy = ((rsi >= 50) & (rsi <= 80)).astype(float)
            rsi_sell = ((rsi <= 50) & (rsi >= 20)).astype(float)
            slope_buy = (slope >= 0).astype(float)
            slope_sell = (slope < 0).astype(float)

            type_bias = _type_bias_for_buy(g["ranking_type"]) if "ranking_type" in g.columns else pd.Series(0.0, index=g.index)
            rank = pd.to_numeric(g.get("rank", pd.Series(index=g.index)), errors="coerce")
            rank_score = ((101.0 - rank.clip(lower=1, upper=100)) / 20.0).fillna(0.0)

            if ranking_mode:
                score_buy = (rank_score + slope_buy + macd_buy + rsi_buy + type_bias.clip(lower=0)).fillna(0.0)
                score_sell = (rank_score + slope_sell + macd_sell + rsi_sell + (-type_bias.clip(upper=0))).fillna(0.0)
                score_total = (score_buy + score_sell).fillna(0.0)
                score_main = pd.concat([score_buy, score_sell], axis=1).max(axis=1).fillna(0.0)
                score_mtf = mtf_calc
            else:
                score_buy = (ma_buy + macd_buy + rsi_buy + slope_buy).fillna(0.0)
                score_sell = (ma_sell + macd_sell + rsi_sell + slope_sell).fillna(0.0)
                score_total = (score_buy - score_sell).fillna(0.0)
                score_main = score_total
                score_mtf = (ma_buy - ma_sell).fillna(0.0)

            _fill_missing_or_zero(g, "score_buy", score_buy, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "score_sell", score_sell, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "buy_score", score_buy, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "sell_score", score_sell, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "score_total", score_total, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "total_score", score_total, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "score", score_main, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "final_score", score_main, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "display_score", score_main, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "score_mtf", score_mtf, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "mtf", score_mtf, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "mtf_score", score_mtf, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "mtf_alignment", score_mtf, zero_is_missing=ranking_mode)
            _fill_missing_or_zero(g, "technical_ready", 1)
            _fill_missing_or_zero(g, "display_ready", 1)
            _fill_missing_or_zero(g, "symbol_hist_len", len(g))
            g = _fill_ranking_type_aliases(g)
            parts.append(g)

        if not parts:
            return out
        out2 = pd.concat(parts, ignore_index=True)
        out2 = _fill_ranking_type_aliases(out2)
        logger.warning(
            "[SUMMARY SAVE CALC] done source=%s interval=%s rows=%s cols=%s score_nonzero=%s slope_nonzero=%s mtf_nonzero=%s macd_nonnull=%s type_nonempty=%s",
            source,
            interval,
            len(out2),
            len(out2.columns),
            int(pd.to_numeric(out2.get("score", pd.Series(index=out2.index)), errors="coerce").fillna(0).ne(0).sum()),
            int(pd.to_numeric(out2.get("slope", pd.Series(index=out2.index)), errors="coerce").fillna(0).ne(0).sum()),
            int(pd.to_numeric(out2.get("mtf", pd.Series(index=out2.index)), errors="coerce").fillna(0).ne(0).sum()),
            int(pd.to_numeric(out2.get("macd", pd.Series(index=out2.index)), errors="coerce").notna().sum()),
            int(_clean_text_series(out2.get("ranking_type", pd.Series(index=out2.index))).str.len().gt(0).sum()),
        )
        return out2

    except Exception:
        logger.exception("[SUMMARY SAVE CALC] failed source=%s interval=%s -> use original df", source, interval)
        return df


def _enrich_for_display(df: pd.DataFrame, interval: int, source: str, context: str) -> pd.DataFrame:
    if not is_nonempty_df(df):
        return df
    try:
        from trading.summary.controller_enrich import enrich_summary_latest
        out = enrich_summary_latest(df, interval=int(interval), context=f"scheduler-{source.lower()}-{context}")
        logger.info(
            "[summary.runners] enrich_for_%s source=%s interval=%s rows=%s cols=%s",
            context,
            source,
            interval,
            len(out) if isinstance(out, pd.DataFrame) else None,
            len(out.columns) if isinstance(out, pd.DataFrame) else None,
        )
        return out
    except Exception:
        logger.exception("[summary.runners] enrich_for_display failed source=%s interval=%s context=%s", source, interval, context)
        return df


def _log_save_column_health(df: pd.DataFrame, interval: int, source: str, stage: str) -> None:
    if not is_nonempty_df(df):
        logger.warning("[SUMMARY SAVE HEALTH] source=%s interval=%s stage=%s rows=0 reason=empty", source, interval, stage)
        return
    try:
        payload = {}
        for c in IMPORTANT_SAVE_COLUMNS:
            if c not in df.columns:
                payload[c] = "MISSING"
                continue
            s = df[c]
            nulls = int(s.isna().sum())
            zeros = None
            try:
                num = pd.to_numeric(s, errors="coerce")
                zeros = int(num.fillna(0).eq(0).sum())
            except Exception:
                pass
            payload[c] = {"null": nulls, "zero": zeros}
        logger.warning(
            "[SUMMARY SAVE HEALTH] source=%s interval=%s stage=%s rows=%s cols=%s health=%s",
            source,
            interval,
            stage,
            len(df),
            len(df.columns),
            payload,
        )
    except Exception:
        logger.exception("[SUMMARY SAVE HEALTH] failed source=%s interval=%s stage=%s", source, interval, stage)


def _prepare_summary_for_output(df: pd.DataFrame, interval: int, source: str, context: str) -> pd.DataFrame:
    out = _calculate_core_technical_columns(df, interval, source)
    out = _enrich_for_display(out, interval, source.upper(), context)
    out = _fill_ranking_type_aliases(out)
    _log_save_column_health(out, interval, source, context)
    return out


# ============================================================
# 保存安全ラッパー
# ============================================================

def save_summary_safe(df: pd.DataFrame, interval: int, source: str) -> bool:
    try:
        rows = df_rows(df)
        if not is_nonempty_df(df):
            logger.warning("[summary.runners] save_summary skipped source=%s interval=%s reason=empty_df rows=%d", source, interval, rows)
            return False

        logger.info("[summary.runners] save_summary start source=%s interval=%s rows=%d", source, interval, rows)
        _log_save_column_health(df, interval, source, "before_calc")
        df_to_save = _prepare_summary_for_output(df, interval, source, "after_calc_before_db")
        save_merged_summary(df_to_save, interval, source=source)
        logger.info(
            "[summary.runners] save_summary success source=%s interval=%s rows=%d saved_cols=%s",
            source,
            interval,
            len(df_to_save) if isinstance(df_to_save, pd.DataFrame) else rows,
            len(df_to_save.columns) if isinstance(df_to_save, pd.DataFrame) else None,
        )
        return True
    except Exception:
        logger.exception("[summary.runners] save_summary failed source=%s interval=%s", source, interval)
        return False


# ============================================================
# PUSHサマリー表示安全ラッパー
# ============================================================

def display_push_summary_safe(df: pd.DataFrame, interval: int, now: dt.datetime) -> bool:
    try:
        if not is_nonempty_df(df):
            logger.warning("[summary.runners] display_push_summary skipped interval=%s reason=empty_df now=%s", interval, now)
            return False

        _, slot_dt = resolve_display_slot(interval=interval, now=now)
        df = _prepare_summary_for_output(df, interval, "push", "display-push-before-liquidity")

        logger.info("[summary.runners] display_push_summary start interval=%s rows=%d now=%s slot=%s", interval, len(df), now, slot_dt)
        log_df_state("display_push_input_before_liquidity", interval, df)
        log_liquidity_profile(df, interval=interval, source="PUSH", label="display_push_before_filter")

        display_df = filter_liquid_summary_for_display(df, interval=interval, source="PUSH")
        if not is_nonempty_df(display_df):
            logger.warning(
                "[summary.runners] display_push_summary skipped interval=%s reason=empty_after_liquidity_filter before_rows=%d now=%s slot=%s",
                interval,
                len(df),
                now,
                slot_dt,
            )
            return False

        display_df = _prepare_summary_for_output(display_df, interval, "push", "display-push-after-liquidity")
        log_df_state("display_push_input_after_liquidity", interval, display_df)
        log_liquidity_profile(display_df, interval=interval, source="PUSH", label="display_push_after_filter")
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
        logger.exception("[summary.runners] display_push_summary failed interval=%s now=%s", interval, now)
        return False


# ============================================================
# RANKINGサマリー表示安全ラッパー
# ============================================================

def display_ranking_summary_safe(df: pd.DataFrame, interval: int, now: dt.datetime) -> bool:
    try:
        if not is_nonempty_df(df):
            logger.warning("[summary.runners] display_ranking_summary skipped interval=%s reason=empty_df now=%s", interval, now)
            return False

        _, slot_dt = resolve_display_slot(interval=interval, now=now)

        # 重要: 保存前だけでなく、Discord/画面表示前にもランキング由来の指標・タイプを補完する。
        df = _prepare_summary_for_output(df, interval, "ranking", "display-ranking-before-liquidity")

        logger.info("[summary.runners] display_ranking_summary start interval=%s rows=%d now=%s slot=%s", interval, len(df), now, slot_dt)
        log_df_state("display_ranking_input_before_liquidity", interval, df)
        log_liquidity_profile(df, interval=interval, source="RANKING", label="display_ranking_before_filter")

        display_df = filter_liquid_summary_for_display(df, interval=interval, source="RANKING")
        if not is_nonempty_df(display_df):
            logger.warning(
                "[summary.runners] display_ranking_summary skipped interval=%s reason=empty_after_liquidity_filter before_rows=%d now=%s slot=%s",
                interval,
                len(df),
                now,
                slot_dt,
            )
            return False

        display_df = _prepare_summary_for_output(display_df, interval, "ranking", "display-ranking-after-liquidity")
        log_df_state("display_ranking_input_after_liquidity", interval, display_df)
        log_liquidity_profile(display_df, interval=interval, source="RANKING", label="display_ranking_after_filter")
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
        logger.exception("[summary.runners] display_ranking_summary failed interval=%s now=%s", interval, now)
        return False
