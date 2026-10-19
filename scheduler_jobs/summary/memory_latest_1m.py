# -*- coding: utf-8 -*-
# ============================================================
# File   : scheduler_jobs/summary/memory_latest_1m.py
# Version: V3-MAIN-MEMORY-NO-STALE-COERCE  (VERSION 定数と一致させること。ログに出る)
# ------------------------------------------------------------
# main.py は PUSH DB 保存をしない前提のまま、PUSHメモリDFから
# 最新1分足 summary を高速生成する。
#
# 移設 (2026-08-12):
#   - core/startup/summary_main_memory_latest_1m_patch.py から移設した。
#     V4 で job_summary のラップを撤去した時点でモンキーパッチは無くなり、
#     _build_memory_1m_summary / _load_push_memory_df を提供するライブラリ +
#     env 既定値 6 件の install() だけになっていたので、
#     利用側と同じ scheduler_jobs/summary/ 配下へ置く。
#   - 末尾の auto install は残す (env の setdefault のみで冪等)。
#     install() は _is_entry_only_context() で非 main を弾くので、
#     import されても main 以外では env を触らない。
#   - このモジュールの関数を差し替える側は core/startup に残っている:
#       summary_main_push_db_refresh_patch  -> _load_push_memory_df
#       (summary_mtf_diff_from_1m_patch の _publish_latest 差し替えは撤去済み。
#        history ブリッジは _publish_latest から直接呼ぶ)
#
# V4:
#   - job_summary/run_push_summary_job/job_1m への再代入、および
#     scheduler.job_push_summary_1m のエイリアス同期を撤去した。
#     このファイル・scheduler_jobs/summary/light_tick_1m.py・
#     scheduler_jobs/summary/direct_push_1m.py の3本が同じ3関数を
#     total-overwrite方式で奪い合っていた「3-way race」の一部
#     (詳細は scheduler_jobs/summary/direct_push_1m.py のコメント参照)。
#     _build_memory_1m_summary はそのままライブラリ関数として維持し、
#     scheduler_jobs/summary/direct_push_1m.py の tier1_build_and_publish
#     経由 (旧来と同じ呼び出し方) で scheduler_jobs/summary/runner_core.py
#     の job_summary から間接的に使われ続ける。
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
import sqlite3
import sys
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from nas_root import nas_path, nas_root

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


def _load_push_memory_df_core() -> pd.DataFrame:
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
        # pd.NA だと object dtype になり fillna で暗黙ダウンキャスト警告になる。
        rs = gain / loss.replace(0, np.nan)
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
    """latest を global へ publish し、続けて 1m history ブリッジを呼ぶ。

    旧構成では core/startup/summary_mtf_diff_from_1m_patch がこのシンボルを
    差し替えて history 保存を足していた。差し替えをやめ、こちらから
    publish_1m_history_bridge() を直接呼ぶ形にした。
    旧ラッパは finally でブリッジを走らせていたので、core が例外を投げても
    ブリッジを実行し、例外はそのまま呼び出し元へ伝播させる。
    """
    try:
        _publish_latest_core(df)
    finally:
        _publish_1m_history_bridge_safe(df)


def _publish_1m_history_bridge_safe(df: pd.DataFrame) -> None:
    try:
        from trading.summary.mtf_diff_from_1m import publish_1m_history_bridge
    except Exception:
        logger.debug("[SUMMARY MAIN MEMORY 1M] history bridge import failed", exc_info=True)
        return
    try:
        publish_1m_history_bridge(df)
    except Exception:
        logger.exception("[SUMMARY MAIN MEMORY 1M] history bridge failed")


def _publish_latest_core(df: pd.DataFrame) -> None:
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
        from .light_tick_1m import _submit_async_ai as submit_ai
        submit_ai(df, interval=1, now=now, run_entry=run_entry, reason="memory_latest_1m")
    except Exception:
        logger.debug("[SUMMARY MAIN MEMORY 1M] async ai submit skipped", exc_info=True)


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
        _INSTALLED = True
        logger.warning("[SUMMARY MAIN MEMORY 1M] installed=%s version=%s coerce_old=%s (job_summary wrap removed; _build_memory_1m_summary now used as a library by scheduler_jobs.summary.direct_push_1m.tier1_build_and_publish)", True, VERSION, _env_bool("SUMMARY_MAIN_MEMORY_COERCE_OLD_TICKS_TO_NOW", False))
        return True
    except Exception:
        logger.exception("[SUMMARY MAIN MEMORY 1M] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN MEMORY 1M] auto install failed")


# ============================================================
# PUSH DB からの再取得 (stale 時のフォールバック)
# 旧 core/startup/summary_main_push_db_refresh_patch.py V2 を本文化。
#
# 旧構成は _load_push_memory_df をモジュール属性ごと差し替えていた。
# install 元は sitecustomize:366 (SYNC_MAIN) の 1 箇所で、
# ラッパ自身も _is_main_py() を見て非 main では base をそのまま返していた。
# その 2 条件 (+ DISABLE_SUMMARY_MAIN_PUSH_DB_REFRESH_PATCH=="1") を
# _push_db_refresh_enabled() が再現する。
#
# 注意: _push_db_refresh_env_bool は旧パッチ側の _env_bool をそのまま持ち込んだもの。
# このファイルの _env_bool は「未知の値 -> default」だが、旧パッチ版は
# 「TRUE 集合に無ければ False」で解釈が違うため共用しないこと。
# ============================================================


def _push_db_refresh_enabled() -> bool:
    """このプロセスで PUSH DB フォールバックを行うか (旧 install スコープの再現)。"""
    try:
        if str(os.environ.get("DISABLE_SUMMARY_MAIN_PUSH_DB_REFRESH_PATCH", "")).strip() == "1":
            return False
        return _is_main_py()
    except Exception:
        return False

def _push_db_refresh_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return float(str(v).replace(",", "").strip())
    except Exception:
        pass
    return float(default)


def _nas_root() -> str:
    for name in ("AUTOSTOCK_NAS_ROOT", "AUTO_STOCK_NAS_ROOT", "AUTOSTOCK_DATA_ROOT"):
        v = os.getenv(name)
        if v and str(v).strip():
            return str(v).strip()
    return nas_root()


def _today_yyyymmdd() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _push_db_path() -> Path:
    explicit = os.getenv("PUSH_DB_PATH") or os.getenv("AUTOSTOCK_PUSH_DB_PATH")
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip())
    # 2026-09-05: writer がローカルへ書くようになったので、読み手も同じ
    # 解決関数を通す。ここを NAS 固定のままにすると、場中ずっと前日の
    # NAS 複製を読んでしまう。
    try:
        from trading.push import push_db_location

        return push_db_location.push_db_path()
    except Exception:
        pass
    return Path(_nas_root()) / "raw_data" / "kabu_station" / "push" / f"push{_today_yyyymmdd()}.db"


def _safe_dt_series(values: Any) -> pd.Series:
    try:
        s = values if isinstance(values, pd.Series) else pd.Series(values)
        out = _to_datetime_iso_fast(s)
        if out is None:
            out = pd.to_datetime(s, errors="coerce")
        try:
            if getattr(out.dt, "tz", None) is not None:
                out = _drop_tz_to_jst(out)
        except Exception:
            try:
                out = out.dt.tz_localize(None)
            except Exception:
                pass
        return out
    except Exception:
        return pd.Series(pd.NaT, index=getattr(values, "index", None))


_PUSH_DT_COLUMNS = ("datetime", "received_at", "time", "current_price_time", "PriceTime", "updated_at", "created_at")


def _drop_tz_to_jst(series):
    """tz 付き Series を tz-naive JST の壁時計へ落とす。

    2026-09-02 実測: s.dt.tz_convert("Asia/Tokyo") は初回だけ 1.4932s かかる
    (NAS 上の .venv から tz データベースを読むため)。PUSH DB の datetime は
    すべて固定オフセット +09:00 なので、その場合は tz_convert を挟まずに
    tz_localize(None) するだけでよい。値が同じことは実測で確認済み
    (equal: True / 1500 行)。0.0001s。
    """
    try:
        tz = series.dt.tz
    except Exception:
        return series
    if tz is None:
        return series
    try:
        off = tz.utcoffset(None)
    except Exception:
        off = None
    if off is not None and off == dt.timedelta(hours=9):
        return series.dt.tz_localize(None)
    return series.dt.tz_convert("Asia/Tokyo").dt.tz_localize(None)


def _to_datetime_iso_fast(values: Any):
    """ISO8601 文字列列を高速に datetime へ変換する。

    2026-09-02 実測 (push20260902.db, 1500 行 / 12 列):
        format 指定なし  ring_merge = 6.164s
        format="ISO8601"           = 0.01s 台
    pandas は format を推定できないと 1 要素ずつ dateutil で解析する
    ("Could not infer format ... falling back to dateutil" の UserWarning)。
    stream_data_raw の datetime / received_at は
        2026-09-02T15:41:37.505281+09:00
        2026-09-02T15:41:37.505281
    のどちらかなので ISO8601 で読める。
    読めない列は従来どおり推定に落として挙動を変えない。
    """
    try:
        col = values if isinstance(values, pd.Series) else pd.Series(values)
    except Exception:
        return None
    if col.dtype.kind == "M":
        return col
    try:
        out = pd.to_datetime(col, errors="coerce", format="ISO8601")
        # ISO8601 で 1 件も読めなかった列は ISO タイムスタンプ列ではない
        # (time='15:41:37' など)。dateutil の 1 要素ずつ解析へは落とさない。
        # 実測: dateutil 経路は 1500 行 x 3 列で 2.0-6.2 秒かかる。
        if out.notna().any() or not col.notna().any():
            return out
        return None
    except Exception:
        return None


def _normalize_dt_columns(df: Any) -> Any:
    """PUSH DB から読んだフレームの時刻列を tz-naive JST に揃える。

    PUSH DB の stream_data_raw は datetime だけ +09:00 付きの ISO 文字列で、
    received_at は naive という不揃いな保存形式になっている
    (実測: '2026-08-31T11:02:17.437863+09:00' / '2026-08-31T11:02:17.437863')。
    そのまま global_data.push_df へ渡すと、WS 経路が作る tz-naive フレームと
    比較できず push_df setter が
      TypeError: Cannot compare tz-naive and tz-aware timestamps
    で落ちる。

    正準形は websocket_handlers.dataframe_manager.make_tz_naive() と同じ
    「tz を外した JST の壁時計」。+09:00 は JST 表記なので Asia/Tokyo へ
    変換してから tz を落とせば値は変わらない。DB は書き換えない。

    ring buffer へ concat する前に通すこと。naive と aware を concat すると
    列が object dtype に落ちて、以後の比較が同じ理由で壊れる。
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = df
    copied = False
    for col in _PUSH_DT_COLUMNS:
        if col not in out.columns:
            continue
        s = _to_datetime_iso_fast(out[col])
        if s is None:
            continue
        try:
            tz = getattr(s.dt, "tz", None)
        except Exception:
            tz = None
        if tz is None:
            # naive の列には触らない。文字列列を datetime へ作り替えると
            # time='11:02:17' のような列の中身が変わってしまう。
            # ここでやるのは「tz を外すこと」だけ。
            continue
        try:
            s = _drop_tz_to_jst(s)
        except Exception:
            continue
        if not copied:
            out = out.copy(deep=False)
            copied = True
        out[col] = s
    return out


def _latest_dt(df: Any) -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    for c in ("datetime", "received_at", "time", "current_price_time", "PriceTime", "updated_at", "created_at"):
        if c in df.columns:
            try:
                x = _safe_dt_series(df[c]).max()
                if pd.notna(x):
                    return pd.Timestamp(x).tz_localize(None)
            except Exception:
                continue
    return None


def _stale_sec(df: pd.DataFrame) -> float | None:
    try:
        latest = _latest_dt(df)
        if latest is None or pd.isna(latest):
            return None
        return (pd.Timestamp(dt.datetime.now()).tz_localize(None) - pd.Timestamp(latest).tz_localize(None)).total_seconds()
    except Exception:
        return None


# ============================================================
# schema キャッシュ
# ------------------------------------------------------------
# PUSH DB は 1 日 1 ファイルで、日中にテーブルや列が増減しない。
# それにもかかわらず 1 回の読み取りごとに
#   SELECT name FROM sqlite_master   (list_tables)
#   PRAGMA table_info(...)           (columns)
# を NAS 越しに実行していた。
#
# 実測 (2026-08-31 12:55, 同一クエリ・同一行数):
#   total=0.339s  list_tables=0.027s columns=0.033s query_fetch=0.096s (空いている時)
#   total=5.742s  list_tables=0.415s columns=0.696s query_fetch=3.552s (writer と競合時)
# 競合時は schema 取得だけで約 1.1 秒使っている。
#
# ファイルパス単位でキャッシュする。日付が変われば path が変わるので
# 自動的に無効化される。SUMMARY_MAIN_PUSH_DB_SCHEMA_CACHE=0 で従来動作。
# ============================================================

_push_db_schema_lock = threading.Lock()
_push_db_schema_cache: dict[str, dict] = {}


def _schema_cache_enabled() -> bool:
    return _env_bool("SUMMARY_MAIN_PUSH_DB_SCHEMA_CACHE", True)


def _table_columns(conn: sqlite3.Connection, table: str, *, cache_key: str = "") -> list[str]:
    if cache_key and _schema_cache_enabled():
        with _push_db_schema_lock:
            entry = _push_db_schema_cache.get(cache_key)
            if entry is not None:
                cols = entry.get("columns", {}).get(table)
                if cols is not None:
                    return list(cols)
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        out = [str(r[1]) for r in rows]
    except Exception:
        return []
    if cache_key and _schema_cache_enabled() and out:
        with _push_db_schema_lock:
            entry = _push_db_schema_cache.setdefault(cache_key, {"tables": None, "columns": {}})
            entry["columns"][table] = list(out)
    return out


def _existing_tables(conn: sqlite3.Connection, *, cache_key: str = "") -> list[str]:
    if cache_key and _schema_cache_enabled():
        with _push_db_schema_lock:
            entry = _push_db_schema_cache.get(cache_key)
            if entry is not None and entry.get("tables") is not None:
                return list(entry["tables"])
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        out = [str(r[0]) for r in rows]
    except Exception:
        return []
    if cache_key and _schema_cache_enabled() and out:
        with _push_db_schema_lock:
            entry = _push_db_schema_cache.setdefault(cache_key, {"tables": None, "columns": {}})
            entry["tables"] = list(out)
    return out


def _schema_cached(cache_key: str, kind: str) -> bool:
    """この呼び出しでキャッシュが効いたか (ログ用)。"""
    if not cache_key or not _schema_cache_enabled():
        return False
    with _push_db_schema_lock:
        entry = _push_db_schema_cache.get(cache_key)
        if entry is None:
            return False
        if kind == "tables":
            return entry.get("tables") is not None
        return bool(entry.get("columns"))


def reset_push_db_schema_cache() -> None:
    """テスト用。"""
    with _push_db_schema_lock:
        _push_db_schema_cache.clear()


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


# ============================================================
# P2: SELECT * が raw_json まで NAS から引いていた
# ------------------------------------------------------------
# stream_data_raw は 66 列あり、その中の raw_json は tick の生 JSON
# (実測 平均 2011 bytes/行) をそのまま持っている。行本体は b-tree の
# leaf に収まるが raw_json は overflow page へ溢れるため、SELECT * は
# 1 行ごとに追加のページ読みを NAS(SMB) 越しに発生させる。
#
# 同じ WHERE / ORDER BY / LIMIT で列だけ変えた実測 (2026-08-31):
#   push20260828.db (569MB, WALなし) 増分678行
#       SELECT *        0.232s
#       必要列のみ      0.003s      (約 77 倍)
#   push20260824.db (433MB)         増分678行
#       SELECT *        0.181s
#       必要列のみ      0.004s
#   push20260831.db 全件5000行 (full mode)
#       SELECT *        6.099s
#       必要列のみ      2.637s
# full mode では 5000 行 x 2KB = 約 10MB を毎回 NAS から引いていた。
#
# raw_json を push_df 側で読んでいる箇所は無い (grep 済み。書き込みと
# ranking 系テーブルの同名列だけ)。取得列から外す。
# 列を外すだけで DB schema は変更しない。
# SUMMARY_MAIN_PUSH_DB_EXCLUDE_COLUMNS="" で従来の SELECT * に戻せる。
# ============================================================

_PUSH_DB_DEFAULT_EXCLUDE_COLUMNS = "raw_json"


def _push_db_excluded_columns() -> set[str]:
    v = os.getenv("SUMMARY_MAIN_PUSH_DB_EXCLUDE_COLUMNS")
    if v is None:
        v = _PUSH_DB_DEFAULT_EXCLUDE_COLUMNS
    return {c.strip().lower() for c in str(v).split(",") if c.strip()}


def _push_db_select_list(cols: list[str]) -> tuple[str, list[str]]:
    """取得する列の SELECT 句を作る。除外できない場合は '*' を返す。"""
    excluded = _push_db_excluded_columns()
    if not excluded:
        return "*", []
    keep = [c for c in cols if str(c).lower() not in excluded]
    dropped = [c for c in cols if str(c).lower() in excluded]
    if not dropped or not keep:
        return "*", []
    return ", ".join(_quote(c) for c in keep), dropped


# ============================================================
# P0-2 / P0-4 / P0-6: PUSH DB 読み取りの計測・共有・増分化
# ------------------------------------------------------------
# 2026-08-28 08:51 の実測で、983 行しか無い入力に対して
# stream_data.fastlane が 41.2 秒、tier1_direct_memory が 49.7 秒
# かかっていた。行数から見てアルゴリズムの問題ではないので、
# 各段を個別に測れるようにする。
#
#   P0-2 connect / tables / columns / execute / fetch / latest を個別計測
#   P0-4 1 tick 内で同じ push DB を何度も開かない (短命キャッシュ)
#   P0-6 last_rowid を保持し、前回以降の tick だけを取る (増分)
#        取得した分はメモリのリングバッファへ積み、そこから返す
# ============================================================

_PUSH_DB_STAGE_WARN_SEC = 1.0

# ============================================================
# 2026-09-02 P0: PUSH DB 読み取りの中断可能化・増分固定・接続再利用
# ------------------------------------------------------------
# 本番実測 (2026-09-02 15:0x, push20260902.db 631MB / WAL 196MB):
#   [PUSH DB STAGE] query_fetch=880.344s mode=full rows=5000
#   [PUSH DB STAGE] connect=2.684s
# 5000 行 DESC 全件読みが 1 回 15 分近くかかっていた。
#
# 悪循環になっていた:
#   full scan (880s) が読み取りトランザクションを開きっぱなし
#     -> wal_checkpoint(PASSIVE) が 1 フレームも進めない
#        (実測 log_frames=41055 checkpointed=0)
#     -> WAL が 196MB まで膨らむ
#     -> ページ解決が更に遅くなる -> full scan が更に長引く
#   しかも full scan が tick 内に終わらないので _push_db_last_rowid が
#   一度も seed されず、**毎回 mode=full** に戻り続けていた
#   (本番ログ: mode=incremental は 0 件)。
#
# 対策 (stale 判定・閾値・fail-closed は変更しない):
#   1. cursor が無いときは full scan せず MAX(rowid) で seed する
#      (MAX(rowid) 実測 0.109s / DESC LIMIT 1 実測 0.028s)
#   2. 以降は必ず WHERE rowid > ? ORDER BY rowid ASC LIMIT ?
#   3. SELECT は 1 分足生成に要る列だけ (66 列 -> 11 列)
#   4. set_progress_handler で SQLite 実行中でも中断できるようにする
#      (deadline / cancel event。Future.cancel() では止まらないため)
#   5. 接続を使い回して NAS の connect コスト (2-4 秒) を毎回払わない
# ============================================================

_push_db_conn: Optional[sqlite3.Connection] = None
_push_db_conn_path: str = ""
_push_db_conn_lock = threading.RLock()
_push_db_cancel = threading.local()

# 1 分足生成に必要な列だけ。stream_data_raw は 66 列あり、
# 板 40 列と raw_json は 1 分足には一切使わない。
_PUSH_DB_WANTED_COLUMNS = (
    "symbol", "symbolname",
    "price", "current_price", "close", "close_price",
    "datetime", "received_at", "date", "time",
    "volume", "trading_volume", "trading_value",
    "opening_price", "high_price", "low_price",
)
# これが取れないと _normalize_push_ticks が成立しないので narrow を諦める。
_PUSH_DB_ESSENTIAL_ANY = (
    ("symbol", "code", "Symbol"),
    ("price", "current_price", "close", "close_price", "CurrentPrice"),
    ("datetime", "received_at", "time", "created_at"),
)


def push_db_set_cancel(event, deadline_sec: float | None = None) -> None:
    """このスレッドの PUSH DB 読み取りに中断条件を設定する。

    event.is_set() か deadline 超過で、実行中の SQLite クエリごと中断する。
    Future.cancel() は既に走っている worker を止められないので、
    「timeout 後に裏で worker が生き残る」を防ぐにはこちらが要る。
    """
    _push_db_cancel.event = event
    _push_db_cancel.deadline = (
        None if deadline_sec is None else (time.monotonic() + float(deadline_sec))
    )


def push_db_clear_cancel() -> None:
    _push_db_cancel.event = None
    _push_db_cancel.deadline = None


def _push_db_query_deadline_sec() -> float:
    """1 回のクエリの上限。0 以下で無効。

    これは「timeout を伸ばす」ものではなく、
    880 秒走り続けるクエリを打ち切るための上限。
    """
    return max(0.0, _env_float("SUMMARY_MAIN_PUSH_DB_QUERY_DEADLINE_SEC", 8.0))


def _install_progress_handler(conn: sqlite3.Connection) -> None:
    ev = getattr(_push_db_cancel, "event", None)
    dl = getattr(_push_db_cancel, "deadline", None)
    hard = _push_db_query_deadline_sec()
    hard_deadline = (time.monotonic() + hard) if hard > 0 else None
    if dl is not None and hard_deadline is not None:
        dl = min(dl, hard_deadline)
    elif dl is None:
        dl = hard_deadline
    if ev is None and dl is None:
        return

    def _handler() -> int:
        try:
            if ev is not None and ev.is_set():
                return 1
            if dl is not None and time.monotonic() > dl:
                return 1
        except Exception:
            return 0
        return 0

    try:
        # 1000 VDBE 命令ごとに判定。NAS の 1 ページ読みより十分細かい。
        conn.set_progress_handler(_handler, 1000)
    except Exception:
        logger.debug("[SUMMARY MAIN PUSH DB REFRESH] progress handler 設定に失敗", exc_info=True)


def _push_db_get_conn(path) -> sqlite3.Connection:
    """読み取り専用接続を使い回す。NAS の connect は 2-4 秒かかることがある。"""
    global _push_db_conn, _push_db_conn_path
    want = str(path)
    if _push_db_conn is not None and _push_db_conn_path == want:
        try:
            _push_db_conn.execute("SELECT 1").fetchone()
            return _push_db_conn
        except Exception:
            logger.warning("[SUMMARY MAIN PUSH DB REFRESH] 既存接続が死んでいたので張り直す", exc_info=True)
            try:
                _push_db_conn.close()
            except Exception:
                pass
            _push_db_conn = None

    timeout = max(0.05, _env_float("SUMMARY_MAIN_PUSH_DB_REFRESH_TIMEOUT_SEC", 0.8))
    conn = sqlite3.connect(want, timeout=timeout, check_same_thread=False)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=%d" % int(timeout * 1000))
        conn.execute("PRAGMA cache_size=-8000")
    except Exception:
        logger.debug("[SUMMARY MAIN PUSH DB REFRESH] pragma skipped", exc_info=True)
    _push_db_conn = conn
    _push_db_conn_path = want
    return conn


def push_db_close_conn() -> None:
    global _push_db_conn, _push_db_conn_path
    with _push_db_conn_lock:
        if _push_db_conn is not None:
            try:
                _push_db_conn.close()
            except Exception:
                pass
        _push_db_conn = None
        _push_db_conn_path = ""


def _push_db_bootstrap_rows() -> int:
    """cursor が無いときに遡る行数。full scan の代わり。"""
    return max(100, _env_int("SUMMARY_MAIN_PUSH_DB_BOOTSTRAP_ROWS", 1500))


def _push_db_narrow_select(cols: list[str]) -> tuple[str, list[str], bool]:
    """1 分足に要る列だけの SELECT 句を作る。

    戻り値: (select_list, 採用した列, narrow できたか)
    """
    lower = {str(c).lower(): c for c in cols}
    keep: list[str] = []
    for want in _PUSH_DB_WANTED_COLUMNS:
        real = lower.get(want.lower())
        if real is not None and real not in keep:
            keep.append(real)
    for group in _PUSH_DB_ESSENTIAL_ANY:
        if not any(lower.get(g.lower()) in keep for g in group):
            return "", [], False
    if not keep:
        return "", [], False
    return ", ".join(_quote(c) for c in keep), keep, True



_push_db_lock = threading.Lock()
_push_db_cache_df: Optional[pd.DataFrame] = None
_push_db_cache_at: float = 0.0
_push_db_last_rowid: Optional[int] = None


# 2026-09-30: 増分 cursor が実時間から 20 分以上遅れ、エントリーが 0 件に
#   なっていた。1 回 LIMIT 5000 行 (約 1.5 分分) しか進まない上、tick の
#   残り予算で打ち切られて成功は数分に 1 回。PUSH の書き込みに追いつけず、
#   direct 1m は「latest が 90 秒より古い」で全部捨てていた。
#   9/29 は 9 回の再起動で cursor が末尾へ seed し直されて表面化しなかった。
#   1 分足に要るのは直近の tick なので、遅れが閾値を超えたら追いかけずに
#   cursor を捨てて末尾から seed し直す (起動時と同じ経路)。
def _push_db_max_lag_sec() -> float:
    """0 以下で無効。SUMMARY_MAIN_PUSH_DB_MAX_LAG_SEC。"""
    return _env_float("SUMMARY_MAIN_PUSH_DB_MAX_LAG_SEC", 120.0)


def _push_db_reseed_if_lagging(latest, *, where: str) -> None:
    global _push_db_last_rowid
    max_lag = _push_db_max_lag_sec()
    if max_lag <= 0 or latest is None:
        return
    try:
        lag = (pd.Timestamp(dt.datetime.now()) - pd.Timestamp(latest)).total_seconds()
    except Exception:
        return
    if lag <= max_lag:
        return
    with _push_db_lock:
        old = _push_db_last_rowid
        _push_db_last_rowid = None
    logger.warning(
        "[SUMMARY MAIN PUSH DB REFRESH] cursor が %.0f 秒遅れている (上限 %.0f 秒) "
        "-> 次回は末尾から seed し直す where=%s latest=%s old_cursor=%s",
        lag, max_lag, where, latest, old,
    )
_push_db_cache_table: str = ""
_push_db_cache_path: str = ""


def _push_db_share_ttl() -> float:
    """この秒数以内の再呼び出しは同じ結果を返す (同一 tick 内の重複 open 防止)。"""
    return max(0.0, _env_float("SUMMARY_MAIN_PUSH_DB_SHARE_TTL_SEC", 5.0))


def _push_db_ring_max() -> int:
    """メモリリングバッファの上限行数。"""
    return max(500, _env_int("SUMMARY_MAIN_PUSH_DB_RING_MAX_ROWS", 20000))


def _push_db_incremental_enabled() -> bool:
    return _push_db_refresh_env_bool("SUMMARY_MAIN_PUSH_DB_INCREMENTAL", True)


class _StageTimer:
    """段ごとの elapsed を測って 1 行にまとめて出す。1 段 >1 秒で WARNING。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.marks: list = []
        self._t0 = time.perf_counter()
        self._last = self._t0
        self.slow: list = []

    def mark(self, name: str, **info) -> None:
        now = time.perf_counter()
        el = now - self._last
        self._last = now
        self.marks.append((name, el, info))
        if el >= _PUSH_DB_STAGE_WARN_SEC:
            self.slow.append((name, el, info))
            logger.warning(
                "[PUSH DB STAGE] slow stage label=%s name=%s elapsed=%.3fs %s "
                "(1 段で 1 秒以上かかっている)",
                self.label, name, el,
                " ".join("%s=%s" % kv for kv in info.items()),
            )

    @property
    def total(self) -> float:
        return time.perf_counter() - self._t0

    def emit(self, **info) -> None:
        parts = " ".join(
            "%s=%.3fs%s" % (
                n, e,
                ("(" + ",".join("%s=%s" % kv for kv in i.items()) + ")") if i else "",
            )
            for n, e, i in self.marks
        )
        log = logger.warning if (self.total >= _PUSH_DB_STAGE_WARN_SEC or self.slow) else logger.info
        log(
            "[PUSH DB STAGE] label=%s total=%.3fs stages=[%s] %s",
            self.label, self.total, parts,
            " ".join("%s=%s" % kv for kv in info.items()),
        )


def push_db_read_stats() -> dict:
    """診断用: 増分カーソルとリングバッファの状態。"""
    with _push_db_lock:
        return {
            "last_rowid": _push_db_last_rowid,
            "ring_rows": 0 if _push_db_cache_df is None else len(_push_db_cache_df),
            "table": _push_db_cache_table,
            "path": _push_db_cache_path,
            "age_sec": None if _push_db_cache_at <= 0 else round(time.monotonic() - _push_db_cache_at, 2),
        }


def reset_push_db_cursor() -> None:
    """テスト用。カーソルとリングバッファを捨てる。"""
    global _push_db_cache_df, _push_db_cache_at, _push_db_last_rowid
    global _push_db_cache_table, _push_db_cache_path
    with _push_db_lock:
        _push_db_cache_df = None
        _push_db_cache_at = 0.0
        _push_db_last_rowid = None
        _push_db_cache_table = ""
        _push_db_cache_path = ""


_PUSH_DB_PROBE_LOCK = threading.Lock()
_PUSH_DB_PROBE_AT: float = 0.0
_PUSH_DB_PROBE_RESULT: dict = {}


def push_db_probe(*, max_age_sec: float = 1.0) -> dict:
    """PUSH DB の最新状態を最小コストで返す。

    返す値:
        max_rowid / latest_datetime / latest_received_at / elapsed / cached

    2026-09-02 P0:
      旧 freshness probe は毎回 sqlite3.connect していた。NAS 上の
      631MB / WAL 196MB という状態では connect だけで 2-4 秒かかり、
      Summary の fresh_input_probe が 15.397 秒になっていた。
      共有接続 + rowid DESC LIMIT 1 の 1 クエリに統一する。
      実測 (contention 無し): connect 0.127s / MAX(rowid) 0.109s /
      rowid DESC LIMIT 1 0.028s。
      閾値 (300 秒) は変更していない。
    """
    global _PUSH_DB_PROBE_AT, _PUSH_DB_PROBE_RESULT
    now_m = time.monotonic()
    with _PUSH_DB_PROBE_LOCK:
        if _PUSH_DB_PROBE_AT > 0.0 and (now_m - _PUSH_DB_PROBE_AT) < max(0.0, max_age_sec):
            out = dict(_PUSH_DB_PROBE_RESULT)
            out["cached"] = True
            return out

    out = {
        "max_rowid": None,
        "latest_datetime": None,
        "latest_received_at": None,
        "elapsed": None,
        "cached": False,
        "error": None,
    }
    t0 = time.perf_counter()
    path = _push_db_path()
    if not path.exists():
        out["error"] = "db_missing"
        out["elapsed"] = round(time.perf_counter() - t0, 3)
        return out

    # 重い読み取りが走っている間は probe も待たせない。
    wait = max(0.05, _env_float("SUMMARY_MAIN_PUSH_DB_PROBE_WAIT_SEC", 1.5))
    if not _push_db_conn_lock.acquire(timeout=wait):
        out["error"] = "busy"
        out["elapsed"] = round(time.perf_counter() - t0, 3)
        with _PUSH_DB_PROBE_LOCK:
            if _PUSH_DB_PROBE_RESULT:
                prev = dict(_PUSH_DB_PROBE_RESULT)
                prev["cached"] = True
                prev["error"] = "busy_used_cache"
                return prev
        return out
    try:
        conn = _push_db_get_conn(path)
        _install_progress_handler(conn)
        row = conn.execute(
            "SELECT rowid, datetime, received_at FROM stream_data_raw "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if row:
            out["max_rowid"] = row[0]
            out["latest_datetime"] = row[1]
            out["latest_received_at"] = row[2]
    except Exception as exc:
        out["error"] = type(exc).__name__
        logger.debug("[PUSH DB PROBE] failed", exc_info=True)
    finally:
        try:
            if _push_db_conn is not None:
                _push_db_conn.set_progress_handler(None, 0)
        except Exception:
            pass
        try:
            _push_db_conn_lock.release()
        except Exception:
            pass

    out["elapsed"] = round(time.perf_counter() - t0, 3)
    with _PUSH_DB_PROBE_LOCK:
        _PUSH_DB_PROBE_AT = time.monotonic()
        _PUSH_DB_PROBE_RESULT = dict(out)
    return out


# ============================================================
# 2026-09-30: PUSH DB を 2 秒ごとに小さく読む専用スレッド
# ------------------------------------------------------------
# 読み取りは summary tick (約 1 分に 1 回) からしか呼ばれず、1 回で 1 分分
# (3,000-5,000 行) を読む必要があった。main.py は ranking summary が CPU の
# 68% を使っていて (py-spy 20 秒: ranking_summary_jobs._task 43% /
# _ranking_bg_task 25%)、GIL の取り合いで fetchall が行数に比例して遅くなる
# (790 行 0.04 秒 / 4,132 行 5.7 秒)。そのうえ tick の残り予算で打ち切られ、
# 末尾からの seed (1,500 行) すら中断され続けて 7 分遅れのまま止まっていた。
#
# 専用スレッドが 2 秒ごとに増分 (100 行前後) を読んで共有キャッシュを
# 新しく保つ。このスレッドには tick の中断期限を付けない (hard deadline
# のみ)。summary tick 側は TTL 内の共有キャッシュを受け取るだけになる。
# SUMMARY_MAIN_PUSH_DB_POLL_SEC=0 で無効 (従来どおり tick からだけ読む)。
# ============================================================
_push_db_poller_started = False
_push_db_poller_lock = threading.Lock()


def _push_db_poll_sec() -> float:
    return _env_float("SUMMARY_MAIN_PUSH_DB_POLL_SEC", 2.0)


def _push_db_poller_loop(interval: float) -> None:
    logger.warning("[SUMMARY MAIN PUSH DB POLLER] start interval=%.1fs version=%s", interval, VERSION)
    while True:
        try:
            _read_push_db_recent(_from_poller=True)
        except Exception:
            logger.debug("[SUMMARY MAIN PUSH DB POLLER] read failed", exc_info=True)
        time.sleep(interval)


def _ensure_push_db_poller() -> None:
    global _push_db_poller_started
    if _push_db_poller_started:
        return
    interval = _push_db_poll_sec()
    if interval <= 0:
        return
    with _push_db_poller_lock:
        if _push_db_poller_started:
            return
        _push_db_poller_started = True
        threading.Thread(
            target=_push_db_poller_loop, args=(max(0.5, interval),),
            name="push-db-poller", daemon=True,
        ).start()


def _read_push_db_recent(_from_poller: bool = False) -> pd.DataFrame:
    """PUSH DB の直近 tick を返す。

    Ver2.0 (2026-09-02 P0):
      - full scan を廃止。cursor が無ければ MAX(rowid) から seed する。
      - 1 分足に要る列だけ SELECT する (66 列 -> 11 列)。
      - set_progress_handler で SQLite 実行中でも中断できる。
      - 接続を使い回す (NAS の connect は 2-4 秒かかることがある)。
    """
    global _push_db_cache_df, _push_db_cache_at, _push_db_last_rowid
    global _push_db_cache_table, _push_db_cache_path

    if not _push_db_refresh_env_bool("SUMMARY_MAIN_PUSH_DB_REFRESH_ENABLED", True):
        return pd.DataFrame()
    path = _push_db_path()
    if not path.exists():
        logger.warning("[SUMMARY MAIN PUSH DB REFRESH] db missing path=%s version=%s", path, VERSION)
        return pd.DataFrame()

    if not _from_poller:
        _ensure_push_db_poller()

    # ---- P0-4: 同一 tick 内の重複 open を避ける ----
    ttl = 0.0 if _from_poller else _push_db_share_ttl()
    with _push_db_lock:
        cached = _push_db_cache_df
        cached_at = _push_db_cache_at
        same_path = (_push_db_cache_path == str(path))
    if (
        ttl > 0.0
        and cached is not None
        and same_path
        and (time.monotonic() - cached_at) < ttl
    ):
        logger.info(
            "[SUMMARY MAIN PUSH DB REFRESH] shared cache hit rows=%s age=%.2fs ttl=%.1fs "
            "(同一 tick 内で push DB を開き直さない)",
            len(cached), time.monotonic() - cached_at, ttl,
        )
        return cached.copy(deep=False)

    # ---- 同時に複数スレッドが同じ DB を叩かない ----
    # 待てないときは古い共有キャッシュを返す (無ければ空)。
    # 空を返しても呼び出し側の stale 判定は緩めない。
    lock_wait = max(0.1, _env_float("SUMMARY_MAIN_PUSH_DB_LOCK_WAIT_SEC", 3.0))
    if not _push_db_conn_lock.acquire(timeout=lock_wait):
        with _push_db_lock:
            cached = _push_db_cache_df
        logger.warning(
            "[SUMMARY MAIN PUSH DB REFRESH] 他スレッドが読み取り中 wait=%.1fs "
            "-> 共有キャッシュを返す rows=%s (同時 full read を作らない)",
            lock_wait, 0 if cached is None else len(cached),
        )
        return cached.copy(deep=False) if cached is not None else pd.DataFrame()

    limit = max(100, _env_int("SUMMARY_MAIN_PUSH_DB_REFRESH_LIMIT", 5000))
    tables_prefer = ["stream_data_raw", "stream_data", "push_stream", "push_ticks", "ticks"]

    st = _StageTimer("push_db_recent")
    try:
        conn = _push_db_get_conn(path)
        st.mark("connect", path=path.name)
        _install_progress_handler(conn)
        st.mark("progress_handler")

        schema_key = str(path)
        tables = _existing_tables(conn, cache_key=schema_key)
        st.mark("list_tables", n=len(tables), cached=_schema_cached(schema_key, "tables"))

        for table in tables_prefer + [t for t in tables if t not in tables_prefer]:
            if table not in tables:
                continue
            cols = _table_columns(conn, table, cache_key=schema_key)
            if not cols:
                continue
            sym_col = next((c for c in ("symbol", "code", "Symbol", "銘柄コード") if c in cols), None)
            price_col = next((c for c in ("current_price", "price", "close", "CurrentPrice", "Price", "現在値") if c in cols), None)
            if sym_col is None or price_col is None:
                continue
            st.mark("columns", table=table, n=len(cols))

            # P2 強化: 1 分足に要る列だけ引く。板 40 列と raw_json は使わない。
            select_list, kept_cols, narrow = _push_db_narrow_select(cols)
            if not narrow:
                select_list, dropped_cols = _push_db_select_list(cols)
                kept_cols = []
            else:
                dropped_cols = [c for c in cols if c not in kept_cols]

            with _push_db_lock:
                cursor_rowid = _push_db_last_rowid
                prev_df = _push_db_cache_df
                prev_table = _push_db_cache_table
                prev_path = _push_db_cache_path

            ring_usable = (
                prev_df is not None
                and not prev_df.empty
                and prev_table == table
                and prev_path == str(path)
            )
            if not ring_usable:
                cursor_rowid = None

            # ---- P0-6 改: cursor が無ければ MAX(rowid) で seed する ----
            #      full scan には絶対に戻さない。
            mode = "incremental"
            if cursor_rowid is None or not _push_db_incremental_enabled():
                try:
                    row = conn.execute(
                        "SELECT MAX(rowid) FROM %s" % _quote(table)
                    ).fetchone()
                    max_rowid_now = int(row[0]) if row and row[0] is not None else 0
                except Exception:
                    logger.warning(
                        "[SUMMARY MAIN PUSH DB REFRESH] MAX(rowid) 取得に失敗 table=%s "
                        "(WITHOUT ROWID か接続断)",
                        table, exc_info=True,
                    )
                    max_rowid_now = None
                if max_rowid_now is None:
                    st.mark("seed_failed", table=table)
                    continue
                cursor_rowid = max(0, max_rowid_now - _push_db_bootstrap_rows())
                mode = "seeded"
                ring_usable = False
                st.mark("seed_rowid", table=table, max_rowid=max_rowid_now, cursor=cursor_rowid)

            sql_inc = (
                "SELECT rowid AS _rowid_, %s FROM %s WHERE rowid > %d ORDER BY rowid ASC LIMIT %d"
                % (select_list, _quote(table), int(cursor_rowid), int(limit))
            )
            try:
                df = pd.read_sql_query(sql_inc, conn)
            except Exception as exc:
                # progress handler による中断もここに来る。full scan へは落とさない。
                st.mark("query_aborted", table=table, mode=mode)
                st.emit(result="aborted", table=table, mode=mode, err=type(exc).__name__)
                logger.warning(
                    "[SUMMARY MAIN PUSH DB REFRESH] クエリ中断/失敗 table=%s mode=%s cursor=%s "
                    "-> 前回の共有キャッシュを返す (full scan へは戻さない)",
                    table, mode, cursor_rowid, exc_info=True,
                )
                with _push_db_lock:
                    cached = _push_db_cache_df
                if cached is not None and not cached.empty:
                    try:
                        _push_db_reseed_if_lagging(_latest_dt(cached), where="aborted")
                    except Exception:
                        pass
                return cached.copy(deep=False) if cached is not None else pd.DataFrame()

            st.mark("query_fetch", table=table, mode=mode, rows=0 if df is None else len(df))

            if df is None or not isinstance(df, pd.DataFrame):
                continue

            new_rows = len(df)
            max_rowid = None
            if "_rowid_" in df.columns and new_rows:
                try:
                    max_rowid = int(pd.to_numeric(df["_rowid_"], errors="coerce").max())
                except Exception:
                    max_rowid = None
                df = df.drop(columns=["_rowid_"], errors="ignore")

            df = _normalize_dt_columns(df)

            # ---- リングバッファへ合流 ----
            if ring_usable:
                if new_rows == 0:
                    out = prev_df
                else:
                    try:
                        out = pd.concat([prev_df, df], ignore_index=True)
                    except Exception:
                        logger.warning(
                            "[SUMMARY MAIN PUSH DB REFRESH] ring concat 失敗 -> 新規分のみ使う",
                            exc_info=True,
                        )
                        out = df
            else:
                out = df

            ring_max = _push_db_ring_max()
            if isinstance(out, pd.DataFrame) and len(out) > ring_max:
                out = out.tail(ring_max).reset_index(drop=True)
            st.mark("ring_merge", mode=mode, new=new_rows,
                    ring=0 if out is None else len(out))

            if not isinstance(out, pd.DataFrame) or out.empty:
                continue

            latest = _latest_dt(out)
            st.mark("latest_dt")

            with _push_db_lock:
                _push_db_cache_df = out
                _push_db_cache_at = time.monotonic()
                _push_db_cache_table = table
                _push_db_cache_path = str(path)
                if max_rowid is not None:
                    if _push_db_last_rowid is None or max_rowid > _push_db_last_rowid:
                        _push_db_last_rowid = max_rowid
                elif mode == "seeded":
                    # 新規 0 行でも seed 位置は覚えて、次回 full には戻らない。
                    _push_db_last_rowid = int(cursor_rowid)

            st.emit(table=table, mode=mode, new_rows=new_rows, ring_rows=len(out),
                    latest=latest, last_rowid=_push_db_last_rowid,
                    cols=len(kept_cols) if kept_cols else "exclude_list")
            # ring 全体の latest ではなく、今回読んだ分の位置で遅れを測る
            if mode == "incremental" and new_rows:
                _push_db_reseed_if_lagging(_latest_dt(df), where="loaded")
            (logger.info if _from_poller else logger.warning)(
                "[SUMMARY MAIN PUSH DB REFRESH] loaded table=%s mode=%s new_rows=%s ring_rows=%s "
                "latest=%s last_rowid=%s sel_cols=%s dropped=%s path=%s version=%s",
                table, mode, new_rows, len(out), latest, _push_db_last_rowid,
                len(kept_cols) if kept_cols else "exclude_list", len(dropped_cols),
                path, VERSION,
            )
            return out.copy(deep=False)

        st.emit(result="no_usable_table")
        logger.warning("[SUMMARY MAIN PUSH DB REFRESH] no usable table path=%s tables=%s version=%s", path, tables[:20], VERSION)
        return pd.DataFrame()
    except Exception:
        st.emit(result="error")
        logger.warning("[SUMMARY MAIN PUSH DB REFRESH] db read failed path=%s version=%s", path, VERSION, exc_info=True)
        return pd.DataFrame()
    finally:
        try:
            if _push_db_conn is not None:
                _push_db_conn.set_progress_handler(None, 0)
        except Exception:
            pass
        try:
            _push_db_conn_lock.release()
        except Exception:
            pass


def _push_df_writeback_enabled() -> bool:
    try:
        v = os.getenv("SUMMARY_MAIN_PUSH_DF_WRITEBACK")
        if v is None or str(v).strip() == "":
            return True
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return True


def _writeback_push_df(db_df: pd.DataFrame, *, memory_stale, db_stale) -> None:
    """PUSH DB から読んだ新しいフレームを global_data.push_df へ書き戻す。

    main.py は自前の PUSH WebSocket を張らないので push_df を書く担当が
    居ない。書き戻さないと push_df を直接読む経路 (push_summary_engine /
    direct_push_1m / push_raw_1m_source) が古いフレームを掴み続ける。
    古いもので上書きしないよう latest_dt を比較する。
    """
    if not _push_df_writeback_enabled():
        return
    if not isinstance(db_df, pd.DataFrame) or db_df.empty:
        return
    # 2026-09-02 P0: timeout 済みの古い worker が数百秒後に生き返って
    # global_data.push_df を上書きするのを止める。
    try:
        from scheduler_jobs.summary.direct_push_1m import _worker_generation_is_current

        if not _worker_generation_is_current():
            logger.warning(
                "[SUMMARY MAIN PUSH DF WRITEBACK][STALE WORKER BLOCKED] "
                "古い世代の worker からの書き戻しを止めた rows=%s",
                len(db_df),
            )
            return
    except Exception:
        logger.debug("[SUMMARY MAIN PUSH DF WRITEBACK] 世代確認に失敗", exc_info=True)
    gd = _global_data()
    if gd is None:
        return
    try:
        cur = getattr(gd, "push_df", None)
        cur_latest = _latest_dt(cur) if isinstance(cur, pd.DataFrame) else None
        new_latest = _latest_dt(db_df)
        if new_latest is None:
            logger.warning(
                "[SUMMARY MAIN PUSH DF WRITEBACK] skipped reason=db_latest_unknown rows=%s",
                len(db_df),
            )
            return
        if cur_latest is not None and new_latest <= cur_latest:
            logger.info(
                "[SUMMARY MAIN PUSH DF WRITEBACK] skipped reason=not_newer "
                "current=%s db=%s", cur_latest, new_latest,
            )
            return
        setattr(gd, "push_df", db_df)
        logger.warning(
            "[SUMMARY MAIN PUSH DF WRITEBACK] global_data.push_df 更新 rows=%s "
            "latest %s -> %s memory_stale=%s db_stale=%s "
            "(main.py は PUSH WS を張らないので DB から書き戻す)",
            len(db_df), cur_latest, new_latest,
            None if memory_stale is None else round(float(memory_stale), 1),
            None if db_stale is None else round(float(db_stale), 1),
        )
    except Exception:
        logger.exception("[SUMMARY MAIN PUSH DF WRITEBACK] failed rows=%s", len(db_df))


def _load_push_memory_df() -> pd.DataFrame:
    """PUSH メモリ DF を返す。main では stale 時に PUSH DB から取り直す。"""
    base = pd.DataFrame()
    try:
        base = _load_push_memory_df_core()
    except Exception:
        logger.warning("[SUMMARY MAIN PUSH DB REFRESH] original memory loader failed", exc_info=True)

    if not _push_db_refresh_enabled():
        return base

    # 2026-10-19: 75 -> 10。毎分 :00 の 1 分足作成時、前回の書き戻しから 60 秒しか経っていないと
    #   memory (最新ティック = 前の分の :59) をそのまま使い、直前の分の足が無いまま stale 判定 (120 秒) で
    #   捨てていた。書き戻しが 2 分に 1 回になり、奇数分のエントリー判定が丸ごと止まっていた (10/19 は 134 回中 111 回)。
    max_stale = max(10.0, _env_float("SUMMARY_MAIN_PUSH_DB_REFRESH_MAX_STALE_SEC", 10.0))
    stale = _stale_sec(base)
    if isinstance(base, pd.DataFrame) and not base.empty and stale is not None and stale <= max_stale:
        return base

    db_df = _read_push_db_recent()
    if db_df.empty:
        logger.warning(
            "[SUMMARY MAIN PUSH DB REFRESH] keep memory because db empty memory_rows=%s memory_stale=%s max_stale=%.1f",
            len(base) if isinstance(base, pd.DataFrame) else 0,
            None if stale is None else round(float(stale), 1),
            max_stale,
        )
        return base

    db_stale = _stale_sec(db_df)
    logger.warning(
        "[SUMMARY MAIN PUSH DB REFRESH] using db rows=%s db_stale=%s memory_rows=%s memory_stale=%s max_stale=%.1f",
        len(db_df),
        None if db_stale is None else round(float(db_stale), 1),
        len(base) if isinstance(base, pd.DataFrame) else 0,
        None if stale is None else round(float(stale), 1),
        max_stale,
    )
    # 読み直したものを push_df へ書き戻す。これをしないと push_df を
    # 直接読む経路が古いフレームを掴み続ける (実測 2016 秒古いまま)。
    _writeback_push_df(db_df, memory_stale=stale, db_stale=db_stale)
    return db_df


__all__ = [
    "VERSION", "install", "_build_memory_1m_summary", "_load_push_memory_df",
    "push_db_read_stats", "push_db_set_cancel", "push_db_clear_cancel",
    "push_db_close_conn", "reset_push_db_cursor", "push_db_probe",
]
