# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_direct_push_force_patch.py
# Version: V1-FORCE-MAIN-DIRECT-PUSH-1M
# ------------------------------------------------------------
# Purpose:
#   Force main.py 1m summary tick to avoid heavy runner paths.
#
#   Some later runtime patches can re-wrap runner_core.job_summary after
#   summary_main_1m_light_tick_patch is installed.  This patch re-applies a
#   direct in-memory push_df 1m summary wrapper and starts a short watcher so
#   the direct wrapper wins during startup.
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
VERSION = "V1-FORCE-MAIN-DIRECT-PUSH-1M"
_PATCHED = False
_WATCHER_STARTED = False
_AI_EXECUTOR: ThreadPoolExecutor | None = None
_AI_LOCK = threading.RLock()
_AI_RUNNING: set[str] = set()
_ORIGINAL_JOB_SUMMARY = None


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
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


def _executor() -> ThreadPoolExecutor:
    global _AI_EXECUTOR
    if _AI_EXECUTOR is None:
        _AI_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, _env_int("SUMMARY_FORCE_DIRECT_AI_WORKERS", 1)), thread_name_prefix="summary-force-direct-ai")
    return _AI_EXECUTOR


def _dt_key(now: Any) -> str:
    return now.strftime("%Y%m%d%H%M%S") if isinstance(now, dt.datetime) else str(now)


def _first(df: pd.DataFrame, names: list[str]) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def _dt_series(s: Any) -> Any:
    try:
        return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
    except Exception:
        try:
            return pd.to_datetime(s, errors="coerce").dt.tz_localize(None)
        except Exception:
            return s


def _num(df: pd.DataFrame, col: str | None, default: float = 0.0) -> pd.Series:
    if col and col in df.columns:
        return pd.to_numeric(df[col], errors="coerce").fillna(default)
    return pd.Series(default, index=df.index, dtype="float64")


def _get_push_df() -> pd.DataFrame:
    try:
        from global_state import global_data
        for name in ("push_df", "PUSH_DF", "latest_push_df"):
            obj = getattr(global_data, name, None)
            if isinstance(obj, pd.DataFrame) and not obj.empty:
                return obj.copy(deep=False)
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
        logger.debug("[SUMMARY FORCE DIRECT 1M] push_df lookup failed", exc_info=True)
    return pd.DataFrame()


def _build_direct(now: dt.datetime) -> pd.DataFrame:
    t0 = time.perf_counter()
    raw = _get_push_df()
    if raw is None or raw.empty:
        return pd.DataFrame()
    try:
        x = raw.copy(deep=False)
        symbol_col = _first(x, ["symbol", "Symbol", "code", "Code", "銘柄コード"])
        price_col = _first(x, ["price", "current_price", "close", "close_price", "CurrentPrice", "現在値"])
        dt_col = _first(x, ["datetime", "dt", "timestamp", "received_at", "time", "Time"])
        if not symbol_col or not price_col or not dt_col:
            logger.warning("[SUMMARY FORCE DIRECT 1M] missing cols symbol=%s price=%s datetime=%s cols=%s", symbol_col, price_col, dt_col, list(x.columns)[:80])
            return pd.DataFrame()
        x["symbol"] = x[symbol_col].astype(str).str.strip()
        x["datetime"] = _dt_series(x[dt_col])
        x["price"] = pd.to_numeric(x[price_col], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime", "price"])
        x = x[x["symbol"].ne("") & (x["price"] > 0)]
        if x.empty:
            return pd.DataFrame()
        cutoff = pd.Timestamp(now).tz_localize(None)
        lookback = max(3, _env_int("SUMMARY_FORCE_DIRECT_LOOKBACK_MIN", 20))
        x = x[(x["datetime"] <= cutoff + pd.Timedelta(seconds=59)) & (x["datetime"] >= cutoff - pd.Timedelta(minutes=lookback))]
        if x.empty:
            return pd.DataFrame()
        symname_col = _first(x, ["symbolname", "symbol_name", "name", "SymbolName", "銘柄名"])
        vol_col = _first(x, ["volume", "trading_volume", "latest_volume", "Volume", "出来高"])
        value_col = _first(x, ["trading_value", "turnover", "TradingValue", "売買代金"])
        x["symbolname"] = x[symname_col].astype(str) if symname_col else x["symbol"]
        x["volume_src"] = _num(x, vol_col, 0.0)
        x["trading_value_src"] = _num(x, value_col, 0.0)
        x["slot"] = x["datetime"].dt.floor("1min")
        x = x.sort_values(["symbol", "slot", "datetime"], kind="stable")
        g = x.groupby(["symbol", "slot"], sort=False)
        bars = pd.DataFrame({
            "symbol": g["symbol"].last(),
            "symbolname": g["symbolname"].last(),
            "datetime": g["slot"].last(),
            "open": g["price"].first(),
            "high": g["price"].max(),
            "low": g["price"].min(),
            "close": g["price"].last(),
            "tick_count": g["price"].size(),
            "first_tick_at": g["datetime"].first(),
            "last_tick_at": g["datetime"].last(),
            "volume_src": g["volume_src"].max(),
            "trading_value_src": g["trading_value_src"].max(),
        }).reset_index(drop=True)
        if bars.empty:
            return pd.DataFrame()
        bars = bars.sort_values(["symbol", "datetime"], kind="stable")
        sg = bars.groupby("symbol", group_keys=False)
        bars["volume"] = sg["volume_src"].diff().fillna(bars["volume_src"])
        bars.loc[(bars["volume"] < 0) | bars["volume"].isna(), "volume"] = bars["volume_src"]
        bars["trading_value"] = sg["trading_value_src"].diff().fillna(bars["trading_value_src"])
        bars.loc[(bars["trading_value"] < 0) | bars["trading_value"].isna(), "trading_value"] = bars["trading_value_src"]
        prev = sg["close"].shift(1)
        bars["ma5"] = sg["close"].transform(lambda s: s.rolling(5, min_periods=1).mean())
        bars["ma25"] = sg["close"].transform(lambda s: s.rolling(25, min_periods=1).mean())
        bars["ma75"] = sg["close"].transform(lambda s: s.rolling(75, min_periods=1).mean())
        bars["slope"] = ((bars["close"] - prev) / prev.replace(0, pd.NA)).fillna(0.0)
        bars["atr"] = (bars["high"] - bars["low"]).abs().replace(0, pd.NA).fillna((bars["close"] * 0.001).abs())
        bars["slope_atr_scaled"] = ((bars["close"] - prev).fillna(0.0) / bars["atr"].replace(0, pd.NA)).fillna(0.0) / 100.0
        bars["rsi"] = 50.0
        bars["macd"] = 0.0
        bars["signal"] = 0.0
        bars["hist"] = 0.0
        move = (bars["slope"] * 1000.0).clip(-5.0, 5.0).fillna(0.0)
        boost = (pd.to_numeric(bars["tick_count"], errors="coerce").fillna(0).clip(0, 20) / 20.0)
        bars["score_buy"] = (move.where(move > 0, 0.0) + boost.where(move > 0, 0.0)).fillna(0.0)
        bars["score_sell"] = ((-move).where(move < 0, 0.0) + boost.where(move < 0, 0.0)).fillna(0.0)
        bars["score_slope"] = move
        bars["score_mtf"] = 0.0
        bars["score"] = bars["score_buy"] - bars["score_sell"]
        bars["score_total"] = bars["score"]
        bars["final_score"] = bars["score"]
        bars["display_score"] = bars["score"]
        bars["technical_ready"] = True
        bars["symbol_hist_len"] = sg["close"].transform("count")
        bars["price"] = bars["close"]
        bars["current_price"] = bars["close"]
        bars["open_price"] = bars["open"]
        bars["high_price"] = bars["high"]
        bars["low_price"] = bars["low"]
        bars["close_price"] = bars["close"]
        bars["vwap"] = bars["close"]
        bars["interval"] = 1
        bars["source"] = "force_main_direct_push_df_1min"
        bars["date"] = bars["datetime"].dt.strftime("%Y-%m-%d")
        bars["time"] = bars["datetime"].dt.strftime("%H:%M:%S")
        bars["start_time"] = bars["time"]
        bars["end_time"] = bars["time"]
        bars["time_range"] = bars["time"]
        latest = bars.groupby("symbol", sort=False, as_index=False).tail(1).reset_index(drop=True)
        min_price = float(os.getenv("SUMMARY_FORCE_DIRECT_MIN_PRICE", os.getenv("ENTRY_MIN_PRICE", "200")) or 200)
        max_price = float(os.getenv("SUMMARY_FORCE_DIRECT_MAX_PRICE", os.getenv("ENTRY_MAX_PRICE", "7000")) or 7000)
        latest = latest[(pd.to_numeric(latest["close"], errors="coerce") > min_price) & (pd.to_numeric(latest["close"], errors="coerce") <= max_price)]
        logger.warning(
            "[SUMMARY FORCE DIRECT 1M] built rows=%s symbols=%s raw_rows=%s latest_dt=%s elapsed=%.3fs",
            len(latest), latest["symbol"].nunique() if not latest.empty else 0, len(raw), latest["datetime"].max() if not latest.empty else None, time.perf_counter() - t0,
        )
        return latest.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY FORCE DIRECT 1M] build failed")
        return pd.DataFrame()


def _store(df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    try:
        from global_state import global_data
        for name in ("push_summary_1", "push_summary_1min", "push_merged_summary_1", "merged_summary_1"):
            setattr(global_data, name, df)
    except Exception:
        pass
    try:
        from core.global_context.context import global_context as GC
        for fn_name in ("set_push_summary", "set_merged_summary"):
            fn = getattr(GC, fn_name, None)
            if callable(fn):
                try:
                    if fn_name == "set_merged_summary":
                        fn(1, df, source="push")
                    else:
                        fn(1, df)
                except TypeError:
                    try:
                        fn(1, df)
                    except Exception:
                        pass
    except Exception:
        pass


def _submit_ai(df: pd.DataFrame, now: dt.datetime, run_entry: bool) -> None:
    if not run_entry or df is None or df.empty or not _env_bool("SUMMARY_FORCE_DIRECT_ASYNC_AI", True):
        return
    key = "force-summary-ai:1:" + _dt_key(now)
    with _AI_LOCK:
        if key in _AI_RUNNING:
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI skipped already_running key=%s rows=%s", key, len(df))
            return
        _AI_RUNNING.add(key)
    df_copy = df.copy(deep=False)
    def _task() -> None:
        try:
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI start key=%s rows=%s", key, len(df_copy))
            try:
                from scheduler_jobs.summary.summary_ai_entry_hook_v20 import run_summary_ai_entry_safe
                run_summary_ai_entry_safe(interval=1, now=now, df=df_copy, source="SUMMARY")
            except Exception:
                logger.exception("[SUMMARY FORCE DIRECT 1M] async AI failed key=%s", key)
        finally:
            with _AI_LOCK:
                _AI_RUNNING.discard(key)
            logger.warning("[SUMMARY FORCE DIRECT 1M] async AI done key=%s", key)
    _executor().submit(_task)
    logger.warning("[SUMMARY FORCE DIRECT 1M] async AI submitted key=%s rows=%s", key, len(df_copy))


def _patch_once(reason: str = "install") -> bool:
    global _ORIGINAL_JOB_SUMMARY
    if not _is_main_py() or not _env_bool("SUMMARY_FORCE_DIRECT_PATCH_ENABLED", True):
        return False
    try:
        import scheduler_jobs.summary.runner_core as rc
        current = getattr(rc, "job_summary", None)
        if getattr(current, "_summary_force_direct_v1", False):
            return True
        if _ORIGINAL_JOB_SUMMARY is None and callable(current):
            _ORIGINAL_JOB_SUMMARY = current
        orig = _ORIGINAL_JOB_SUMMARY if callable(_ORIGINAL_JOB_SUMMARY) else current

        def job_summary_force(interval: int, display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True, **kwargs) -> pd.DataFrame:
            interval_i = int(interval)
            now_i = (now or rc.now_naive()).replace(microsecond=0)
            if interval_i != 1:
                return orig(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)
            t0 = time.perf_counter()
            df = _build_direct(now_i)
            if df is not None and not df.empty:
                _store(df)
                _submit_ai(df, now_i, run_entry)
                logger.warning("[SUMMARY FORCE DIRECT 1M] return interval=1 rows=%s elapsed=%.3fs mode=forced_direct", len(df), time.perf_counter() - t0)
                return df
            logger.warning("[SUMMARY FORCE DIRECT 1M] direct empty -> original fallback interval=1")
            return orig(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)

        job_summary_force._summary_force_direct_v1 = True  # type: ignore[attr-defined]
        job_summary_force._original = orig  # type: ignore[attr-defined]
        rc.job_summary = job_summary_force
        rc.run_push_summary_job = lambda interval=1, display=True, now=None, run_entry=True, **kwargs: job_summary_force(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)
        rc.job_1m = lambda display=True, now=None, run_entry=True: job_summary_force(1, display=display, now=now, run_entry=run_entry)
        logger.warning("[SUMMARY FORCE DIRECT 1M] patched reason=%s target=runner_core.job_summary version=%s", reason, VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY FORCE DIRECT 1M] patch failed reason=%s", reason)
        return False


def _watcher() -> None:
    deadline = time.time() + max(30, _env_int("SUMMARY_FORCE_DIRECT_WATCH_SEC", 180))
    i = 0
    while time.time() < deadline:
        try:
            _patch_once(reason=f"watcher:{i}")
        except Exception:
            logger.debug("[SUMMARY FORCE DIRECT 1M] watcher reapply failed", exc_info=True)
        i += 1
        time.sleep(max(0.5, float(os.getenv("SUMMARY_FORCE_DIRECT_WATCH_INTERVAL", "2.0"))))
    logger.warning("[SUMMARY FORCE DIRECT 1M] watcher done reapplies=%s", i)


def install() -> bool:
    global _PATCHED, _WATCHER_STARTED
    ok = _patch_once(reason="install")
    if ok and not _WATCHER_STARTED and _env_bool("SUMMARY_FORCE_DIRECT_WATCHER", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-force-direct-1m-watch", daemon=True).start()
        logger.warning("[SUMMARY FORCE DIRECT 1M] watcher started version=%s", VERSION)
    _PATCHED = bool(ok)
    logger.warning("[SUMMARY FORCE DIRECT 1M] installed version=%s ok=%s main=%s", VERSION, ok, _is_main_py())
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY FORCE DIRECT 1M] auto install failed")

__all__ = ["VERSION", "install"]
