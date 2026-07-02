# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_memory_latest_1m_patch.py
# Version: V3-MAIN-MEMORY-NO-STALE-COERCE
# ------------------------------------------------------------
# main.py は PUSH DB 保存をしない前提のまま、PUSHメモリDFから
# 最新1分足 summary を高速生成する。
#
# V3:
#   - 古い PUSH tick を now に付け替えて「新鮮なsummary」に見せない。
#   - raw_rows があっても tick_dt が lookback 外なら空を返す。
#   - これにより score/slope/macd=0 の偽fresh summaryをAIへ渡さない。
#   - 互換用に SUMMARY_MAIN_MEMORY_COERCE_OLD_TICKS_TO_NOW=1 の時だけ
#     旧coerce動作を許可する。既定は 0。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sys
import time
from functools import wraps
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V3-MAIN-MEMORY-NO-STALE-COERCE"
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


def _flatten_object_dict_columns(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    out = df.copy()
    try:
        for c in list(out.columns):
            try:
                s = out[c].dropna()
                if s.empty:
                    continue
                sample = s.iloc[0]
                if isinstance(sample, dict):
                    exp = pd.json_normalize(out[c]).add_prefix(f"{c}.")
                    exp.index = out.index
                    out = pd.concat([out.drop(columns=[c]), exp], axis=1)
            except Exception:
                continue
    except Exception:
        pass
    out.columns = [str(c).strip() for c in out.columns]
    out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
    return out


def _canon(s: Any) -> str:
    return re.sub(r"[^0-9a-zA-Z一-龥ぁ-んァ-ヶー]", "", str(s or "").strip().lower())


def _first_existing(df: pd.DataFrame, names: tuple[str, ...]) -> Optional[str]:
    if not isinstance(df, pd.DataFrame):
        return None
    exact = {str(c): c for c in df.columns}
    canon = {_canon(c): c for c in df.columns}
    for name in names:
        if name in exact:
            return exact[name]
        cn = _canon(name)
        if cn in canon:
            return canon[cn]
    for name in names:
        cn = _canon(name)
        for c in df.columns:
            cc = _canon(c)
            if cc.endswith(cn):
                return c
    return None


def _to_naive_datetime_any(values: Any, *, now: dt.datetime, date_values: Any = None) -> pd.Series:
    idx = getattr(values, "index", None)
    try:
        s = pd.Series(values, index=idx) if not isinstance(values, pd.Series) else values.copy()
    except Exception:
        return pd.Series(pd.NaT, index=idx)

    today_s = pd.Timestamp(now).strftime("%Y-%m-%d")
    if date_values is not None:
        try:
            ds = pd.Series(date_values, index=s.index) if not isinstance(date_values, pd.Series) else date_values.reindex(s.index)
            dparsed = pd.to_datetime(ds, errors="coerce")
            today_by_row = dparsed.dt.strftime("%Y-%m-%d").where(dparsed.notna(), today_s)
        except Exception:
            today_by_row = pd.Series(today_s, index=s.index)
    else:
        today_by_row = pd.Series(today_s, index=s.index)

    def one(v: Any, day: str) -> Any:
        try:
            if pd.isna(v):
                return pd.NaT
            txt = str(v).strip()
            if txt == "":
                return pd.NaT
            if re.fullmatch(r"\d{5,6}", txt):
                txt = txt.zfill(6)
                return pd.Timestamp(f"{day} {txt[0:2]}:{txt[2:4]}:{txt[4:6]}")
            if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?", txt):
                return pd.Timestamp(f"{day} {txt}")
            ts = pd.Timestamp(v)
            if pd.isna(ts):
                return pd.NaT
            if getattr(ts, "tzinfo", None) is not None:
                try:
                    ts = ts.tz_convert("Asia/Tokyo").tz_localize(None)
                except Exception:
                    ts = ts.tz_localize(None)
            return ts
        except Exception:
            return pd.NaT

    try:
        out = pd.Series([one(v, d) for v, d in zip(s.tolist(), today_by_row.tolist())], index=s.index)
        return pd.to_datetime(out, errors="coerce")
    except Exception:
        return pd.Series(pd.NaT, index=s.index)


def _load_push_memory_df() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    gd = _global_data()
    if gd is not None:
        for name in ("push_df", "stream_data", "latest_push_df", "push_data", "push_snapshot_df", "PUSH_DF"):
            try:
                x = getattr(gd, name, None)
                if isinstance(x, pd.DataFrame) and not x.empty:
                    frames.append(x)
            except Exception:
                pass
        for name in ("get_push_df", "get_latest_push_df", "get_stream_data"):
            try:
                fn = getattr(gd, name, None)
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
        out = pd.concat([_flatten_object_dict_columns(_as_df(x)) for x in frames if isinstance(x, pd.DataFrame) and not x.empty], ignore_index=True, sort=False)
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MAIN MEMORY 1M] concat push memory df failed")
        return _flatten_object_dict_columns(frames[-1]) if frames else pd.DataFrame()


def _normalize_push_ticks(df: pd.DataFrame, *, now: dt.datetime) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = _flatten_object_dict_columns(df)
    sym_col = _first_existing(out, ("symbol", "Symbol", "code", "Code", "symbol_code", "SymbolCode", "銘柄コード"))
    price_col = _first_existing(out, (
        "current_price", "CurrentPrice", "price", "Price", "close", "close_price", "Close", "ClosePrice", "現在値", "現在値段",
    ))
    recv_col = _first_existing(out, ("received_at", "ReceivedAt", "recv_at", "inserted_at", "created_at", "updated_at"))
    event_col = _first_existing(out, (
        "datetime", "Datetime", "time", "Time", "current_price_time", "CurrentPriceTime", "timestamp", "PriceTime", "時刻", "現在値時刻",
    ))
    date_col = _first_existing(out, ("date", "Date", "business_date", "BusinessDate", "年月日", "日付"))
    vol_col = _first_existing(out, ("trading_volume", "TradingVolume", "volume", "Volume", "出来高", "売買高"))
    val_col = _first_existing(out, ("trading_value", "TradingValue", "turnover", "Value", "売買代金"))
    name_col = _first_existing(out, ("symbolname", "SymbolName", "symbol_name", "name", "Name", "銘柄名"))
    high_col = _first_existing(out, ("high_price", "HighPrice", "high", "High", "高値"))
    low_col = _first_existing(out, ("low_price", "LowPrice", "low", "Low", "安値"))
    open_col = _first_existing(out, ("opening_price", "OpeningPrice", "open", "Open", "open_price", "始値"))

    if sym_col is None or price_col is None:
        logger.warning(
            "[SUMMARY MAIN MEMORY 1M] required columns missing symbol_col=%s price_col=%s cols=%s raw_rows=%s version=%s",
            sym_col,
            price_col,
            list(out.columns)[:120],
            len(out),
            VERSION,
        )
        return pd.DataFrame()

    norm = pd.DataFrame(index=out.index)
    norm["symbol"] = out[sym_col].astype(str).str.strip().str.upper().str.replace(r"\.T$", "", regex=True).str.replace(r"\.0$", "", regex=True)
    norm["price"] = pd.to_numeric(out[price_col], errors="coerce")
    norm["current_price"] = norm["price"]
    norm["close"] = norm["price"]

    date_values = out[date_col] if date_col is not None else None
    recv_dt = _to_naive_datetime_any(out[recv_col], now=now, date_values=date_values) if recv_col is not None else pd.Series(pd.NaT, index=out.index)
    event_dt = _to_naive_datetime_any(out[event_col], now=now, date_values=date_values) if event_col is not None else pd.Series(pd.NaT, index=out.index)

    norm["tick_dt"] = recv_dt.where(recv_dt.notna(), event_dt)
    missing_dt = norm["tick_dt"].isna()
    if int(missing_dt.sum()) > 0:
        # 欠損時刻だけは now 補完を許可。古い時刻の付け替えは下で拒否する。
        norm.loc[missing_dt, "tick_dt"] = pd.Timestamp(now).tz_localize(None)

    try:
        now_ts = pd.Timestamp(now).tz_localize(None)
        cutoff = now_ts + pd.Timedelta(seconds=max(3, _env_int("SUMMARY_MAIN_MEMORY_FUTURE_GRACE_SEC", 75)))
        lookback_min = max(1, _env_int("SUMMARY_MAIN_MEMORY_LOOKBACK_MIN", 30))
        floor = now_ts - pd.Timedelta(minutes=lookback_min)
        keep = (norm["tick_dt"] <= cutoff) & (norm["tick_dt"] >= floor)
        if int(keep.sum()) == 0 and len(norm) > 0:
            min_dt = norm["tick_dt"].min() if "tick_dt" in norm.columns else None
            max_dt = norm["tick_dt"].max() if "tick_dt" in norm.columns else None
            if _env_bool("SUMMARY_MAIN_MEMORY_COERCE_OLD_TICKS_TO_NOW", False):
                logger.warning(
                    "[SUMMARY MAIN MEMORY 1M] no rows in time window -> COERCE OLD TICKS ENABLED raw_rows=%s min_dt=%s max_dt=%s lookback_min=%s version=%s",
                    len(norm), min_dt, max_dt, lookback_min, VERSION,
                )
                norm["tick_dt"] = now_ts
                keep = pd.Series(True, index=norm.index)
            else:
                try:
                    stale_sec = (now_ts - pd.Timestamp(max_dt).tz_localize(None)).total_seconds() if max_dt is not None and not pd.isna(max_dt) else None
                except Exception:
                    stale_sec = None
                logger.warning(
                    "[SUMMARY MAIN MEMORY 1M] stale push memory rejected raw_rows=%s min_dt=%s max_dt=%s stale_sec=%s lookback_min=%s coerce_old=0 version=%s",
                    len(norm),
                    min_dt,
                    max_dt,
                    None if stale_sec is None else round(float(stale_sec), 1),
                    lookback_min,
                    VERSION,
                )
                return pd.DataFrame()
        norm = norm[keep].copy()
    except Exception:
        logger.debug("[SUMMARY MAIN MEMORY 1M] time window filter failed", exc_info=True)

    norm["trading_volume"] = pd.to_numeric(out[vol_col], errors="coerce") if vol_col is not None else pd.NA
    norm["trading_value"] = pd.to_numeric(out[val_col], errors="coerce") if val_col is not None else pd.NA
    norm["symbolname"] = out[name_col].fillna("").astype(str) if name_col is not None else ""
    norm["day_high"] = pd.to_numeric(out[high_col], errors="coerce") if high_col is not None else pd.NA
    norm["day_low"] = pd.to_numeric(out[low_col], errors="coerce") if low_col is not None else pd.NA
    norm["day_open"] = pd.to_numeric(out[open_col], errors="coerce") if open_col is not None else pd.NA

    before = len(norm)
    norm = norm.dropna(subset=["symbol", "price", "tick_dt"])
    norm = norm[norm["symbol"].astype(str).str.len() > 0]
    norm = norm[norm["price"] > 0].copy()
    if norm.empty:
        logger.warning(
            "[SUMMARY MAIN MEMORY 1M] normalized empty after drop before=%s symbol_col=%s price_col=%s recv_col=%s event_col=%s version=%s",
            before,
            sym_col,
            price_col,
            recv_col,
            event_col,
            VERSION,
        )
        return pd.DataFrame()
    norm = norm.sort_values(["symbol", "tick_dt"], kind="stable").reset_index(drop=True)
    logger.warning(
        "[SUMMARY MAIN MEMORY 1M] normalized ticks rows=%s symbols=%s raw_rows=%s symbol_col=%s price_col=%s recv_col=%s event_col=%s latest_tick=%s version=%s",
        len(norm),
        int(norm["symbol"].nunique()),
        len(out),
        sym_col,
        price_col,
        recv_col,
        event_col,
        norm["tick_dt"].max(),
        VERSION,
    )
    return norm


def _build_memory_1m_summary(*, now: dt.datetime) -> pd.DataFrame:
    raw = _load_push_memory_df()
    ticks = _normalize_push_ticks(raw, now=now)
    if ticks.empty:
        logger.warning("[SUMMARY MAIN MEMORY 1M] no usable PUSH memory rows raw_rows=%s version=%s", len(raw) if isinstance(raw, pd.DataFrame) else 0, VERSION)
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
        bars["hist"] = bars["macd"] - bars["signal"]

        diff = bars.groupby("symbol")["close"].diff()
        gain = diff.clip(lower=0).groupby(bars["symbol"]).transform(lambda s: s.rolling(14, min_periods=1).mean())
        loss = (-diff.clip(upper=0)).groupby(bars["symbol"]).transform(lambda s: s.rolling(14, min_periods=1).mean())
        rs = gain / loss.replace(0, pd.NA)
        bars["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0)

        bars["symbol_hist_len"] = bars.groupby("symbol").cumcount() + 1
        latest = bars.groupby("symbol", as_index=False, group_keys=False).tail(1).copy()
        latest["volume"] = pd.to_numeric(latest.get("trading_volume"), errors="coerce").fillna(pd.to_numeric(latest.get("tick_count"), errors="coerce")).fillna(0.0)
        latest["turnover"] = pd.to_numeric(latest.get("trading_value"), errors="coerce")
        latest["turnover"] = latest["turnover"].fillna(latest["close"] * latest["volume"])

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
        latest["open_price"] = latest["open"]
        latest["high_price"] = latest["high"]
        latest["low_price"] = latest["low"]
        latest["close_price"] = latest["close"]
        latest["current_price"] = latest["close"]
        latest["price"] = latest["close"]
        latest["vwap"] = latest["close"]

        max_symbols = max(10, _env_int("SUMMARY_MAIN_MEMORY_MAX_SYMBOLS", 200))
        latest = latest.sort_values(["datetime", "symbol"], kind="stable").tail(max_symbols).reset_index(drop=True)

        latest_dt = latest["datetime"].max() if "datetime" in latest.columns and not latest.empty else None
        age = None
        try:
            age = (pd.Timestamp(now).tz_localize(None) - pd.Timestamp(latest_dt).tz_localize(None)).total_seconds()
        except Exception:
            pass
        logger.warning(
            "[SUMMARY MAIN MEMORY 1M] built rows=%s symbols=%s latest_dt=%s age_sec=%s raw_rows=%s tick_rows=%s version=%s",
            len(latest),
            int(latest["symbol"].nunique()) if "symbol" in latest.columns else 0,
            latest_dt,
            None if age is None else round(float(age), 1),
            len(raw),
            len(ticks),
            VERSION,
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
            for name in ("push_summary_1", "push_summary_1min", "push_merged_summary_1", "push_merged_summary_1min", "merged_summary_1", "merged_summary_1min"):
                try:
                    setattr(gd, name, df)
                except Exception:
                    pass
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
    if getattr(orig_job_summary, "_summary_main_memory_latest_wrapped_v3", False):
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
        raw = _load_push_memory_df()
        df = _build_memory_1m_summary(now=now_i)
        if isinstance(df, pd.DataFrame) and not df.empty:
            _publish_latest(df)
            _submit_async_ai(df, now=now_i, run_entry=bool(run_entry))
            logger.warning(
                "[SUMMARY MAIN MEMORY 1M] return memory summary rows=%s latest_dt=%s elapsed=%.3fs display_skipped=True version=%s",
                len(df),
                df["datetime"].max() if "datetime" in df.columns else None,
                time.perf_counter() - t0,
                VERSION,
            )
            return df

        if _env_bool("SUMMARY_MAIN_MEMORY_NO_HEAVY_FALLBACK_WHEN_RAW_EXISTS", True) and isinstance(raw, pd.DataFrame) and not raw.empty:
            logger.warning(
                "[SUMMARY MAIN MEMORY 1M] memory build empty but raw exists -> skip original heavy fallback interval=%s raw_rows=%s elapsed=%.3fs version=%s",
                interval_i,
                len(raw),
                time.perf_counter() - t0,
                VERSION,
            )
            return pd.DataFrame()

        logger.warning("[SUMMARY MAIN MEMORY 1M] memory summary empty -> original fallback interval=%s version=%s", interval_i, VERSION)
        return orig_job_summary(interval_i, display=display, now=now_i, run_entry=run_entry, **kwargs)

    job_summary_memory._summary_main_memory_latest_wrapped = True  # type: ignore[attr-defined]
    job_summary_memory._summary_main_memory_latest_wrapped_v2 = True  # type: ignore[attr-defined]
    job_summary_memory._summary_main_memory_latest_wrapped_v3 = True  # type: ignore[attr-defined]
    job_summary_memory._original = orig_job_summary  # type: ignore[attr-defined]
    rc.job_summary = job_summary_memory
    rc.run_push_summary_job = lambda interval=1, display=True, now=None, run_entry=True, **kwargs: job_summary_memory(int(interval), display=display, now=now, run_entry=run_entry, **kwargs)
    rc.job_1m = lambda display=True, now=None, run_entry=True: job_summary_memory(1, display=display, now=now, run_entry=run_entry)
    logger.warning("[SUMMARY MAIN MEMORY 1M] runner_core wrapped version=%s", VERSION)
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
        os.environ.setdefault("SUMMARY_MAIN_MEMORY_LOOKBACK_MIN", "30")
        os.environ.setdefault("SUMMARY_MAIN_MEMORY_MAX_SYMBOLS", "200")
        os.environ.setdefault("SUMMARY_MAIN_MEMORY_ASYNC_AI", "1")
        os.environ.setdefault("SUMMARY_MAIN_MEMORY_NO_HEAVY_FALLBACK_WHEN_RAW_EXISTS", "1")
        os.environ.setdefault("SUMMARY_MAIN_MEMORY_COERCE_OLD_TICKS_TO_NOW", "0")
        ok = _wrap_runner_core()
        _wrap_scheduler_runner_aliases()
        _INSTALLED = bool(ok)
        logger.warning("[SUMMARY MAIN MEMORY 1M] installed=%s version=%s coerce_old=%s", ok, VERSION, _env_bool("SUMMARY_MAIN_MEMORY_COERCE_OLD_TICKS_TO_NOW", False))
        return bool(ok)
    except Exception:
        logger.exception("[SUMMARY MAIN MEMORY 1M] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN MEMORY 1M] auto install failed")


__all__ = ["VERSION", "install", "_build_memory_1m_summary", "_load_push_memory_df"]
