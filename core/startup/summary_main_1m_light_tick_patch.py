# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_1m_light_tick_patch.py
# Version: V3-MAIN-1M-DIRECT-PUSH-DF-LIGHT-SUMMARY
# ------------------------------------------------------------
# Purpose:
#   Keep main.py summary_parent_tick responsive.
#
#   V2 still called the resolved push_summary_runner, which can resolve to
#   summary_mtf_diff_from_1m_patch._patched_diff_update and spend many seconds
#   in MTF/cache/display/controller paths.  In main.py, build the 1m summary
#   directly from in-memory global_data.push_df first, submit Summary-AI, then
#   return immediately.  Fall back to the original runner only when direct build
#   is unavailable/empty.
#
#   main_database.py remains the owner of DB persistence.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V3-MAIN-1M-DIRECT-PUSH-DF-LIGHT-SUMMARY"
_INSTALLED = False
_AI_EXECUTOR: ThreadPoolExecutor | None = None
_AI_LOCK = threading.RLock()
_AI_RUNNING_KEYS: set[str] = set()


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
            return False
        return "main.py" in argv
    except Exception:
        return False


def _is_entry_only_context() -> bool:
    try:
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return _is_main_py() or _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False) or role in {"entry_only", "main_entry_only", "read_only", "no_save"}
    except Exception:
        return _is_main_py()


def _executor() -> ThreadPoolExecutor:
    global _AI_EXECUTOR
    if _AI_EXECUTOR is None:
        workers = max(1, _env_int("SUMMARY_MAIN_ASYNC_AI_WORKERS", 1))
        _AI_EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="summary-main-ai-async")
    return _AI_EXECUTOR


def _dt_key(now: Any) -> str:
    try:
        if isinstance(now, dt.datetime):
            return now.strftime("%Y%m%d%H%M%S")
        return str(now)
    except Exception:
        return "unknown"


def _normalize_dt_naive_series(s: Any) -> Any:
    try:
        return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
    except Exception:
        try:
            return pd.to_datetime(s, errors="coerce").dt.tz_localize(None)
        except Exception:
            return s


def _first_existing(df: pd.DataFrame, names: list[str]) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    return None


def _num_series(df: pd.DataFrame, col: str | None, default: float = 0.0) -> pd.Series:
    try:
        if col and col in df.columns:
            return pd.to_numeric(df[col], errors="coerce").fillna(default)
        return pd.Series(default, index=df.index, dtype="float64")
    except Exception:
        return pd.Series(default, index=df.index if isinstance(df, pd.DataFrame) else None, dtype="float64")


def _get_global_push_df() -> pd.DataFrame:
    try:
        from global_state import global_data
        for name in ("push_df", "PUSH_DF", "latest_push_df"):
            obj = getattr(global_data, name, None)
            if isinstance(obj, pd.DataFrame) and not obj.empty:
                return obj.copy(deep=False)
        # Some implementations expose a getter.
        for name in ("get_push_df", "get_latest_push_df"):
            fn = getattr(global_data, name, None)
            if callable(fn):
                try:
                    obj = fn()
                    if isinstance(obj, pd.DataFrame) and not obj.empty:
                        return obj.copy(deep=False)
                except Exception:
                    pass
    except Exception:
        logger.debug("[SUMMARY MAIN LIGHT TICK] global_data push_df lookup failed", exc_info=True)
    return pd.DataFrame()


def _build_direct_1m_from_push_df(*, now: dt.datetime) -> pd.DataFrame:
    t0 = time.perf_counter()
    raw = _get_global_push_df()
    if raw is None or raw.empty:
        return pd.DataFrame()
    try:
        x = raw.copy(deep=False)
        symbol_col = _first_existing(x, ["symbol", "Symbol", "code", "Code", "銘柄コード"])
        price_col = _first_existing(x, ["price", "current_price", "close", "close_price", "CurrentPrice", "current", "現在値"])
        dt_col = _first_existing(x, ["datetime", "dt", "timestamp", "received_at", "time", "Time"])
        if symbol_col is None or price_col is None or dt_col is None:
            logger.warning(
                "[SUMMARY MAIN LIGHT TICK] direct push_df missing required cols symbol=%s price=%s datetime=%s cols=%s",
                symbol_col,
                price_col,
                dt_col,
                list(x.columns)[:80],
            )
            return pd.DataFrame()

        x["symbol"] = x[symbol_col].astype(str).str.strip()
        x = x[x["symbol"].ne("")]
        x["datetime"] = _normalize_dt_naive_series(x[dt_col])
        x["price"] = pd.to_numeric(x[price_col], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime", "price"])
        x = x[x["price"] > 0]
        if x.empty:
            return pd.DataFrame()

        cutoff = pd.Timestamp(now).tz_localize(None)
        lookback_min = max(3, _env_int("SUMMARY_MAIN_DIRECT_PUSH_LOOKBACK_MIN", 20))
        x = x[(x["datetime"] <= cutoff + pd.Timedelta(seconds=59)) & (x["datetime"] >= cutoff - pd.Timedelta(minutes=lookback_min))]
        if x.empty:
            return pd.DataFrame()

        symname_col = _first_existing(x, ["symbolname", "symbol_name", "name", "SymbolName", "銘柄名"])
        volume_col = _first_existing(x, ["volume", "trading_volume", "latest_volume", "Volume", "出来高"])
        value_col = _first_existing(x, ["trading_value", "turnover", "TradingValue", "売買代金"])
        high_col = _first_existing(x, ["high", "high_price", "HighPrice"])
        low_col = _first_existing(x, ["low", "low_price", "LowPrice"])
        open_col = _first_existing(x, ["open", "open_price", "OpeningPrice"])
        vwap_col = _first_existing(x, ["vwap", "VWAP"])

        x["slot"] = x["datetime"].dt.floor("1min")
        x["volume_src"] = _num_series(x, volume_col, 0.0)
        x["trading_value_src"] = _num_series(x, value_col, 0.0)
        x["high_src"] = _num_series(x, high_col, 0.0)
        x["low_src"] = _num_series(x, low_col, 0.0)
        x["open_src"] = _num_series(x, open_col, 0.0)
        x["vwap_src"] = _num_series(x, vwap_col, 0.0)
        if symname_col and symname_col in x.columns:
            x["symbolname"] = x[symname_col].astype(str)
        else:
            x["symbolname"] = x["symbol"]

        x = x.sort_values(["symbol", "slot", "datetime"], kind="stable")
        grouped = x.groupby(["symbol", "slot"], sort=False)
        bars = pd.DataFrame({
            "symbol": grouped["symbol"].last(),
            "symbolname": grouped["symbolname"].last(),
            "datetime": grouped["slot"].last(),
            "open": grouped["price"].first(),
            "high": grouped["price"].max(),
            "low": grouped["price"].min(),
            "close": grouped["price"].last(),
            "tick_count": grouped["price"].size(),
            "first_tick_at": grouped["datetime"].first(),
            "last_tick_at": grouped["datetime"].last(),
            "volume_src": grouped["volume_src"].max(),
            "trading_value_src": grouped["trading_value_src"].max(),
            "vwap_src": grouped["vwap_src"].last(),
        }).reset_index(drop=True)
        if bars.empty:
            return pd.DataFrame()

        bars = bars.sort_values(["symbol", "datetime"], kind="stable")
        # PUSH volume is often cumulative. Use diff when it looks cumulative;
        # otherwise keep raw max as one-minute volume.
        bars["volume"] = bars.groupby("symbol")["volume_src"].diff().fillna(bars["volume_src"])
        bars.loc[(bars["volume"] < 0) | bars["volume"].isna(), "volume"] = bars["volume_src"]
        bars["trading_value"] = bars.groupby("symbol")["trading_value_src"].diff().fillna(bars["trading_value_src"])
        bars.loc[(bars["trading_value"] < 0) | bars["trading_value"].isna(), "trading_value"] = bars["trading_value_src"]
        bars["price"] = bars["close"]
        bars["current_price"] = bars["close"]
        bars["open_price"] = bars["open"]
        bars["high_price"] = bars["high"]
        bars["low_price"] = bars["low"]
        bars["close_price"] = bars["close"]
        bars["vwap"] = bars["vwap_src"].where(pd.to_numeric(bars["vwap_src"], errors="coerce") > 0, bars["close"])

        g = bars.groupby("symbol", group_keys=False)
        bars["ma5"] = g["close"].transform(lambda s: s.rolling(5, min_periods=1).mean())
        bars["ma25"] = g["close"].transform(lambda s: s.rolling(25, min_periods=1).mean())
        bars["ma75"] = g["close"].transform(lambda s: s.rolling(75, min_periods=1).mean())
        prev_close = g["close"].shift(1)
        bars["slope"] = ((bars["close"] - prev_close) / prev_close.replace(0, pd.NA)).fillna(0.0)
        bars["atr"] = (bars["high"] - bars["low"]).abs().replace(0, pd.NA).fillna((bars["close"] * 0.001).abs())
        bars["slope_atr_scaled"] = ((bars["close"] - prev_close).fillna(0.0) / bars["atr"].replace(0, pd.NA)).fillna(0.0) / 100.0
        bars["rsi"] = 50.0
        bars["macd"] = 0.0
        bars["signal"] = 0.0
        bars["hist"] = 0.0

        # Lightweight score: keep sign/direction usable for AI candidates without
        # running the heavy scoring/display path.  Liquidity and detailed guards
        # still run later in entry pipeline.
        move_score = (bars["slope"] * 1000.0).clip(-5.0, 5.0).fillna(0.0)
        vol_boost = (pd.to_numeric(bars["tick_count"], errors="coerce").fillna(0.0).clip(0, 20) / 20.0)
        bars["score_buy"] = (move_score.where(move_score > 0, 0.0) + vol_boost.where(move_score > 0, 0.0)).fillna(0.0)
        bars["score_sell"] = ((-move_score).where(move_score < 0, 0.0) + vol_boost.where(move_score < 0, 0.0)).fillna(0.0)
        bars["score_slope"] = move_score
        bars["score_mtf"] = 0.0
        bars["score"] = bars["score_buy"] - bars["score_sell"]
        bars["score_total"] = bars["score"]
        bars["final_score"] = bars["score"]
        bars["display_score"] = bars["score"]
        bars["technical_ready"] = True
        bars["symbol_hist_len"] = g["close"].transform("count")
        bars["interval"] = 1
        bars["source"] = "main_direct_push_df_1min"
        bars["date"] = bars["datetime"].dt.strftime("%Y-%m-%d")
        bars["time"] = bars["datetime"].dt.strftime("%H:%M:%S")
        bars["start_time"] = bars["time"]
        bars["end_time"] = bars["time"]
        bars["time_range"] = bars["time"]

        # Return latest bar per symbol at or before now.
        latest = bars.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False, sort=False).tail(1)
        min_price = float(os.getenv("SUMMARY_MAIN_DIRECT_MIN_PRICE", os.getenv("ENTRY_MIN_PRICE", "200")) or 200)
        max_price = float(os.getenv("SUMMARY_MAIN_DIRECT_MAX_PRICE", os.getenv("ENTRY_MAX_PRICE", "7000")) or 7000)
        latest = latest[(pd.to_numeric(latest["close"], errors="coerce") > min_price) & (pd.to_numeric(latest["close"], errors="coerce") <= max_price)]
        cols = [
            "symbol", "datetime", "symbolname", "open", "high", "low", "close", "volume", "trading_value", "tick_count",
            "first_tick_at", "last_tick_at", "open_price", "high_price", "low_price", "close_price", "price", "current_price",
            "date", "time", "start_time", "end_time", "time_range", "ma5", "ma25", "ma75", "rsi", "macd", "signal", "hist",
            "atr", "slope", "slope_atr_scaled", "vwap", "score", "score_buy", "score_sell", "score_slope", "score_mtf",
            "score_total", "final_score", "display_score", "technical_ready", "symbol_hist_len", "interval", "source",
        ]
        latest = latest[[c for c in cols if c in latest.columns]].reset_index(drop=True)
        logger.warning(
            "[SUMMARY MAIN DIRECT 1M] built rows=%s symbols=%s raw_rows=%s raw_symbols=%s latest_dt=%s elapsed=%.3fs",
            len(latest),
            latest["symbol"].nunique() if "symbol" in latest.columns and not latest.empty else 0,
            len(raw),
            x["symbol"].nunique() if "symbol" in x.columns and not x.empty else 0,
            latest["datetime"].max() if "datetime" in latest.columns and not latest.empty else None,
            time.perf_counter() - t0,
        )
        return latest
    except Exception:
        logger.exception("[SUMMARY MAIN DIRECT 1M] build failed")
        return pd.DataFrame()


def _store_direct_summary(df: pd.DataFrame, *, interval: int) -> None:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return
    try:
        from core.global_context.context import global_context as GC
        for fn_name in ("set_push_summary", "set_merged_summary"):
            fn = getattr(GC, fn_name, None)
            if callable(fn):
                try:
                    if fn_name == "set_merged_summary":
                        fn(int(interval), df, source="push")
                    else:
                        fn(int(interval), df)
                except TypeError:
                    try:
                        fn(int(interval), df)
                    except Exception:
                        pass
    except Exception:
        logger.debug("[SUMMARY MAIN DIRECT 1M] global_context store failed", exc_info=True)
    try:
        from global_state import global_data
        setattr(global_data, "push_summary_1", df)
        setattr(global_data, "push_summary_1min", df)
        setattr(global_data, "push_merged_summary_1", df)
        setattr(global_data, "merged_summary_1", df)
    except Exception:
        logger.debug("[SUMMARY MAIN DIRECT 1M] global_data store failed", exc_info=True)


def _normalize_df_light(df: pd.DataFrame, *, interval: int, now: dt.datetime) -> pd.DataFrame:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        out = df.copy(deep=False)
        if "datetime" in out.columns:
            out["datetime"] = _normalize_dt_naive_series(out["datetime"])
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip()
            out = out[out["symbol"].ne("")]
        if "datetime" in out.columns:
            try:
                cutoff = pd.Timestamp(now).tz_localize(None)
                out = out[out["datetime"].isna() | (out["datetime"] <= cutoff)]
            except Exception:
                pass
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MAIN LIGHT TICK] light normalize failed interval=%s", interval)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _submit_async_ai(df: pd.DataFrame, *, interval: int, now: dt.datetime, run_entry: bool, reason: str) -> None:
    if not (_is_entry_only_context() and _env_bool("SUMMARY_MAIN_ASYNC_AI_ENTRY", True)):
        return
    if not (run_entry and int(interval) in (1, 3, 5)):
        logger.info(
            "[SUMMARY MAIN LIGHT TICK] async AI skipped interval=%s run_entry=%s reason=%s",
            interval,
            run_entry,
            "interval_not_enabled" if int(interval) not in (1, 3, 5) else "run_entry_false",
        )
        return
    rows = len(df) if isinstance(df, pd.DataFrame) else 0
    if rows <= 0:
        logger.warning("[SUMMARY MAIN LIGHT TICK] async AI skipped empty interval=%s reason=%s", interval, reason)
        return
    key = f"summary-ai:{int(interval)}:{_dt_key(now)}"
    with _AI_LOCK:
        if key in _AI_RUNNING_KEYS:
            logger.warning("[SUMMARY MAIN LIGHT TICK] async AI skipped already_running key=%s rows=%s reason=%s", key, rows, reason)
            return
        _AI_RUNNING_KEYS.add(key)

    df_copy = df.copy(deep=False) if isinstance(df, pd.DataFrame) else df

    def _task() -> None:
        try:
            logger.warning("[SUMMARY MAIN LIGHT TICK] async AI start key=%s interval=%s rows=%s now=%s reason=%s", key, interval, rows, now, reason)
            try:
                from scheduler_jobs.summary.summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
                run_summary_ai_entry_safe(interval=int(interval), now=now, df=df_copy, source="SUMMARY")
            except Exception:
                logger.exception("[SUMMARY MAIN LIGHT TICK] async AI failed key=%s interval=%s", key, interval)
        finally:
            with _AI_LOCK:
                _AI_RUNNING_KEYS.discard(key)
            logger.warning("[SUMMARY MAIN LIGHT TICK] async AI done key=%s interval=%s", key, interval)

    _executor().submit(_task)
    logger.warning("[SUMMARY MAIN LIGHT TICK] async AI submitted key=%s interval=%s rows=%s now=%s reason=%s", key, interval, rows, now, reason)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _is_entry_only_context():
        logger.warning("[SUMMARY MAIN LIGHT TICK] skipped non-main context version=%s", VERSION)
        return False

    try:
        os.environ.setdefault("SUMMARY_MAIN_ASYNC_AI_ENTRY", "1")
        os.environ.setdefault("SUMMARY_MAIN_SKIP_SYNC_SAVE", "1")
        os.environ.setdefault("SUMMARY_MAIN_ASYNC_AI_WORKERS", "1")
        os.environ.setdefault("SUMMARY_MAIN_EARLY_RETURN_AFTER_AI_SUBMIT", "1")
        os.environ.setdefault("SUMMARY_MAIN_DIRECT_PUSH_DF", "1")
        os.environ.setdefault("SUMMARY_MAIN_DIRECT_PUSH_LOOKBACK_MIN", "20")
        os.environ.setdefault("SUMMARY_MAIN_LIGHT_DISPLAY", "0")

        import scheduler_jobs.summary.runner_core as rc

        orig_job_summary = getattr(rc, "job_summary", None)
        orig_ai = getattr(rc, "_run_push_ai_entry_before_display", None)
        orig_save = getattr(rc, "_save_summary_if_owner", None)

        if callable(orig_save) and not getattr(orig_save, "_main_light_tick_wrapped", False):
            def _save_summary_if_owner_light(df: pd.DataFrame, interval: int, *, source: str) -> None:
                if _is_entry_only_context() and _env_bool("SUMMARY_MAIN_SKIP_SYNC_SAVE", True):
                    logger.warning(
                        "[SUMMARY MAIN LIGHT TICK] sync save skipped in main interval=%s source=%s rows=%s reason=database_owner_main_database",
                        interval,
                        source,
                        len(df) if isinstance(df, pd.DataFrame) else 0,
                    )
                    return None
                return orig_save(df, interval, source=source)

            _save_summary_if_owner_light._main_light_tick_wrapped = True  # type: ignore[attr-defined]
            _save_summary_if_owner_light._original = orig_save  # type: ignore[attr-defined]
            rc._save_summary_if_owner = _save_summary_if_owner_light

        if callable(orig_ai) and not getattr(orig_ai, "_main_light_tick_wrapped", False):
            def _run_push_ai_entry_before_display_light(df: pd.DataFrame, interval: int, now: dt.datetime, run_entry: bool) -> None:
                _submit_async_ai(df, interval=int(interval), now=now, run_entry=run_entry, reason="original_ai_hook_async")
                return None

            _run_push_ai_entry_before_display_light._main_light_tick_wrapped = True  # type: ignore[attr-defined]
            _run_push_ai_entry_before_display_light._original = orig_ai  # type: ignore[attr-defined]
            rc._run_push_ai_entry_before_display = _run_push_ai_entry_before_display_light

        if callable(orig_job_summary) and not getattr(orig_job_summary, "_main_light_tick_job_wrapped_v3", False):
            def job_summary_light(interval: int, display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True, **kwargs) -> pd.DataFrame:
                if not (_is_entry_only_context() and _env_bool("SUMMARY_MAIN_EARLY_RETURN_AFTER_AI_SUBMIT", True)):
                    return orig_job_summary(interval, display=display, now=now, run_entry=run_entry, **kwargs)
                interval_i = int(interval)
                now_i = (now or rc.now_naive()).replace(microsecond=0)
                if interval_i != 1:
                    return orig_job_summary(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)
                t0 = time.perf_counter()
                try:
                    logger.warning(
                        "[SUMMARY MAIN LIGHT TICK] light job start interval=%s display=%s run_entry=%s now=%s extra_keys=%s direct_push_df=%s",
                        interval_i,
                        display,
                        run_entry,
                        now_i,
                        sorted(list(kwargs.keys())),
                        _env_bool("SUMMARY_MAIN_DIRECT_PUSH_DF", True),
                    )
                    df = pd.DataFrame()
                    meta: dict[str, Any] = {"source": "none"}

                    if _env_bool("SUMMARY_MAIN_DIRECT_PUSH_DF", True):
                        df = _build_direct_1m_from_push_df(now=now_i)
                        if not df.empty:
                            meta = {"source": "main_direct_push_df", "direct": True}
                            _store_direct_summary(df, interval=interval_i)
                            try:
                                rc.log_job_result("job_summary(PUSH-DIRECT-LIGHT)", interval_i, df, meta)
                            except Exception:
                                pass
                            _submit_async_ai(df, interval=interval_i, now=now_i, run_entry=run_entry, reason="direct_push_df")
                            logger.warning(
                                "[SUMMARY MAIN LIGHT TICK] light job return interval=%s rows=%s elapsed=%.3fs mode=direct_push_df display_skipped=%s",
                                interval_i,
                                len(df),
                                time.perf_counter() - t0,
                                not _env_bool("SUMMARY_MAIN_LIGHT_DISPLAY", False),
                            )
                            return df
                        logger.warning("[SUMMARY MAIN LIGHT TICK] direct push_df empty interval=%s -> runner fallback", interval_i)

                    runner = rc.resolve_push_summary_runner()
                    if not callable(runner):
                        raise RuntimeError("push summary runner is not available")
                    result = rc.call_runner_with_optional_now(runner, interval=interval_i, now=now_i, **kwargs)
                    df, meta = rc.normalize_runner_output(result)
                    if not isinstance(df, pd.DataFrame) or df.empty:
                        logger.warning("[SUMMARY MAIN LIGHT TICK] light runner empty interval=%s -> fallback", interval_i)
                        df = rc.fallback_push_summary_df(interval_i, now=now_i)
                    df = _normalize_df_light(df, interval=interval_i, now=now_i)
                    if not df.empty:
                        try:
                            rc.log_job_result("job_summary(PUSH-LIGHT)", interval_i, df, meta if isinstance(meta, dict) else {})
                        except Exception:
                            pass
                        _submit_async_ai(df, interval=interval_i, now=now_i, run_entry=run_entry, reason="early_after_runner")
                    else:
                        logger.warning("[SUMMARY MAIN LIGHT TICK] light job empty after normalize interval=%s", interval_i)
                    logger.warning(
                        "[SUMMARY MAIN LIGHT TICK] light job return interval=%s rows=%s elapsed=%.3fs mode=runner_fallback display_skipped=%s",
                        interval_i,
                        len(df) if isinstance(df, pd.DataFrame) else 0,
                        time.perf_counter() - t0,
                        not _env_bool("SUMMARY_MAIN_LIGHT_DISPLAY", False),
                    )
                    if _env_bool("SUMMARY_MAIN_LIGHT_DISPLAY", False):
                        try:
                            rc._display_push_sync_or_async(df, interval_i, now_i, display)
                        except Exception:
                            logger.exception("[SUMMARY MAIN LIGHT TICK] optional display submit failed interval=%s", interval_i)
                    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
                except Exception:
                    logger.exception("[SUMMARY MAIN LIGHT TICK] light job failed interval=%s -> original fallback", interval_i)
                    return orig_job_summary(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)

            job_summary_light._main_light_tick_job_wrapped = True  # type: ignore[attr-defined]
            job_summary_light._main_light_tick_job_wrapped_v3 = True  # type: ignore[attr-defined]
            job_summary_light._original = orig_job_summary  # type: ignore[attr-defined]
            rc.job_summary = job_summary_light
            try:
                rc.run_push_summary_job = lambda interval=1, display=True, now=None, run_entry=True, **kwargs: job_summary_light(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)
                rc.job_1m = lambda display=True, now=None, run_entry=True: job_summary_light(1, display=display, now=now, run_entry=run_entry)
            except Exception:
                pass

        _INSTALLED = True
        logger.warning(
            "[SUMMARY MAIN LIGHT TICK] installed version=%s early_return=%s direct_push_df=%s async_ai=%s skip_sync_save=%s main=%s",
            VERSION,
            os.getenv("SUMMARY_MAIN_EARLY_RETURN_AFTER_AI_SUBMIT"),
            os.getenv("SUMMARY_MAIN_DIRECT_PUSH_DF"),
            os.getenv("SUMMARY_MAIN_ASYNC_AI_ENTRY"),
            os.getenv("SUMMARY_MAIN_SKIP_SYNC_SAVE"),
            _is_main_py(),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY MAIN LIGHT TICK] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN LIGHT TICK] auto install failed")

__all__ = ["VERSION", "install"]
