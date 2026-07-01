# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_memory_latest_1m_patch.py
# Purpose:
#   - main.py は PUSH DB 保存をしない前提のまま、PUSHメモリDFから
#     最新1分足 summary を高速生成する。
#   - 既存 runner がDB履歴/補完/表示/enrichで重くなり、latest_dt が
#     09:30等で止まるケースを避ける。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import time
from functools import wraps
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1-MAIN-MEMORY-LATEST-1M"
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return int(float(str(v).replace(",", "").strip()))
    except Exception:
        pass
    return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return float(str(v).replace(",", "").strip())
    except Exception:
        pass
    return float(default)


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in (sys.argv or []))
    except Exception:
        return ""


def _is_main_py() -> bool:
    argv = _argv_text()
    if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
        return False
    return "main.py" in argv


def _is_entry_only_context() -> bool:
    try:
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return _is_main_py() or _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False) or role in {"entry_only", "main_entry_only", "read_only", "no_save"}
    except Exception:
        return _is_main_py()


def _global_data() -> Any:
    try:
        from global_state import global_data
        return global_data
    except Exception:
        try:
            from core.global_context.context import global_data  # type: ignore
            return global_data
        except Exception:
            return None


def _as_df(x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    if x is None:
        return pd.DataFrame()
    try:
        return pd.DataFrame(x)
    except Exception:
        return pd.DataFrame()


def _first_existing(df: pd.DataFrame, names: tuple[str, ...]) -> Optional[str]:
    cols = {str(c).lower(): c for c in df.columns}
    for name in names:
        if name in df.columns:
            return name
        lc = name.lower()
        if lc in cols:
            return cols[lc]
    return None


def _to_naive_datetime(s: Any) -> pd.Series:
    try:
        out = pd.to_datetime(s, errors="coerce")
        try:
            if getattr(out.dt, "tz", None) is not None:
                out = out.dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)
        except Exception:
            try:
                out = out.dt.tz_localize(None)
            except Exception:
                pass
        return out
    except Exception:
        return pd.Series(pd.NaT, index=getattr(s, "index", None))


def _load_push_memory_df() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []

    gd = _global_data()
    if gd is not None:
        for name in ("push_df", "stream_data", "latest_push_df", "push_data", "push_snapshot_df"):
            try:
                x = getattr(gd, name, None)
                if isinstance(x, pd.DataFrame) and not x.empty:
                    frames.append(x)
            except Exception:
                pass
        try:
            fn = getattr(gd, "get_push_df", None)
            if callable(fn):
                x = fn()
                if isinstance(x, pd.DataFrame) and not x.empty:
                    frames.append(x)
        except Exception:
            pass

    try:
        from trading.push.push_stream import get_push_dataframe
        x = get_push_dataframe()
        if isinstance(x, pd.DataFrame) and not x.empty:
            frames.append(x)
    except Exception:
        pass

    if not frames:
        return pd.DataFrame()

    try:
        out = pd.concat([_as_df(x) for x in frames if isinstance(x, pd.DataFrame) and not x.empty], ignore_index=True, sort=False)
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MAIN MEMORY 1M] concat push memory df failed")
        return frames[-1].copy() if frames else pd.DataFrame()


def _normalize_push_ticks(df: pd.DataFrame, *, now: dt.datetime) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]

    sym_col = _first_existing(out, ("symbol", "Symbol", "code", "Code", "symbol_code", "銘柄コード"))
    price_col = _first_existing(out, ("current_price", "CurrentPrice", "price", "Price", "close", "close_price", "Close"))
    recv_col = _first_existing(out, ("received_at", "ReceivedAt", "inserted_at", "created_at"))
    event_col = _first_existing(out, ("datetime", "time", "current_price_time", "CurrentPriceTime", "timestamp", "PriceTime"))
    vol_col = _first_existing(out, ("trading_volume", "TradingVolume", "volume", "Volume"))
    val_col = _first_existing(out, ("trading_value", "TradingValue", "turnover", "Value"))
    name_col = _first_existing(out, ("symbolname", "SymbolName", "name", "Name", "銘柄名"))
    high_col = _first_existing(out, ("high_price", "HighPrice", "high"))
    low_col = _first_existing(out, ("low_price", "LowPrice", "low"))
    open_col = _first_existing(out, ("opening_price", "OpeningPrice", "open", "open_price"))

    if sym_col is None or price_col is None:
        return pd.DataFrame()

    norm = pd.DataFrame(index=out.index)
    norm["symbol"] = out[sym_col].astype(str).str.strip().str.upper().str.replace(r"\.T$", "", regex=True).str.replace(r"\.0$", "", regex=True)
    norm["price"] = pd.to_numeric(out[price_col], errors="coerce")
    norm["current_price"] = norm["price"]
    norm["close"] = norm["price"]

    if recv_col is not None:
        norm["received_at"] = _to_naive_datetime(out[recv_col])
    else:
        norm["received_at"] = pd.NaT
    if event_col is not None:
        norm["event_dt"] = _to_naive_datetime(out[event_col])
    else:
        norm["event_dt"] = pd.NaT

    # main.py の判定では「PUSHを受け取った現在時刻」を優先する。
    # kabu Station の CurrentPriceTime は値が動かない銘柄で09:30等のまま残るため、
    # これをdatetimeに使うと summary latest_dt が古く固定される。
    norm["tick_dt"] = norm["received_at"].where(norm["received_at"].notna(), norm["event_dt"])
    norm["tick_dt"] = norm["tick_dt"].where(norm["tick_dt"].notna(), pd.Timestamp(now))

    try:
        cutoff = pd.Timestamp(now).tz_localize(None) + pd.Timedelta(seconds=3)
        floor = cutoff - pd.Timedelta(minutes=max(3, _env_int("SUMMARY_MAIN_MEMORY_LOOKBACK_MIN", 20)))
        norm = norm[(norm["tick_dt"] <= cutoff) & (norm["tick_dt"] >= floor)].copy()
    except Exception:
        pass

    if vol_col is not None:
        norm["trading_volume"] = pd.to_numeric(out[vol_col], errors="coerce")
    else:
        norm["trading_volume"] = pd.NA
    if val_col is not None:
        norm["trading_value"] = pd.to_numeric(out[val_col], errors="coerce")
    else:
        norm["trading_value"] = pd.NA
    if name_col is not None:
        norm["symbolname"] = out[name_col].fillna("").astype(str)
    else:
        norm["symbolname"] = ""
    if high_col is not None:
        norm["day_high"] = pd.to_numeric(out[high_col], errors="coerce")
    else:
        norm["day_high"] = pd.NA
    if low_col is not None:
        norm["day_low"] = pd.to_numeric(out[low_col], errors="coerce")
    else:
        norm["day_low"] = pd.NA
    if open_col is not None:
        norm["day_open"] = pd.to_numeric(out[open_col], errors="coerce")
    else:
        norm["day_open"] = pd.NA

    norm = norm.dropna(subset=["symbol", "price", "tick_dt"])
    norm = norm[norm["symbol"].astype(str).str.len() > 0].copy()
    if norm.empty:
        return pd.DataFrame()
    norm = norm.sort_values(["symbol", "tick_dt"], kind="stable").reset_index(drop=True)
    return norm


def _build_memory_1m_summary(*, now: dt.datetime) -> pd.DataFrame:
    raw = _load_push_memory_df()
    ticks = _normalize_push_ticks(raw, now=now)
    if ticks.empty:
        logger.warning("[SUMMARY MAIN MEMORY 1M] no usable PUSH memory rows raw_rows=%s", len(raw) if isinstance(raw, pd.DataFrame) else 0)
        return pd.DataFrame()

    try:
        ticks["bar_dt"] = ticks["tick_dt"].dt.floor("min")
        grouped = ticks.groupby(["symbol", "bar_dt"], sort=False)
        bars = grouped.agg(
            symbolname=("symbolname", "last"),
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            current_price=("price", "last"),
            price=("price", "last"),
            tick_count=("price", "count"),
            first_tick_at=("tick_dt", "min"),
            last_tick_at=("tick_dt", "max"),
            trading_volume=("trading_volume", "last"),
            trading_value=("trading_value", "last"),
            day_open=("day_open", "last"),
            day_high=("day_high", "last"),
            day_low=("day_low", "last"),
        ).reset_index().rename(columns={"bar_dt": "datetime"})

        bars = bars.sort_values(["symbol", "datetime"], kind="stable")
        bars["prev_close"] = bars.groupby("symbol")["close"].shift(1)
        bars["slope"] = ((bars["close"] - bars["prev_close"]) / bars["prev_close"].replace(0, pd.NA)).fillna(0.0)
        bars["slope_atr_scaled"] = bars["slope"]
        bars["score_slope"] = bars["slope"] * 100.0
        bars["range_pct"] = ((bars["high"] - bars["low"]) / bars["close"].replace(0, pd.NA)).fillna(0.0)
        bars["atr"] = (bars["high"] - bars["low"]).fillna(0.0)

        for win in (5, 25, 75):
            bars[f"ma{win}"] = bars.groupby("symbol")["close"].transform(lambda s, w=win: s.rolling(w, min_periods=1).mean())
        bars["ema12"] = bars.groupby("symbol")["close"].transform(lambda s: s.ewm(span=12, adjust=False, min_periods=1).mean())
        bars["ema26"] = bars.groupby("symbol")["close"].transform(lambda s: s.ewm(span=26, adjust=False, min_periods=1).mean())
        bars["macd"] = (bars["ema12"] - bars["ema26"]).fillna(0.0)
        bars["signal"] = bars.groupby("symbol")["macd"].transform(lambda s: s.ewm(span=9, adjust=False, min_periods=1).mean()).fillna(0.0)

        diff = bars.groupby("symbol")["close"].diff()
        gain = diff.clip(lower=0).groupby(bars["symbol"]).transform(lambda s: s.rolling(14, min_periods=1).mean())
        loss = (-diff.clip(upper=0)).groupby(bars["symbol"]).transform(lambda s: s.rolling(14, min_periods=1).mean())
        rs = gain / loss.replace(0, pd.NA)
        bars["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0)

        bars["symbol_hist_len"] = bars.groupby("symbol").cumcount() + 1
        latest = bars.groupby("symbol", as_index=False, group_keys=False).tail(1).copy()

        # 日中累積出来高/売買代金がある場合は、それを流動性フィルタに使えるようにする。
        latest["volume"] = pd.to_numeric(latest.get("trading_volume"), errors="coerce").fillna(pd.to_numeric(latest.get("tick_count"), errors="coerce")).fillna(0.0)
        latest["turnover"] = pd.to_numeric(latest.get("trading_value"), errors="coerce")
        latest["turnover"] = latest["turnover"].fillna(latest["close"] * latest["volume"])

        # 軽量スコア。重いscoring/enrichは後段の非同期・既存パッチに任せる。
        score = (latest["slope"].fillna(0.0) * 1000.0).clip(-3.0, 3.0)
        range_bonus = (latest["range_pct"].fillna(0.0) * 20.0).clip(0.0, 2.0)
        latest["score_buy"] = (score.clip(lower=0.0) + range_bonus.where(score > 0, 0.0)).fillna(0.0)
        latest["score_sell"] = ((-score).clip(lower=0.0) + range_bonus.where(score < 0, 0.0)).fillna(0.0)
        latest["score"] = latest["score_buy"] - latest["score_sell"]
        latest["score_total"] = latest["score"]
        latest["final_score"] = latest["score"]
        latest["display_score"] = latest["score"]

        latest["mtf"] = latest.get("mtf", 0.0)
        latest["score_mtf"] = latest.get("score_mtf", 0.0)
        latest["mtf_score"] = latest.get("mtf_score", 0.0)
        latest["technical_ready"] = True
        latest["display_ready"] = True
        latest["source"] = "push_memory_1m"
        latest["interval"] = 1
        latest["start_time"] = latest["datetime"]
        latest["end_time"] = latest["datetime"] + pd.Timedelta(minutes=1)
        latest["time"] = latest["datetime"]
        latest["date"] = latest["datetime"].dt.date.astype(str)

        max_symbols = max(10, _env_int("SUMMARY_MAIN_MEMORY_MAX_SYMBOLS", 200))
        latest = latest.sort_values(["datetime", "symbol"], kind="stable").tail(max_symbols).reset_index(drop=True)

        latest_dt = latest["datetime"].max() if "datetime" in latest.columns and not latest.empty else None
        age = None
        try:
            age = (pd.Timestamp(now).tz_localize(None) - pd.Timestamp(latest_dt).tz_localize(None)).total_seconds()
        except Exception:
            pass
        logger.warning(
            "[SUMMARY MAIN MEMORY 1M] built rows=%s symbols=%s latest_dt=%s age_sec=%s raw_rows=%s tick_rows=%s",
            len(latest),
            int(latest["symbol"].nunique()) if "symbol" in latest.columns else 0,
            latest_dt,
            None if age is None else round(float(age), 1),
            len(raw),
            len(ticks),
        )
        return latest
    except Exception:
        logger.exception("[SUMMARY MAIN MEMORY 1M] build failed raw_rows=%s tick_rows=%s", len(raw), len(ticks))
        return pd.DataFrame()


def _publish_latest(df: pd.DataFrame) -> None:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return
    try:
        gd = _global_data()
        if gd is not None:
            fn = getattr(gd, "set_merged_summary", None) or getattr(gd, "set_push_merged_summary", None)
            if callable(fn):
                try:
                    fn(tf=1, df=df, source="push")
                except TypeError:
                    try:
                        fn(1, df)
                    except TypeError:
                        fn(1, df, "push")
        try:
            from core.global_context.context import global_data as ctx_gd
            fn2 = getattr(ctx_gd, "set_merged_summary", None) or getattr(ctx_gd, "set_push_merged_summary", None)
            if callable(fn2):
                try:
                    fn2(tf=1, df=df, source="push")
                except TypeError:
                    fn2(1, df)
        except Exception:
            pass
    except Exception:
        logger.debug("[SUMMARY MAIN MEMORY 1M] publish skipped", exc_info=True)


def _submit_async_ai(df: pd.DataFrame, *, now: dt.datetime, run_entry: bool) -> None:
    if not (_env_bool("SUMMARY_MAIN_MEMORY_ASYNC_AI", True) and run_entry and isinstance(df, pd.DataFrame) and not df.empty):
        return
    try:
        from core.startup.summary_main_1m_light_tick_patch import _submit_async_ai as submit_ai
        submit_ai(df, interval=1, now=now, run_entry=run_entry, reason="memory_latest_1m")
    except Exception:
        logger.debug("[SUMMARY MAIN MEMORY 1M] async ai submit skipped", exc_info=True)


def _wrap_runner_core() -> bool:
    try:
        import scheduler_jobs.summary.runner_core as rc
    except Exception:
        logger.exception("[SUMMARY MAIN MEMORY 1M] import runner_core failed")
        return False

    orig_job_summary = getattr(rc, "job_summary", None)
    if not callable(orig_job_summary):
        logger.warning("[SUMMARY MAIN MEMORY 1M] runner_core.job_summary unavailable")
        return False
    if getattr(orig_job_summary, "_summary_main_memory_latest_wrapped", False):
        return True

    @wraps(orig_job_summary)
    def job_summary_memory(interval: int, display: bool = True, now: Optional[dt.datetime] = None, run_entry: bool = True, **kwargs):
        interval_i = int(interval)
        if not (_is_entry_only_context() and interval_i == 1 and _env_bool("SUMMARY_MAIN_MEMORY_LATEST_1M_ENABLED", True)):
            return orig_job_summary(interval_i, display=display, now=now, run_entry=run_entry, **kwargs)

        now_i = now or dt.datetime.now()
        try:
            now_i = now_i.replace(microsecond=0)
        except Exception:
            now_i = dt.datetime.now().replace(microsecond=0)

        t0 = time.perf_counter()
        df = _build_memory_1m_summary(now=now_i)
        if isinstance(df, pd.DataFrame) and not df.empty:
            _publish_latest(df)
            _submit_async_ai(df, now=now_i, run_entry=bool(run_entry))
            logger.warning(
                "[SUMMARY MAIN MEMORY 1M] return memory summary rows=%s latest_dt=%s elapsed=%.3fs display_skipped=True",
                len(df),
                df["datetime"].max() if "datetime" in df.columns else None,
                time.perf_counter() - t0,
            )
            return df

        logger.warning("[SUMMARY MAIN MEMORY 1M] memory summary empty -> original fallback interval=%s", interval_i)
        return orig_job_summary(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)

    job_summary_memory._summary_main_memory_latest_wrapped = True  # type: ignore[attr-defined]
    job_summary_memory._original = orig_job_summary  # type: ignore[attr-defined]
    rc.job_summary = job_summary_memory
    rc.run_push_summary_job = lambda interval=1, display=True, now=None, run_entry=True, **kwargs: job_summary_memory(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)
    rc.job_1m = lambda display=True, now=None, run_entry=True: job_summary_memory(1, display=display, now=now, run_entry=run_entry)
    logger.warning("[SUMMARY MAIN MEMORY 1M] runner_core wrapped")
    return True


def _wrap_scheduler_runner_aliases() -> None:
    try:
        import scheduler_jobs.summary.scheduler as scheduler
        import scheduler_jobs.summary.runner_core as rc
        if callable(getattr(rc, "job_1m", None)):
            scheduler.job_push_summary_1m = rc.job_1m
            logger.warning("[SUMMARY MAIN MEMORY 1M] scheduler job_push_summary_1m alias updated")
    except Exception:
        logger.debug("[SUMMARY MAIN MEMORY 1M] scheduler alias update skipped", exc_info=True)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _is_entry_only_context():
        logger.warning("[SUMMARY MAIN MEMORY 1M] skipped non-main context version=%s", VERSION)
        return False
    try:
        os.environ.setdefault("SUMMARY_MAIN_MEMORY_LATEST_1M_ENABLED", "1")
        os.environ.setdefault("SUMMARY_MAIN_MEMORY_LOOKBACK_MIN", "20")
        os.environ.setdefault("SUMMARY_MAIN_MEMORY_MAX_SYMBOLS", "200")
        os.environ.setdefault("SUMMARY_MAIN_MEMORY_ASYNC_AI", "1")
        ok = _wrap_runner_core()
        _wrap_scheduler_runner_aliases()
        _INSTALLED = bool(ok)
        logger.warning("[SUMMARY MAIN MEMORY 1M] installed=%s version=%s", ok, VERSION)
        return bool(ok)
    except Exception:
        logger.exception("[SUMMARY MAIN MEMORY 1M] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN MEMORY 1M] auto install failed")


__all__ = ["VERSION", "install"]
