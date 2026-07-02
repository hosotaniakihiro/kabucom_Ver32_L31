# ============================================================
# File   : trading/summary/engine/push_summary_engine.py
# Version: Ver1.2.1-PUSH-EMPTY-NO-PREVIOUS-REUSE
#          -PUSH-ONLY
#          -PIPELINE-FIRST
#          -IMPORT-COMPAT
#          -NO-PREVIOUS-DAY-REUSE
#          -DIRECT-OHLC-FALLBACK-WHEN-PIPELINE-EMPTY
# ------------------------------------------------------------
# ✔ PUSHベースのサマリー計算専用
# ✔ ranking 系ロジックは一切持たない
# ✔ push_rows=0 時に前回/前日 PUSH merged summary を再投入しない
# ✔ 前日分や巨大キャッシュを parent tick に渡してタイムアウトさせない
# ✔ PUSH が来た時だけ当日データに絞って pipeline / direct OHLC fallback を実行
# ============================================================
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from global_state import global_data
except Exception:
    try:
        from core.global_context.context import global_data  # type: ignore
    except Exception:
        global_data = None

try:
    from trading.summary.engine.data_quality_engine import run_data_quality_checks
except Exception:
    run_data_quality_checks = None


def _resolve_callable(candidates: list[tuple[str, str]]) -> Optional[Callable]:
    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                logger.info("[PUSH SUMMARY ENGINE] resolved %s -> %s.%s", func_name, module_name, func_name)
                return fn
            logger.warning("[PUSH SUMMARY ENGINE] candidate attribute missing: %s.%s", module_name, func_name)
        except Exception as e:
            logger.warning("[PUSH SUMMARY ENGINE] candidate import failed: %s.%s: %s: %s", module_name, func_name, type(e).__name__, e)
    return None


def _resolve_build_incremental_summary() -> Optional[Callable]:
    return _resolve_callable([
        ("trading.summary.pipeline.summary_pipeline", "run_summary_pipeline"),
        ("trading.summary.engine.summary_incremental_engine", "build_incremental_summary"),
        ("trading.summary.engine.incremental_summary_engine", "build_incremental_summary"),
        ("trading.summary.engine.summary_recovery_engine", "build_incremental_summary"),
    ])


def _safe_attr(obj: Any, name: str, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _safe_copy_df(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, tuple) and len(value) >= 1 and isinstance(value[0], pd.DataFrame):
        return value[0].copy()
    if isinstance(value, dict):
        for key in ("result_df", "merged_df", "df", "summary_df", "output_df", "display_df", "latest_df", "latest_summary_df"):
            v = value.get(key)
            if isinstance(v, pd.DataFrame):
                return v.copy()
    try:
        return pd.DataFrame(value).copy()
    except Exception:
        return pd.DataFrame()


def _safe_symbol_count(df: pd.DataFrame) -> int:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return 0
    try:
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _safe_latest_dt(df: pd.DataFrame):
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    for col in ("datetime", "end_time", "start_time", "snapshot_time", "received_at", "CurrentPriceTime", "current_price_time"):
        if col not in df.columns:
            continue
        try:
            s = pd.to_datetime(df[col], errors="coerce")
            if s.notna().any():
                ts = s.max()
                try:
                    ts = ts.tz_localize(None)
                except Exception:
                    pass
                return ts
        except Exception:
            pass
    return None


def _today_date():
    try:
        return pd.Timestamp.now().date()
    except Exception:
        return None


def _latest_date(df: pd.DataFrame):
    latest = _safe_latest_dt(df)
    try:
        return pd.Timestamp(latest).date() if latest is not None else None
    except Exception:
        return None


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = df.copy()
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    elif "date" in out.columns and "start_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["date"].astype(str).str.strip() + " " + out["start_time"].astype(str).str.strip(), errors="coerce")
    elif "date" in out.columns and "time" in out.columns:
        out["datetime"] = pd.to_datetime(out["date"].astype(str).str.strip() + " " + out["time"].astype(str).str.strip(), errors="coerce")
    elif "end_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["end_time"], errors="coerce")
    elif "snapshot_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["snapshot_time"], errors="coerce")
    elif "CurrentPriceTime" in out.columns:
        out["datetime"] = pd.to_datetime(out["CurrentPriceTime"], errors="coerce")
    elif "current_price_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["current_price_time"], errors="coerce")
    elif "received_at" in out.columns:
        out["datetime"] = pd.to_datetime(out["received_at"], errors="coerce")
    else:
        out["datetime"] = pd.NaT
    try:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.tz_localize(None)
    except Exception:
        pass
    return out


def _ensure_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = df.copy()
    if "symbol" not in out.columns:
        for c in ("Symbol", "symbol_code", "Code", "code"):
            if c in out.columns:
                out["symbol"] = out[c]
                break
    if "symbol" not in out.columns:
        out["symbol"] = ""
    out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    out = out[out["symbol"] != ""].copy()
    return out


def _coalesce_numeric_columns(df: pd.DataFrame, target: str, candidates: list[str]) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = df.copy()
    merged = None
    for c in candidates:
        if c not in out.columns:
            continue
        try:
            s = pd.to_numeric(out[c], errors="coerce")
            merged = s if merged is None else merged.combine_first(s)
        except Exception:
            pass
    if merged is not None:
        out[target] = merged
    return out


def _normalize_push_source_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_copy_df(df)
    if out.empty:
        return out
    out = _ensure_symbol(out)
    out = _ensure_datetime(out)
    out = _coalesce_numeric_columns(out, "close", ["close", "close_price", "price", "Price", "current_price", "CurrentPrice", "last_price", "LastPrice", "Close", "ClosePrice"])
    out = _coalesce_numeric_columns(out, "open", ["open", "open_price", "Open", "OpenPrice"])
    out = _coalesce_numeric_columns(out, "high", ["high", "high_price", "High", "HighPrice"])
    out = _coalesce_numeric_columns(out, "low", ["low", "low_price", "Low", "LowPrice"])
    if "close" in out.columns:
        for c in ("open", "high", "low"):
            if c not in out.columns:
                out[c] = out["close"]
            else:
                try:
                    out[c] = pd.to_numeric(out[c], errors="coerce").combine_first(pd.to_numeric(out["close"], errors="coerce"))
                except Exception:
                    pass
        if "close_price" not in out.columns:
            out["close_price"] = out["close"]
        if "price" not in out.columns:
            out["price"] = out["close"]
        if "current_price" not in out.columns:
            out["current_price"] = out["close"]
    if "open" in out.columns and "open_price" not in out.columns:
        out["open_price"] = out["open"]
    if "high" in out.columns and "high_price" not in out.columns:
        out["high_price"] = out["high"]
    if "low" in out.columns and "low_price" not in out.columns:
        out["low_price"] = out["low"]
    out = _coalesce_numeric_columns(out, "volume", ["volume", "Volume", "TradingVolume", "trading_volume", "CumVolume", "cum_volume", "last_cum_volume"])
    if "symbolname" not in out.columns:
        for c in ("SymbolName", "symbol_name", "name", "Name"):
            if c in out.columns:
                out["symbolname"] = out[c]
                break
    if "symbolname" not in out.columns and "symbol" in out.columns:
        out["symbolname"] = out["symbol"]
    if "source" not in out.columns:
        out["source"] = "push_stream"
    out = out.dropna(subset=["symbol"], how="any")
    if "datetime" in out.columns:
        out = out.dropna(subset=["datetime"], how="all")
    logger.info("[PUSH SUMMARY ENGINE] normalized push source rows=%s cols=%s symbols=%s latest_dt=%s", len(out), len(out.columns), _safe_symbol_count(out), _safe_latest_dt(out))
    return out.reset_index(drop=True)


def _normalize_summary_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_copy_df(df)
    if out.empty:
        return out
    out = _ensure_symbol(_ensure_datetime(out))
    return out.reset_index(drop=True)


def _filter_same_day(df: pd.DataFrame, *, ref_df: pd.DataFrame | None = None, today_only: bool = True) -> pd.DataFrame:
    out = _normalize_summary_df(df)
    if out.empty or "datetime" not in out.columns:
        return out
    ref_date = _latest_date(ref_df) if isinstance(ref_df, pd.DataFrame) and not ref_df.empty else None
    if ref_date is None and today_only:
        ref_date = _today_date()
    if ref_date is None:
        return out
    try:
        s = pd.to_datetime(out["datetime"], errors="coerce")
        before = len(out)
        out = out[s.dt.date == ref_date].copy()
        if before and len(out) != before:
            logger.warning("[PUSH SUMMARY ENGINE] filtered stale summary rows before=%s after=%s date=%s", before, len(out), ref_date)
    except Exception:
        logger.exception("[PUSH SUMMARY ENGINE] same-day filter failed")
    return out.reset_index(drop=True)


def _trim_recent_per_symbol(df: pd.DataFrame, bars: int = 120) -> pd.DataFrame:
    out = _normalize_summary_df(df)
    if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
        return out
    try:
        before = len(out)
        out = out.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", group_keys=False).tail(int(bars)).reset_index(drop=True)
        if before != len(out):
            logger.warning("[PUSH SUMMARY ENGINE] trimmed summary rows before=%s after=%s bars=%s", before, len(out), bars)
    except Exception:
        logger.exception("[PUSH SUMMARY ENGINE] trim recent failed")
    return out


def _log_df_stats(label: str, df: pd.DataFrame) -> None:
    rows = len(df) if isinstance(df, pd.DataFrame) else 0
    cols = len(df.columns) if isinstance(df, pd.DataFrame) else 0
    symbols = _safe_symbol_count(df)
    latest_dt = _safe_latest_dt(df)
    logger.info("[PUSH SUMMARY STATS] %s | rows=%d cols=%d symbols=%d latest_dt=%s", label, rows, cols, symbols, latest_dt)


def _get_from_global_data(candidates: list[str]) -> Any:
    if global_data is None:
        return None
    for name in candidates:
        try:
            if hasattr(global_data, name):
                value = getattr(global_data, name)
                if value is not None:
                    logger.info("[PUSH SUMMARY ENGINE] global_data hit key=%s type=%s", name, type(value).__name__)
                    return value
        except Exception:
            pass
    logger.info("[PUSH SUMMARY ENGINE] global_data miss keys=%s", candidates)
    return None


def _resolve_push_source_df() -> pd.DataFrame:
    df = _get_from_global_data(["stream_data", "push_df", "push_data", "latest_push_df", "push_snapshot_df"])
    if (not isinstance(df, pd.DataFrame)) or df.empty:
        getter = _safe_attr(global_data, "get_push_df", None)
        if callable(getter):
            try:
                df = getter()
                logger.info("[PUSH SUMMARY ENGINE] get_push_df() used")
            except Exception:
                logger.exception("[PUSH SUMMARY ENGINE] get_push_df() failed")
    out = _normalize_push_source_df(df)
    out = _filter_same_day(out, today_only=True)
    _log_df_stats("resolved push source", out)
    return out


def _resolve_summary_source_df(interval: int, *, push_df: pd.DataFrame) -> pd.DataFrame:
    tf = int(interval)
    df = _get_from_global_data([f"push_summary_{tf}min", f"push_summary_{tf}", f"summary_{tf}min_df", f"summary_df_{tf}min", f"latest_summary_{tf}min", f"push_merged_summary_{tf}min", f"push_merged_summary_{tf}"])
    if (not isinstance(df, pd.DataFrame)) or df.empty:
        getter = _safe_attr(global_data, "get_push_summary", None)
        if callable(getter):
            try:
                df = getter(tf)
                logger.info("[PUSH SUMMARY ENGINE] get_push_summary(tf=%s) used", tf)
            except Exception:
                logger.exception("[PUSH SUMMARY ENGINE] get_push_summary(tf=%s) failed", tf)
    if (not isinstance(df, pd.DataFrame)) or df.empty:
        getter = _safe_attr(global_data, "get_merged_summary", None)
        if callable(getter):
            try:
                df = getter(tf, source="push")
                logger.info("[PUSH SUMMARY ENGINE] get_merged_summary(tf=%s, source=push) used", tf)
            except TypeError:
                try:
                    df = getter(tf)
                    logger.info("[PUSH SUMMARY ENGINE] get_merged_summary(tf=%s) used as push fallback", tf)
                except Exception:
                    pass
            except Exception:
                pass
    out = _normalize_summary_df(df)
    out = _filter_same_day(out, ref_df=push_df, today_only=True)
    out = _trim_recent_per_symbol(out, bars=120)
    _log_df_stats(f"resolved push summary source interval={tf}", out)
    return out


def _is_latest_only_frame(df: pd.DataFrame) -> tuple[bool, float, int, int]:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return False, 0.0, 0, 0
    try:
        out = _ensure_symbol(_ensure_datetime(df))
        total = len(out)
        if total == 0 or "datetime" not in out.columns:
            return False, 0.0, 0, total
        s = pd.to_datetime(out["datetime"], errors="coerce")
        if s.notna().sum() == 0:
            return False, 0.0, 0, total
        latest = s.max()
        one_bar = int((s == latest).sum())
        ratio = float(one_bar / total) if total > 0 else 0.0
        return ratio >= 0.98, ratio, one_bar, total
    except Exception:
        logger.exception("[PUSH SUMMARY ENGINE] latest-only probe failed")
        return False, 0.0, 0, 0


def _maturity_profile(df: pd.DataFrame) -> dict:
    prof = {"rows": 0, "symbols": 0, "technical_ready_rows": 0, "hist_ge3": 0, "hist_max": 0.0, "score_nonzero": 0, "score_buy_nonzero": 0, "score_sell_nonzero": 0}
    if not isinstance(df, pd.DataFrame) or df.empty:
        return prof
    try:
        prof["rows"] = len(df)
        prof["symbols"] = _safe_symbol_count(df)
        if "technical_ready" in df.columns:
            prof["technical_ready_rows"] = int(pd.Series(df["technical_ready"]).fillna(False).astype(bool).sum())
        if "symbol_hist_len" in df.columns:
            hist = pd.to_numeric(df["symbol_hist_len"], errors="coerce")
            prof["hist_ge3"] = int((hist >= 3).sum())
            prof["hist_max"] = float(hist.max()) if hist.notna().any() else 0.0
        for col, key in (("score", "score_nonzero"), ("score_buy", "score_buy_nonzero"), ("score_sell", "score_sell_nonzero")):
            if col in df.columns:
                s = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
                prof[key] = int((s != 0).sum())
    except Exception:
        logger.exception("[PUSH SUMMARY ENGINE] maturity profile failed")
    return prof


def _looks_immature(df: pd.DataFrame) -> tuple[bool, dict]:
    prof = _maturity_profile(df)
    immature = prof["rows"] > 0 and ((prof["technical_ready_rows"] == 0 and prof["hist_ge3"] == 0 and prof["hist_max"] <= 2.0) or (prof["score_nonzero"] == 0 and prof["score_buy_nonzero"] == 0 and prof["score_sell_nonzero"] == 0))
    return immature, prof


def _calc_rsi_direct(close: pd.Series, period: int = 14) -> pd.Series:
    try:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        rs = avg_gain / avg_loss.replace(0, pd.NA)
        return pd.to_numeric(100 - (100 / (1 + rs)), errors="coerce").fillna(50.0)
    except Exception:
        return pd.Series([50.0] * len(close), index=close.index)


def _build_direct_ohlc_from_push_source(push_df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    ticks = _normalize_push_source_df(push_df)
    if ticks.empty:
        logger.warning("[PUSH SUMMARY ENGINE][DIRECT OHLC] normalized ticks empty interval=%s", interval)
        return pd.DataFrame()
    required = {"datetime", "symbol", "close"}
    if not required.issubset(set(ticks.columns)):
        logger.warning("[PUSH SUMMARY ENGINE][DIRECT OHLC] required cols missing interval=%s cols=%s", interval, list(ticks.columns))
        return pd.DataFrame()
    ticks = ticks.dropna(subset=["datetime", "symbol", "close"]).copy()
    if ticks.empty:
        return pd.DataFrame()
    ticks["datetime"] = pd.to_datetime(ticks["datetime"], errors="coerce")
    try:
        ticks["datetime"] = ticks["datetime"].dt.tz_localize(None)
    except Exception:
        pass
    freq = f"{int(interval)}min"
    ticks["_slot"] = ticks["datetime"].dt.floor(freq)
    ticks = ticks.sort_values(["symbol", "datetime"], kind="stable")

    def _last_nonempty(s: pd.Series):
        try:
            x = s.dropna()
            return x.iloc[-1] if not x.empty else ""
        except Exception:
            return ""

    bars = ticks.groupby(["symbol", "_slot"], as_index=False).agg(
        symbolname=("symbolname", _last_nonempty),
        open=("close", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "max"),
        tick_count=("close", "count"),
        first_tick_at=("datetime", "min"),
        last_tick_at=("datetime", "max"),
    ).rename(columns={"_slot": "datetime"})
    for c in ("open", "high", "low", "close", "volume"):
        if c in bars.columns:
            bars[c] = pd.to_numeric(bars[c], errors="coerce")
    bars = bars.dropna(subset=["symbol", "datetime", "close"]).copy()
    if bars.empty:
        return pd.DataFrame()
    parts = []
    for _sym, one in bars.groupby("symbol", sort=False):
        one = one.copy().sort_values("datetime", kind="stable")
        close = pd.to_numeric(one["close"], errors="coerce")
        prev_close = close.shift(1)
        pct = (close - prev_close) / prev_close.replace(0, pd.NA)
        intrabar = (close - pd.to_numeric(one["open"], errors="coerce")) / pd.to_numeric(one["open"], errors="coerce").replace(0, pd.NA)
        rng = (pd.to_numeric(one["high"], errors="coerce") - pd.to_numeric(one["low"], errors="coerce")) / close.replace(0, pd.NA)
        one["rsi"] = _calc_rsi_direct(close)
        ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
        one["macd"] = (ema12 - ema26).fillna(0.0)
        one["signal"] = one["macd"].ewm(span=9, adjust=False, min_periods=1).mean().fillna(0.0)
        one["hist"] = (one["macd"] - one["signal"]).fillna(0.0)
        one["slope"] = pct.combine_first(intrabar).fillna(0.0)
        one["slope_atr_scaled"] = one["slope"]
        one["mtf"] = 0.0
        one["score_slope"] = pd.to_numeric(one["slope"], errors="coerce").fillna(0.0) * 100.0
        one["score_mtf"] = 0.0
        base = pct.combine_first(intrabar).combine_first(rng).fillna(0.0) * 100.0
        tick_bonus = pd.to_numeric(one.get("tick_count", 1), errors="coerce") if "tick_count" in one.columns else pd.Series([1] * len(one), index=one.index)
        tick_bonus = pd.Series(tick_bonus, index=one.index).fillna(1).clip(lower=1) * 0.0001
        base = base.where(base.abs() > 0, tick_bonus)
        one["score"] = base.fillna(0.0001)
        one["score_total"] = one["score"] + one["score_slope"].fillna(0.0) + one["score_mtf"].fillna(0.0)
        one["final_score"] = one["score_total"]
        one["display_score"] = one["score_total"]
        one["score_buy"] = one["score_total"].clip(lower=0)
        one["score_sell"] = (-one["score_total"]).clip(lower=0)
        one["technical_ready"] = True
        one["symbol_hist_len"] = range(1, len(one) + 1)
        one["source"] = "push_stream_direct_ohlc_fallback"
        one["interval"] = int(interval)
        parts.append(one)
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    logger.warning("[PUSH SUMMARY ENGINE][DIRECT OHLC] built fallback interval=%s rows=%s symbols=%s latest_dt=%s", interval, len(out), _safe_symbol_count(out), _safe_latest_dt(out))
    return out.reset_index(drop=True)


def _call_summary_fn(fn: Callable, *, interval: int, label: str) -> pd.DataFrame:
    for call in (lambda: fn(interval=interval), lambda: fn(interval), lambda: fn()):
        try:
            return _safe_copy_df(call())
        except TypeError:
            continue
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] %s failed", label)
            return pd.DataFrame()
    return pd.DataFrame()


def _run_push_summary(interval: int, *, summary_df: pd.DataFrame, push_df: pd.DataFrame) -> pd.DataFrame:
    fn = _resolve_build_incremental_summary()
    if fn is None:
        logger.warning("[PUSH SUMMARY ENGINE] build_incremental_summary unavailable")
        return pd.DataFrame()
    module_name = getattr(fn, "__module__", "")
    func_name = getattr(fn, "__name__", "")
    if module_name == "trading.summary.pipeline.summary_pipeline" and func_name == "run_summary_pipeline":
        try:
            out = fn(summary_df=summary_df, push_df=push_df, interval=interval, evaluate_signals=True, latest_only=False, recent_bars_per_symbol=120)
            out = _safe_copy_df(out)
            latest_only, ratio, one_bar, total = _is_latest_only_frame(out)
            immature, prof = _looks_immature(out)
            logger.info("[PUSH SUMMARY ENGINE] pipeline interval=%s summary_rows=%s push_rows=%s out_rows=%s latest_only=%s ratio=%.4f one_bar=%s total=%s immature=%s hist_max=%s ready_rows=%s score_nonzero=%s", interval, len(summary_df), len(push_df), len(out), latest_only, ratio, one_bar, total, immature, prof.get("hist_max", 0.0), prof.get("technical_ready_rows", 0), prof.get("score_nonzero", 0))
            if out.empty and not push_df.empty:
                logger.warning("[PUSH SUMMARY ENGINE] pipeline returned empty interval=%s push_rows=%s -> direct OHLC fallback", interval, len(push_df))
                out = _build_direct_ohlc_from_push_source(push_df, interval=interval)
            logger.info("[PUSH SUMMARY TRACE] interval=%s runner_result_rows=%s latest_dt=%s", interval, len(out), _safe_latest_dt(out))
            return out
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] run_summary_pipeline failed interval=%s", interval)
            return pd.DataFrame()
    df = _call_summary_fn(fn, interval=interval, label="push summary")
    logger.info("[PUSH SUMMARY TRACE] interval=%s runner_result_rows=%s latest_dt=%s", interval, len(df), _safe_latest_dt(df))
    return df


def _normalize_for_source(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
    out = _normalize_summary_df(df)
    if out.empty:
        return pd.DataFrame()
    if "source" not in out.columns:
        out["source"] = source_name
    else:
        out["source"] = out["source"].fillna(source_name).astype(str)
    if "datetime" not in out.columns:
        out["datetime"] = pd.NaT
    out = out[out["symbol"] != ""].copy()
    return out.reset_index(drop=True)


def _run_quality_engine(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    if run_data_quality_checks is None:
        logger.info("[PUSH SUMMARY ENGINE] data quality skipped (engine unavailable)")
        return df
    try:
        out = run_data_quality_checks(df, interval=interval)
        return out if isinstance(out, pd.DataFrame) else df
    except TypeError:
        try:
            out = run_data_quality_checks(df)
            return out if isinstance(out, pd.DataFrame) else df
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] data quality failed (legacy)")
            return df
    except Exception:
        logger.exception("[PUSH SUMMARY ENGINE] data quality failed")
        return df


def _should_store_push_summary(df: pd.DataFrame, interval: int) -> bool:
    if not isinstance(df, pd.DataFrame) or df.empty:
        logger.warning("[PUSH SUMMARY ENGINE] store skipped: empty interval=%s", interval)
        return False
    latest_only, ratio, one_bar, total = _is_latest_only_frame(df)
    immature, prof = _looks_immature(df)
    logger.info("[PUSH SUMMARY ENGINE] store probe interval=%s latest_only=%s ratio=%.4f one_bar=%s total=%s immature=%s rows=%s symbols=%s ready_rows=%s hist_ge3=%s hist_max=%s score_nonzero=%s", interval, latest_only, ratio, one_bar, total, immature, prof.get("rows", 0), prof.get("symbols", 0), prof.get("technical_ready_rows", 0), prof.get("hist_ge3", 0), prof.get("hist_max", 0.0), prof.get("score_nonzero", 0))
    if latest_only and total <= max(3, prof.get("symbols", 0)):
        return False
    if immature and prof.get("rows", 0) <= max(5, prof.get("symbols", 0) + 2):
        return False
    return True


def _store_push_merged_summary(interval: int, df: pd.DataFrame) -> None:
    if global_data is None or not isinstance(df, pd.DataFrame):
        return
    setter = _safe_attr(global_data, "set_merged_summary", None)
    if callable(setter):
        try:
            try:
                setter(interval, df.copy(), source="push")
            except TypeError:
                setter(interval, df.copy())
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] set_merged_summary failed interval=%s", interval)
    for name in (f"push_merged_summary_{interval}min", f"push_merged_summary_{interval}", f"merged_summary_{interval}min", f"merged_summary_{interval}", "merged_summary"):
        try:
            setattr(global_data, name, df.copy())
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] store failed key=%s interval=%s", name, interval)


def _store_push_frame(interval: int, push_df: pd.DataFrame) -> None:
    if global_data is None:
        return
    for name in (f"push_summary_{interval}min", f"push_summary_{interval}"):
        try:
            setattr(global_data, name, push_df.copy())
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] store failed key=%s interval=%s", name, interval)
    setter = _safe_attr(global_data, "set_push_summary", None)
    if callable(setter):
        try:
            setter(interval, push_df.copy())
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] set_push_summary failed interval=%s", interval)


def build_summary(interval: int = 1) -> pd.DataFrame:
    """
    PUSH専用 summary engine。
    ranking へは絶対にフォールバックしない。

    Ver1.2.1:
    push_rows=0 のとき、前回/前日の push_merged_summary を再保存しない。
    これにより、前日巨大キャッシュを毎分 pipeline に渡して timeout する問題を防ぐ。
    """
    logger.info("🚀 push_summary_engine START interval=%s", interval)
    push_source_df = _resolve_push_source_df()
    if push_source_df.empty:
        empty = pd.DataFrame()
        _store_push_frame(interval=interval, push_df=empty)
        logger.warning("[PUSH SUMMARY ENGINE] push source empty -> no previous merged reuse interval=%s", interval)
        logger.info("✅ push_summary_engine END interval=%s push_rows=0 stored_new_push=False latest_dt=None", interval)
        return empty
    summary_df = _resolve_summary_source_df(interval, push_df=push_source_df)
    push_df = _run_push_summary(interval=interval, summary_df=summary_df, push_df=push_source_df)
    push_df = _normalize_for_source(push_df, "push")
    push_df = _filter_same_day(push_df, ref_df=push_source_df, today_only=True)
    push_df = _run_quality_engine(push_df, interval=interval)
    _log_df_stats("push summary", push_df)
    _store_push_frame(interval=interval, push_df=push_df)
    push_rows = len(push_df) if isinstance(push_df, pd.DataFrame) else 0
    stored_new_push = False
    if push_rows > 0 and _should_store_push_summary(push_df, interval=interval):
        _store_push_merged_summary(interval=interval, df=push_df)
        stored_new_push = True
    elif push_rows > 0:
        logger.warning("[PUSH SUMMARY ENGINE] overwrite skipped by maturity/latest-only guard interval=%s push_rows=%s", interval, push_rows)
    else:
        logger.warning("[PUSH SUMMARY ENGINE] skip PUSH merged_summary overwrite because push_rows=0 interval=%s", interval)
    logger.info("✅ push_summary_engine END interval=%s push_rows=%d stored_new_push=%s latest_dt=%s", interval, push_rows, stored_new_push, _safe_latest_dt(push_df))
    return push_df if isinstance(push_df, pd.DataFrame) else pd.DataFrame()


def build_push_summary(interval: int = 1) -> pd.DataFrame:
    return build_summary(interval=interval)


def push_summary_engine(interval: int = 1) -> pd.DataFrame:
    return build_summary(interval=interval)


def run_push_summary_engine(interval: int = 1) -> pd.DataFrame:
    return build_summary(interval=interval)


def run_summary_engine(interval: int = 1) -> pd.DataFrame:
    return build_summary(interval=interval)


def run(interval: int = 1) -> pd.DataFrame:
    return build_summary(interval=interval)
