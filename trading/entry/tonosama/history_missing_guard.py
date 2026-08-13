# ============================================================
# File   : trading/entry/tonosama/history_missing_guard.py
# Version: V5.3-PUSH-RAW-DB-CACHED-RESAMPLE-TIMEBOX
# ------------------------------------------------------------
# 目的:
#   1) 3m/5m summary が no-ratio の場合、pushYYYYMMDD.db の
#      直近PUSH raw履歴から 1m履歴を作り、3m/5mへresampleして
#      本物の volume_surge_ratio を復旧する。
#   2) PUSH raw DB履歴はTonosama実行内で1回だけ読み、3m/5mで共有する。
#   3) DB読込・変換を軽量化し、Tonosama timeout/thread詰まりを抑える。
#   4) ratio_nonnull=0 の summary_db/failopen 結果は本物として採用しない。
#   5) history_missing/failopen 行は低出来高・低変動ならDROPする。
# ============================================================

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}

_RAW1_CACHE: dict[str, Any] = {"ts": 0.0, "df": pd.DataFrame(), "key": None}
_RESAMPLE_CACHE: dict[tuple[int, str | None], tuple[float, pd.DataFrame]] = {}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(str(raw).replace(",", "")))
    except Exception:
        return int(default)


def _setdefault_env(name: str, value: str) -> None:
    try:
        cur = os.getenv(name)
        if cur is None or str(cur).strip() == "":
            os.environ[name] = str(value)
            logger.warning("[TONOSAMA HISTORY GUARD] env default set %s=%s", name, value)
    except Exception:
        pass


def _set_env(name: str, value: str) -> None:
    try:
        old = os.getenv(name)
        os.environ[name] = str(value)
        if str(old) != str(value):
            logger.warning("[TONOSAMA HISTORY GUARD] env forced %s: %s -> %s", name, old, value)
    except Exception:
        pass


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _push_db_path() -> str:
    base = os.getenv(
        "PUSH_DB_DIR",
        os.getenv("RAW_PUSH_DIR", r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\push"),
    )
    return os.getenv("PUSH_DB_PATH", str(Path(base) / f"push{_today()}.db"))


def _qident(name: Any) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
    except Exception:
        return False


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({_qident(table)})").fetchall()}
    except Exception:
        return set()


def _enable_controlled_history_fallback_defaults() -> None:
    if _env_bool("TONOSAMA_FORCE_HISTORY_FAILCLOSE", False):
        logger.warning("[TONOSAMA HISTORY GUARD] explicit fail-close requested by env")
        return
    _set_env("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING", "1")
    _set_env("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY", "1")
    _set_env("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", "1")
    _set_env("TONOSAMA_DROP_HISTORY_MISSING_ENTRY", "0")
    _setdefault_env("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE", "3.0")
    _setdefault_env("TONOSAMA_RAW1_RESAMPLE_FALLBACK", "1")
    _setdefault_env("TONOSAMA_HISTORY_MISSING_QUALITY_GUARD", "1")
    _setdefault_env("TONOSAMA_RAW1_HISTORY_RESAMPLE", "1")
    _setdefault_env("TONOSAMA_RAW1_HISTORY_MIN_BARS", "8")
    _setdefault_env("TONOSAMA_RAW1_HISTORY_MIN_BARS_3M", "8")
    _setdefault_env("TONOSAMA_RAW1_HISTORY_MIN_BARS_5M", "5")
    _setdefault_env("TONOSAMA_SURGE_RATIO_MIN_PERIODS", "1")
    _setdefault_env("TONOSAMA_PUSH_RAW_DB_HISTORY_ENABLED", "1")
    _setdefault_env("TONOSAMA_PUSH_RAW_DB_HISTORY_LOOKBACK_MIN", "30")
    _setdefault_env("TONOSAMA_PUSH_RAW_DB_HISTORY_LIMIT", "20000")
    _setdefault_env("TONOSAMA_PUSH_RAW_DB_HISTORY_CACHE_SEC", "8")
    _setdefault_env("TONOSAMA_PUSH_RAW_DB_HISTORY_TIMEOUT_SEC", "0.35")
    _setdefault_env("TONOSAMA_PUSH_RAW_DB_HISTORY_BUSY_TIMEOUT_MS", "150")
    _setdefault_env("TONOSAMA_PUSH_RAW_DB_HISTORY_MAX_ELAPSED_SEC", "2.0")


def _raw1_min_bars_for_interval(interval: int) -> int:
    try:
        interval_i = int(interval)
    except Exception:
        interval_i = 1
    if interval_i == 5:
        return max(1, _env_int("TONOSAMA_RAW1_HISTORY_MIN_BARS_5M", 5))
    if interval_i == 3:
        return max(1, _env_int("TONOSAMA_RAW1_HISTORY_MIN_BARS_3M", 8))
    return max(1, _env_int("TONOSAMA_RAW1_HISTORY_MIN_BARS", 8))




def _first_existing(df: pd.DataFrame, names: list[str]) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def _safe_df(obj: Any) -> pd.DataFrame:
    return obj if isinstance(obj, pd.DataFrame) else pd.DataFrame()


def _num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if df is None or df.empty or col not in df.columns:
            return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
        return pd.to_numeric(df[col], errors="coerce").fillna(default)
    except Exception:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")


def _max_existing_numeric(df: pd.DataFrame, names: list[str], default: float = 0.0) -> pd.Series:
    try:
        found = [n for n in names if n in df.columns]
        if not found:
            return pd.Series(default, index=df.index, dtype="float64")
        vals = [_num(df, n, default) for n in found]
        return pd.concat(vals, axis=1).max(axis=1).fillna(default)
    except Exception:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")


def _cache_key_for_push_db(path: str, now_i: dt.datetime, lookback_min: int, limit: int) -> str:
    return f"{path}|{now_i.strftime('%Y%m%d%H%M')}|{lookback_min}|{limit}"


def _load_push_raw_db_1m_history() -> pd.DataFrame:
    if not _env_bool("TONOSAMA_PUSH_RAW_DB_HISTORY_ENABLED", True):
        return pd.DataFrame()

    started = time.monotonic()
    path = _push_db_path()
    p = Path(path)
    if not p.exists():
        logger.warning("[TONOSAMA PUSH RAW DB HISTORY] db not found path=%s", path)
        return pd.DataFrame()

    now_i = dt.datetime.now().replace(second=0, microsecond=0)
    lookback_min = max(10, _env_int("TONOSAMA_PUSH_RAW_DB_HISTORY_LOOKBACK_MIN", 30))
    limit = max(1000, _env_int("TONOSAMA_PUSH_RAW_DB_HISTORY_LIMIT", 20000))
    cache_sec = max(0.0, _env_float("TONOSAMA_PUSH_RAW_DB_HISTORY_CACHE_SEC", 8.0))
    max_elapsed = max(0.2, _env_float("TONOSAMA_PUSH_RAW_DB_HISTORY_MAX_ELAPSED_SEC", 2.0))
    key = _cache_key_for_push_db(path, now_i, lookback_min, limit)

    try:
        if _RAW1_CACHE.get("key") == key and (time.monotonic() - float(_RAW1_CACHE.get("ts") or 0)) <= cache_sec:
            cached = _safe_df(_RAW1_CACHE.get("df"))
            logger.warning(
                "[TONOSAMA PUSH RAW DB HISTORY] cache hit rows=%s symbols=%s key_minute=%s",
                len(cached),
                cached["symbol"].nunique() if not cached.empty and "symbol" in cached.columns else 0,
                now_i.strftime("%H:%M"),
            )
            return cached.copy()
    except Exception:
        pass

    since = now_i - dt.timedelta(minutes=lookback_min)
    timeout_sec = max(0.05, _env_float("TONOSAMA_PUSH_RAW_DB_HISTORY_TIMEOUT_SEC", 0.35))
    busy_ms = int(max(50, _env_float("TONOSAMA_PUSH_RAW_DB_HISTORY_BUSY_TIMEOUT_MS", 150)))

    try:
        with sqlite3.connect(str(p), timeout=timeout_sec) as conn:
            conn.execute(f"PRAGMA busy_timeout={busy_ms};")
            conn.execute("PRAGMA query_only=ON;")
            table = "stream_data_raw" if _table_exists(conn, "stream_data_raw") else "stream_data"
            cols = _table_cols(conn, table)
            if not {"symbol", "datetime", "price"}.issubset(cols):
                logger.warning("[TONOSAMA PUSH RAW DB HISTORY] required cols missing table=%s cols=%s path=%s", table, sorted(cols), path)
                return pd.DataFrame()

            wanted = ["symbol", "symbolname", "datetime", "date", "time", "price", "volume", "trading_value", "received_at"]
            select_cols = [c for c in wanted if c in cols]
            dt_col = "received_at" if "received_at" in cols else "datetime"

            where_parts: list[str] = []
            params: list[Any] = []
            if "date" in cols:
                where_parts.append("date = ?")
                params.append(now_i.strftime("%Y-%m-%d"))
            where_parts.append(f"{_qident(dt_col)} >= ?")
            params.append(since.isoformat())
            where = " AND ".join(where_parts)

            sql = (
                f"SELECT {','.join(_qident(c) for c in select_cols)} "
                f"FROM {_qident(table)} WHERE {where} "
                f"ORDER BY {_qident(dt_col)} DESC LIMIT ?"
            )
            params.append(limit)
            df = pd.read_sql_query(sql, conn, params=params)
    except Exception:
        logger.debug("[TONOSAMA PUSH RAW DB HISTORY] load failed path=%s", path, exc_info=True)
        return pd.DataFrame()

    elapsed_load = time.monotonic() - started
    if elapsed_load > max_elapsed:
        logger.warning(
            "[TONOSAMA PUSH RAW DB HISTORY] load over budget -> empty elapsed=%.3fs max=%.3fs rows=%s path=%s",
            elapsed_load, max_elapsed, len(df) if isinstance(df, pd.DataFrame) else 0, path,
        )
        return pd.DataFrame()

    if df.empty:
        logger.warning("[TONOSAMA PUSH RAW DB HISTORY] empty path=%s since=%s lookback_min=%s", path, since, lookback_min)
        _RAW1_CACHE.update({"ts": time.monotonic(), "df": pd.DataFrame(), "key": key})
        return df

    try:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime", "symbol"])
        df["symbol"] = df["symbol"].astype(str).str.strip()
        df = df[df["symbol"] != ""].copy()
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df = df.dropna(subset=["price"])
        df = df[df["price"] > 0].copy()
        if df.empty:
            return pd.DataFrame()

        max_symbols = max(30, _env_int("TONOSAMA_PUSH_RAW_DB_HISTORY_MAX_SYMBOLS", 120))
        if df["symbol"].nunique() > max_symbols:
            top_symbols = df.groupby("symbol").size().sort_values(ascending=False).head(max_symbols).index
            df = df[df["symbol"].isin(top_symbols)].copy()

        df["slot"] = df["datetime"].dt.floor("1min")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0) if "volume" in df.columns else 0.0
        df["trading_value"] = pd.to_numeric(df["trading_value"], errors="coerce").fillna(0.0) if "trading_value" in df.columns else 0.0
        if "symbolname" not in df.columns:
            df["symbolname"] = ""

        df = df.sort_values(["symbol", "slot", "datetime"])
        grouped = df.groupby(["symbol", "slot"], sort=False)
        out = pd.DataFrame({
            "symbol": grouped["symbol"].last(),
            "symbolname": grouped["symbolname"].last(),
            "datetime": grouped["slot"].last(),
            "open": grouped["price"].first(),
            "high": grouped["price"].max(),
            "low": grouped["price"].min(),
            "close": grouped["price"].last(),
            "cum_volume": grouped["volume"].max(),
            "cum_trading_value": grouped["trading_value"].max(),
        }).reset_index(drop=True)

        out = out.sort_values(["symbol", "datetime"])
        out["volume"] = out.groupby("symbol")["cum_volume"].diff().fillna(0.0)
        out.loc[(out["volume"] < 0) | out["volume"].isna(), "volume"] = 0.0
        raw_positive = pd.to_numeric(out["cum_volume"], errors="coerce").fillna(0.0) > 0
        delta_positive = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0) > 0
        if int(delta_positive.sum()) < max(3, int(raw_positive.sum() * 0.05)):
            out["volume"] = pd.to_numeric(out["cum_volume"], errors="coerce").fillna(0.0)
            volume_mode = "raw_max"
        else:
            volume_mode = "cum_diff"

        out["trading_value"] = out.groupby("symbol")["cum_trading_value"].diff().fillna(0.0)
        out.loc[out["trading_value"] < 0, "trading_value"] = 0.0
        out["price"] = out["close"]
        out["current_price"] = out["close"]
        out["open_price"] = out["open"]
        out["high_price"] = out["high"]
        out["low_price"] = out["low"]
        out["close_price"] = out["close"]
        out["interval"] = 1
        out["source"] = "tonosama_push_raw_db_history_1m"
        out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
        out["time"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%H:%M:%S")
        out = out.dropna(subset=["datetime", "close"]).reset_index(drop=True)

        _RAW1_CACHE.update({"ts": time.monotonic(), "df": out.copy(), "key": key})
        logger.warning(
            "[TONOSAMA PUSH RAW DB HISTORY] loaded v5.3 rows=%s symbols=%s latest=%s lookback_min=%s limit=%s volume_nonzero=%s volume_mode=%s elapsed=%.3fs path=%s",
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
            lookback_min,
            limit,
            int((pd.to_numeric(out["volume"], errors="coerce").fillna(0) > 0).sum()) if "volume" in out.columns else 0,
            volume_mode,
            time.monotonic() - started,
            path,
        )
        return out.copy()
    except Exception:
        logger.exception("[TONOSAMA PUSH RAW DB HISTORY] transform failed path=%s", path)
        return pd.DataFrame()


def _call_gc_history(interval: int) -> pd.DataFrame:
    try:
        from core.global_context.context import global_context as GC
        fn = getattr(GC, "get_summary_history", None)
        if callable(fn):
            try:
                return _safe_df(fn(int(interval), source="push"))
            except TypeError:
                return _safe_df(fn(int(interval)))
    except Exception:
        logger.debug("[TONOSAMA RAW1 HISTORY] global_context history failed", exc_info=True)
    return pd.DataFrame()


def _call_global_data_history(interval: int) -> pd.DataFrame:
    try:
        from global_state import global_data
        fn = getattr(global_data, "get_summary_history", None)
        if callable(fn):
            try:
                return _safe_df(fn(int(interval), source="push"))
            except TypeError:
                return _safe_df(fn(int(interval)))
    except Exception:
        logger.debug("[TONOSAMA RAW1 HISTORY] global_data history failed", exc_info=True)
    return pd.DataFrame()


def _load_raw1_history_core(vs: Any, *, interval: int = 1) -> pd.DataFrame:
    frames: list[tuple[str, pd.DataFrame]] = []
    if _env_bool("TONOSAMA_RAW1_HISTORY_RESAMPLE", True):
        frames.append(("push_raw_db_history", _load_push_raw_db_1m_history()))
        frames.append(("global_context_history", _call_gc_history(1)))
        frames.append(("global_data_history", _call_global_data_history(1)))
    try:
        frames.append(("merged_latest", _safe_df(vs.load_merged_summary(1))))
    except Exception:
        logger.debug("[TONOSAMA RAW1 HISTORY] merged latest load failed", exc_info=True)

    norm_parts: list[pd.DataFrame] = []
    stats: list[dict[str, Any]] = []
    for label, df in frames:
        try:
            if df is None or df.empty:
                stats.append({"source": label, "rows": 0, "symbols": 0, "latest": None})
                continue
            x = vs.normalize_summary_base(df, interval=1)
            if x is None or x.empty or "datetime" not in x.columns:
                stats.append({"source": label, "rows": len(df), "symbols": 0, "latest": None, "normalized": 0})
                continue
            x = x.copy()
            x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
            x = x.dropna(subset=["symbol", "datetime"])
            if x.empty:
                continue
            x["_raw1_source"] = label
            norm_parts.append(x)
            stats.append({"source": label, "rows": len(x), "symbols": int(x["symbol"].nunique()) if "symbol" in x.columns else 0, "latest": str(x["datetime"].max())})
        except Exception:
            logger.debug("[TONOSAMA RAW1 HISTORY] normalize failed source=%s", label, exc_info=True)

    if not norm_parts:
        logger.warning("[TONOSAMA RAW1 HISTORY] no usable 1m history interval=%s stats=%s", interval, stats)
        return pd.DataFrame()

    out = pd.concat(norm_parts, ignore_index=True, sort=False)
    out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last").sort_values(["symbol", "datetime"])
    min_bars = _raw1_min_bars_for_interval(interval)
    counts = out.groupby("symbol")["datetime"].transform("count") if not out.empty else pd.Series(dtype="int64")
    enough = out[counts >= min_bars].copy()
    if enough.empty:
        logger.warning("[TONOSAMA RAW1 HISTORY] insufficient 1m history interval=%s rows=%s symbols=%s min_bars=%s stats=%s", interval, len(out), int(out["symbol"].nunique()) if "symbol" in out.columns else 0, min_bars, stats)
        return out

    logger.warning("[TONOSAMA RAW1 HISTORY] loaded interval=%s rows=%s symbols=%s usable_rows=%s usable_symbols=%s min_bars=%s latest=%s stats=%s", interval, len(out), int(out["symbol"].nunique()) if "symbol" in out.columns else 0, len(enough), int(enough["symbol"].nunique()) if "symbol" in enough.columns else 0, min_bars, enough["datetime"].max(), stats)
    return enough


def _resample_1m_to_interval_core(vs: Any, interval: int) -> pd.DataFrame:
    try:
        if not _env_bool("TONOSAMA_RAW1_RESAMPLE_FALLBACK", True):
            return pd.DataFrame()
        interval_i = int(interval)

        raw1_key = None
        try:
            raw1_key = str(_RAW1_CACHE.get("key"))
        except Exception:
            raw1_key = None
        cache_key = (interval_i, raw1_key)
        cache_sec = max(0.0, _env_float("TONOSAMA_PUSH_RAW_DB_HISTORY_CACHE_SEC", 8.0))
        cached_tuple = _RESAMPLE_CACHE.get(cache_key)
        if cached_tuple and (time.monotonic() - cached_tuple[0]) <= cache_sec:
            cached = cached_tuple[1]
            logger.warning(
                "[TONOSAMA RAW1 RESAMPLE] cache hit interval=%sm rows=%s symbols=%s",
                interval_i, len(cached), cached["symbol"].nunique() if not cached.empty and "symbol" in cached.columns else 0,
            )
            return cached.copy()

        raw1 = _load_raw1_history(vs, interval=interval_i)
        if raw1 is None or raw1.empty or "datetime" not in raw1.columns:
            return pd.DataFrame()

        x = raw1.copy()
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime"])
        if x.empty:
            return pd.DataFrame()

        open_col = _first_existing(x, ["open", "open_price", "close", "close_price"])
        high_col = _first_existing(x, ["high", "high_price", "close", "close_price"])
        low_col = _first_existing(x, ["low", "low_price", "close", "close_price"])
        close_col = _first_existing(x, ["close", "close_price", "current_price", "price"])
        volume_col = _first_existing(x, ["volume", "trading_volume", "latest_volume"])
        if close_col is None:
            return pd.DataFrame()

        x["open"] = pd.to_numeric(x[open_col], errors="coerce") if open_col else pd.to_numeric(x[close_col], errors="coerce")
        x["high"] = pd.to_numeric(x[high_col], errors="coerce") if high_col else pd.to_numeric(x[close_col], errors="coerce")
        x["low"] = pd.to_numeric(x[low_col], errors="coerce") if low_col else pd.to_numeric(x[close_col], errors="coerce")
        x["close"] = pd.to_numeric(x[close_col], errors="coerce")
        x["volume"] = pd.to_numeric(x[volume_col], errors="coerce").fillna(0.0) if volume_col else 0.0
        x = x.dropna(subset=["close"])
        if x.empty:
            return pd.DataFrame()

        rule = f"{interval_i}min"
        out = (
            x.sort_values(["symbol", "datetime"])
            .groupby(["symbol", pd.Grouper(key="datetime", freq=rule, label="right", closed="right")], sort=False)
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
                symbolname=("symbolname", "last") if "symbolname" in x.columns else ("symbol", "last"),
            )
            .reset_index()
            .dropna(subset=["close"])
        )
        if out.empty:
            return pd.DataFrame()
        out["source"] = f"tonosama_raw1_history_resample_{interval_i}m"
        out["interval"] = interval_i
        _RESAMPLE_CACHE[cache_key] = (time.monotonic(), out.copy())

        logger.warning(
            "[TONOSAMA RAW1 RESAMPLE] built interval=%sm rows=%s symbols=%s latest=%s volume_nonzero=%s raw1_min_bars=%s",
            interval_i,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            out["datetime"].max() if "datetime" in out.columns else None,
            int((pd.to_numeric(out.get("volume", 0), errors="coerce").fillna(0) > 0).sum()) if "volume" in out.columns else 0,
            _raw1_min_bars_for_interval(interval_i),
        )
        return out
    except Exception:
        logger.exception("[TONOSAMA RAW1 RESAMPLE] failed interval=%s", interval)
        return pd.DataFrame()


def _has_real_surge_ratio(df: pd.DataFrame, interval: int) -> bool:
    try:
        if df is None or df.empty:
            return False
        ratio_col = f"volume_surge_ratio_{int(interval)}m"
        if ratio_col not in df.columns:
            return False
        return bool(pd.to_numeric(df[ratio_col], errors="coerce").notna().any())
    except Exception:
        return False


def _compute_surge_features_core(vs: Any, df: pd.DataFrame, *, interval: int, source_label: str) -> pd.DataFrame:
    try:
        x = vs.normalize_summary_base(df, interval=interval)
        if x is None or x.empty:
            return pd.DataFrame()

        interval_i = int(interval)
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime"]).sort_values(["symbol", "datetime"])
        if x.empty:
            return pd.DataFrame()

        g = x.groupby("symbol", group_keys=False)
        avg_col = f"prev{vs.VOLUME_AVG_LOOKBACK_BARS}_volume_avg_{interval_i}m"
        ratio_col = f"volume_surge_ratio_{interval_i}m"
        prev_close_col = f"prev_close_{interval_i}m"
        price_chg_col = f"price_change_pct_{interval_i}m"
        up_streak_col = f"prev_{interval_i}m_up_streak"
        down_streak_col = f"prev_{interval_i}m_down_streak"
        last_delta_col = f"prev_{interval_i}m_last_delta_pct"

        x["volume"] = pd.to_numeric(x["volume"], errors="coerce").fillna(0.0) if "volume" in x.columns else 0.0
        x["close"] = pd.to_numeric(x["close"], errors="coerce")
        min_periods = max(1, _env_int("TONOSAMA_SURGE_RATIO_MIN_PERIODS", 1))
        x[avg_col] = g["volume"].transform(lambda s: s.shift(1).rolling(vs.VOLUME_AVG_LOOKBACK_BARS, min_periods=min_periods).mean())
        x[ratio_col] = pd.to_numeric(x["volume"] / x[avg_col].replace(0, pd.NA), errors="coerce").replace([float("inf"), -float("inf")], pd.NA)
        x[prev_close_col] = g["close"].shift(1)
        x[price_chg_col] = ((x["close"] - x[prev_close_col]) / x[prev_close_col].replace(0, pd.NA) * 100.0)
        x[price_chg_col] = pd.to_numeric(x[price_chg_col], errors="coerce").replace([float("inf"), -float("inf")], pd.NA)
        x[last_delta_col] = x[price_chg_col].fillna(0.0)
        x[f"_is_{interval_i}m_up"] = pd.to_numeric(x["close"], errors="coerce") > pd.to_numeric(x[prev_close_col], errors="coerce")
        x[f"_is_{interval_i}m_down"] = pd.to_numeric(x["close"], errors="coerce") < pd.to_numeric(x[prev_close_col], errors="coerce")
        x[up_streak_col] = g[f"_is_{interval_i}m_up"].apply(vs._consecutive_true_counts)
        x[down_streak_col] = g[f"_is_{interval_i}m_down"].apply(vs._consecutive_true_counts)

        if x[price_chg_col].isna().all():
            fallback_chg = vs._intrabar_price_change_pct(x, interval_i)
            if fallback_chg.notna().any():
                x[price_chg_col] = fallback_chg
                logger.warning("[TONOSAMA SURGE ROLLING PATCH] price_change fallback open_to_close interval=%sm rows=%s nonnull=%s", interval_i, len(x), int(fallback_chg.notna().sum()))

        recent = vs._filter_recent_rows(x, interval=interval_i, label=f"feature_latest_after_rolling:{source_label}")
        if recent.empty:
            return pd.DataFrame()
        latest = recent.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"]).groupby("symbol", group_keys=False).tail(1)

        keep_cols = ["symbol", "datetime", "close", "volume", avg_col, ratio_col, prev_close_col, price_chg_col, up_streak_col, down_streak_col, last_delta_col]
        for c in keep_cols:
            if c not in latest.columns:
                latest[c] = pd.NA
        latest = latest[keep_cols].copy().rename(columns={"datetime": f"datetime_{interval_i}m", "close": f"close_{interval_i}m", "volume": f"volume_{interval_i}m"})
        for c in [up_streak_col, down_streak_col]:
            latest[c] = pd.to_numeric(latest[c], errors="coerce").fillna(0).astype(int)
        latest[last_delta_col] = pd.to_numeric(latest[last_delta_col], errors="coerce").fillna(0.0)

        ratio_nonnull = int(pd.to_numeric(latest[ratio_col], errors="coerce").notna().sum()) if ratio_col in latest.columns else 0
        logger.warning(
            "[TONOSAMA SURGE ROLLING PATCH] %sm latest rows=%s source=%s ratio_nonnull=%s up_ge3=%s down_ge3=%s head=%s",
            interval_i,
            len(latest),
            source_label,
            ratio_nonnull,
            int((latest[up_streak_col] >= 3).sum()),
            int((latest[down_streak_col] >= 3).sum()),
            latest[[c for c in ["symbol", f"close_{interval_i}m", f"volume_{interval_i}m", avg_col, up_streak_col, down_streak_col, last_delta_col, ratio_col, price_chg_col] if c in latest.columns]].head(8).to_dict("records"),
        )
        return latest.reset_index(drop=True)
    except Exception:
        logger.exception("[TONOSAMA SURGE ROLLING PATCH] compute failed interval=%s source=%s", interval, source_label)
        return pd.DataFrame()


def _orig_add_volume_surge_features():
    """旧 _ORIGINAL_ADD_SURGE。volume_surge の素の実装を遅延解決する。"""
    try:
        import trading.entry.tonosama.volume_surge as vs

        return getattr(vs, "_add_volume_surge_features_core", None)
    except Exception:
        logger.exception("[TONOSAMA SURGE ROLLING PATCH] original add_volume_surge_features missing")
        return None


def add_volume_surge_features_guarded(df: "pd.DataFrame", *, interval: int) -> "pd.DataFrame":
    """旧 _patched_add_volume_surge_features (vs.add_volume_surge_features 差し替え)。

    summary_db 由来で surge ratio が取れないときに raw1 履歴の resample から
    作り直す。作り直しても実 ratio が無ければ空を返して failopen を防ぐ。
    """
    import trading.entry.tonosama.volume_surge as vs

    _orig = _orig_add_volume_surge_features()
    interval_i = int(interval)
    try:
        out = _compute_surge_features(vs, df, interval=interval_i, source_label="summary_db")
        if not out.empty and _has_real_surge_ratio(out, interval_i):
            return out
        if not out.empty:
            logger.warning("[TONOSAMA SURGE ROLLING PATCH] ignore no-ratio summary_db interval=%sm rows=%s", interval_i, len(out))

        if interval_i in (3, 5):
            rebuilt = _resample_1m_to_interval(vs, interval_i)
            out = _compute_surge_features(vs, rebuilt, interval=interval_i, source_label="raw1_history_resample")
            if not out.empty and _has_real_surge_ratio(out, interval_i):
                logger.warning("[TONOSAMA SURGE ROLLING PATCH] recovered %sm surge ratio from raw1 history rows=%s", interval_i, len(out))
                return out
            if not out.empty:
                logger.warning("[TONOSAMA SURGE ROLLING PATCH] raw1 history resample has no real ratio interval=%sm rows=%s -> empty", interval_i, len(out))
                return pd.DataFrame()

        original = _orig(df, interval=interval_i) if callable(_orig) else pd.DataFrame()
        if not original.empty and not _has_real_surge_ratio(original, interval_i):
            logger.warning("[TONOSAMA SURGE ROLLING PATCH] suppress original no-ratio failopen source interval=%sm rows=%s", interval_i, len(original))
            return pd.DataFrame()
        return original
    except Exception:
        logger.exception("[TONOSAMA SURGE ROLLING PATCH] patched add_volume_surge_features failed interval=%s", interval_i)
        return _orig(df, interval=interval_i) if callable(_orig) else pd.DataFrame()


def _sample(df: pd.DataFrame, limit: int = 8) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    cols = [c for c in ["symbol", "symbolname", "close", "volume", "_latest_volume", "_max_volume_surge_ratio", "_max_price_change_pct", "_surge_tf", "_volume_surge_history_missing", "_volume_surge_failopen", "_body_change_pct", "_intrabar_range_pct", "_slope", "price_change_5s_pct", "volume_surge_ratio_5s", "_history_failopen_turnover", "_history_failopen_quality_ok"] if c in df.columns]
    out: list[dict[str, Any]] = []
    for _, row in df.head(limit).iterrows():
        item: dict[str, Any] = {}
        for c in cols:
            v = row.get(c)
            try:
                if pd.isna(v):
                    v = None
            except Exception:
                pass
            if isinstance(v, float):
                v = round(v, 6)
            item[c] = v
        out.append(item)
    return out


def _history_missing_quality_mask(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(False, index=df.index if df is not None else None, dtype="bool")

    latest_vol = _max_existing_numeric(df, ["_latest_volume", "latest_volume", "volume", "volume_1m", "volume_3m", "volume_5m"], 0.0)
    close = _max_existing_numeric(df, ["close", "close_price", "current_price", "price"], 0.0)
    turnover = latest_vol * close
    price_abs = _num(df, "_max_price_change_pct", 0.0).abs()
    body_abs = _num(df, "_body_change_pct", 0.0).abs()
    range_abs = _num(df, "_intrabar_range_pct", 0.0).abs()
    five_abs = _num(df, "price_change_5s_pct", 0.0).abs()
    five_surge = _num(df, "volume_surge_ratio_5s", 0.0)

    min_volume = _env_float("TONOSAMA_HISTORY_MISSING_MIN_VOLUME", 500000.0)
    min_turnover = _env_float("TONOSAMA_HISTORY_MISSING_MIN_TURNOVER", 10000000.0)
    min_price = _env_float("TONOSAMA_HISTORY_MISSING_MIN_PRICE_CHANGE_PCT", 0.50)
    min_body = _env_float("TONOSAMA_HISTORY_MISSING_MIN_BODY_PCT", 0.30)
    min_range = _env_float("TONOSAMA_HISTORY_MISSING_MIN_RANGE_PCT", 1.00)
    min_5sec = _env_float("TONOSAMA_HISTORY_MISSING_MIN_5SEC_CHANGE_PCT", 0.05)
    min_5sec_surge = _env_float("TONOSAMA_HISTORY_MISSING_MIN_5SEC_SURGE", 1.20)

    liquidity_ok = (latest_vol >= min_volume) & (turnover >= min_turnover)
    movement_ok = (price_abs >= min_price) | (body_abs >= min_body) | (range_abs >= min_range) | (five_abs >= min_5sec)
    five_ok = (five_abs >= min_5sec) | (five_surge >= min_5sec_surge) | (five_surge <= 0)
    ok = (liquidity_ok & movement_ok & five_ok).fillna(False).astype(bool)
    try:
        df["_history_failopen_turnover"] = turnover
        df["_history_failopen_quality_ok"] = ok
    except Exception:
        pass
    return ok


def _pass_history_missing_failopen(df: pd.DataFrame, *, stage: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    has_missing = "_volume_surge_history_missing" in df.columns
    has_failopen = "_volume_surge_failopen" in df.columns
    if not has_missing and not has_failopen:
        return df

    try:
        x = df.copy()
        missing = x.get("_volume_surge_history_missing", pd.Series(False, index=x.index)).fillna(False).astype(bool)
        failopen = x.get("_volume_surge_failopen", pd.Series(False, index=x.index)).fillna(False).astype(bool)
        affected_mask = (missing | failopen).fillna(False).astype(bool)
        affected = int(affected_mask.sum())
    except Exception:
        logger.debug("[TONOSAMA HISTORY GUARD] affected mask failed stage=%s", stage, exc_info=True)
        return df

    if not affected:
        logger.warning("[TONOSAMA HISTORY GUARD] pass-through history-missing/failopen rows stage=%s rows=%s affected=0 sample=[]", stage, len(df))
        return df

    if not _env_bool("TONOSAMA_HISTORY_MISSING_QUALITY_GUARD", True):
        logger.warning("[TONOSAMA HISTORY GUARD] pass-through history-missing/failopen rows stage=%s rows=%s affected=%s quality_guard=0 sample=%s", stage, len(x), affected, _sample(x.loc[affected_mask].copy()))
        return x

    quality_ok = _history_missing_quality_mask(x)
    drop_mask = affected_mask & ~quality_ok
    kept = x.loc[~drop_mask].copy()
    dropped = int(drop_mask.sum())
    if dropped:
        logger.warning(
            "[TONOSAMA HISTORY GUARD] drop weak history-missing/failopen rows stage=%s before=%s after=%s dropped=%s sample=%s",
            stage, len(x), len(kept), dropped, _sample(x.loc[drop_mask].copy()),
        )
    logger.warning(
        "[TONOSAMA HISTORY GUARD] pass-through history-missing/failopen rows stage=%s rows=%s affected=%s kept=%s dropped=%s sample=%s",
        stage, len(x), affected, int((affected_mask & quality_ok).sum()), dropped,
        _sample(kept.loc[affected_mask.reindex(kept.index, fill_value=False)].copy()) if not kept.empty else [],
    )
    return kept


def build_scalping_feature_df_guarded(original, *args, **kwargs):
    """旧 _patched_build_scalping_feature_df (runner.build_scalping_feature_df 差し替え)。

    original は runner が from-import で束縛していた素の実装
    (volume_surge 側の failclose ラッパーとは別束縛。実測済み)。
    """
    df = original(*args, **kwargs) if callable(original) else pd.DataFrame()
    if isinstance(df, pd.DataFrame):
        return _pass_history_missing_failopen(df, stage="build_scalping_feature_df")
    return df


def guard_enabled() -> bool:
    """旧 install() の TONOSAMA_HISTORY_MISSING_GUARD_ENABLED 判定。

    偽のときは runner 側のラッパーを掛けない (旧 install も同じ位置で return した)。
    """
    return _env_bool("TONOSAMA_HISTORY_MISSING_GUARD_ENABLED", True)


def apply_history_guard_defaults() -> bool:
    """旧 install() の残り。関数差し替えは本文化済みなので env 既定値だけ。

    volatility_filter のしきい値は trading/filters/volatility_filter.py へ、
    add_volume_surge_features / build_scalping_feature_df のラップは
    volume_surge.py / runner.py へ本文化済み。
    """
    global _PATCHED
    if _PATCHED:
        return True
    try:
        _enable_controlled_history_fallback_defaults()
        _PATCHED = True
        logger.warning(
            "[TONOSAMA HISTORY GUARD] defaults applied v5.3 guard_enabled=%s raw1_resample=%s raw1_history=%s push_raw_db_history=%s lookback_min=%s raw_limit=%s cache_sec=%.1f timeout=%.2f busy_ms=%s min_bars_3m=%s min_bars_5m=%s ratio_min_periods=%s quality_guard=%s",
            guard_enabled(),
            _env_bool("TONOSAMA_RAW1_RESAMPLE_FALLBACK", True),
            _env_bool("TONOSAMA_RAW1_HISTORY_RESAMPLE", True),
            _env_bool("TONOSAMA_PUSH_RAW_DB_HISTORY_ENABLED", True),
            _env_int("TONOSAMA_PUSH_RAW_DB_HISTORY_LOOKBACK_MIN", 30),
            _env_int("TONOSAMA_PUSH_RAW_DB_HISTORY_LIMIT", 20000),
            _env_float("TONOSAMA_PUSH_RAW_DB_HISTORY_CACHE_SEC", 8.0),
            _env_float("TONOSAMA_PUSH_RAW_DB_HISTORY_TIMEOUT_SEC", 0.35),
            _env_int("TONOSAMA_PUSH_RAW_DB_HISTORY_BUSY_TIMEOUT_MS", 150),
            _raw1_min_bars_for_interval(3),
            _raw1_min_bars_for_interval(5),
            _env_int("TONOSAMA_SURGE_RATIO_MIN_PERIODS", 1),
            _env_bool("TONOSAMA_HISTORY_MISSING_QUALITY_GUARD", True),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA HISTORY GUARD] defaults apply failed")
        return False


try:
    apply_history_guard_defaults()
except Exception:
    logger.exception("[TONOSAMA HISTORY GUARD] auto defaults failed")


__all__ = [
    "apply_history_guard_defaults",
    "guard_enabled",
    "add_volume_surge_features_guarded",
    "build_scalping_feature_df_guarded",
]


# ============================================================
# TONOSAMA DT TZ GUARD (旧 core/startup/tonosama_datetime_tz_guard_patch.py)
# ------------------------------------------------------------
# 旧パッチはこのモジュールの _load_raw1_history を完全置換し、
# _resample_1m_to_interval / _compute_surge_features を
# 「pd.to_datetime を安全版へ一時差し替えして呼ぶ」ラッパーで包んでいた。
# tz 付き / naive が混在した datetime で pandas の concat / drop_duplicates /
# sort が落ちる問題への対策。
#
# 旧パッチは sitecustomize の BACKGROUND_MAIN_PATCHES (既定 ON) と
# fast_startup_runtime_patch の両方から install されていたため、実質常時 ON。
# ここでは同じ既定のまま、記録されているキルスイッチ
# DISABLE_TONOSAMA_DT_TZ_GUARD_PATCH を _tzg_enabled() で見る。
#
# 意図的な差分: 旧 fast_startup_runtime_patch 経由の install は
# DISABLE_TONOSAMA_DT_TZ_GUARD_PATCH を見ずに必ず適用していた。
# 本文化後はどの経路でも同じ env が効く (キルスイッチが実際に効くようになる)。
# ============================================================

_TONOSAMA_DT_TZ_GUARD_PATCHED = True
_ORIGINAL_TO_DATETIME = pd.to_datetime


def _tzg_enabled() -> bool:
    return not _env_bool("DISABLE_TONOSAMA_DT_TZ_GUARD_PATCH", False)


def _tzg_normalize_one_datetime(v: Any) -> Any:
    try:
        x = _ORIGINAL_TO_DATETIME(v, errors="coerce")
        if pd.isna(x):
            return pd.NaT
        tzinfo = getattr(x, "tzinfo", None)
        if tzinfo is not None:
            try:
                return x.tz_convert("Asia/Tokyo").tz_localize(None)
            except Exception:
                try:
                    return x.tz_localize(None)
                except Exception:
                    return pd.NaT
        return x
    except Exception:
        return pd.NaT


def _tzg_normalize_dt_jst_naive_series(s: Any) -> pd.Series:
    try:
        if isinstance(s, pd.Series):
            out = s.map(_tzg_normalize_one_datetime)
            return _ORIGINAL_TO_DATETIME(out, errors="coerce")
        out = pd.Series(s).map(_tzg_normalize_one_datetime)
        return _ORIGINAL_TO_DATETIME(out, errors="coerce")
    except Exception:
        try:
            return _ORIGINAL_TO_DATETIME(s, errors="coerce")
        except Exception:
            return pd.Series(pd.NaT, index=getattr(s, "index", None))


def _tzg_safe_to_datetime(arg: Any, *args: Any, **kwargs: Any) -> Any:
    """
    pandas.to_datetime compatible wrapper.

    Important:
    - tz-naive values are treated as already-JST wall-clock time and are not shifted.
    - tz-aware values are converted to Asia/Tokyo, then made tz-naive.
    - explicit utc=True calls are delegated to pandas unchanged.
    """
    try:
        if bool(kwargs.get("utc", False)):
            return _ORIGINAL_TO_DATETIME(arg, *args, **kwargs)
        if isinstance(arg, pd.Series):
            return _tzg_normalize_dt_jst_naive_series(arg)
        if isinstance(arg, pd.Index):
            return pd.DatetimeIndex(_tzg_normalize_dt_jst_naive_series(pd.Series(arg)))
        if isinstance(arg, (list, tuple)):
            return pd.DatetimeIndex(_tzg_normalize_dt_jst_naive_series(pd.Series(arg)))
        return _tzg_normalize_one_datetime(arg)
    except Exception:
        return _ORIGINAL_TO_DATETIME(arg, *args, **kwargs)


@contextlib.contextmanager
def _tzg_temporary_safe_to_datetime(pd_module: Any):
    old = getattr(pd_module, "to_datetime", None)
    try:
        pd_module.to_datetime = _tzg_safe_to_datetime
        yield
    finally:
        try:
            if old is not None:
                pd_module.to_datetime = old
        except Exception:
            pass


def _tzg_normalize_summary_datetime_df(df: Any) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty or "datetime" not in df.columns:
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    x = df.copy()
    try:
        x["datetime"] = _tzg_normalize_dt_jst_naive_series(x["datetime"])
        if "symbol" in x.columns:
            x["symbol"] = x["symbol"].astype(str).str.strip()
            x = x[x["symbol"] != ""].copy()
        x = x.dropna(subset=["datetime"])
        if "symbol" in x.columns and not x.empty:
            x = x.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
        elif not x.empty:
            x = x.sort_values(["datetime"], kind="stable").reset_index(drop=True)
    except Exception:
        logger.debug("[TONOSAMA DT TZ GUARD] dataframe datetime normalize failed", exc_info=True)
    return x


def _tzg_safe_df(obj: Any) -> pd.DataFrame:
    return obj if isinstance(obj, pd.DataFrame) else pd.DataFrame()


def _load_raw1_history_tz_safe(vs: Any, *, interval: int = 1) -> pd.DataFrame:
    frames: list[tuple[str, pd.DataFrame]] = []
    try:
        if _env_bool("TONOSAMA_RAW1_HISTORY_RESAMPLE", True):
            frames.append(("push_raw_db_history", _tzg_safe_df(_load_push_raw_db_1m_history())))
            frames.append(("global_context_history", _tzg_safe_df(_call_gc_history(1))))
            frames.append(("global_data_history", _tzg_safe_df(_call_global_data_history(1))))
        try:
            frames.append(("merged_latest", _tzg_safe_df(vs.load_merged_summary(1))))
        except Exception:
            logger.debug("[TONOSAMA DT TZ GUARD] merged latest load failed", exc_info=True)

        norm_parts: list[pd.DataFrame] = []
        stats: list[dict[str, Any]] = []
        for label, df in frames:
            try:
                if df is None or df.empty:
                    stats.append({"source": label, "rows": 0, "symbols": 0, "latest": None})
                    continue
                with _tzg_temporary_safe_to_datetime(pd):
                    x = vs.normalize_summary_base(df, interval=1)
                if x is None or x.empty or "datetime" not in x.columns:
                    stats.append({"source": label, "rows": len(df), "symbols": 0, "latest": None, "normalized": 0})
                    continue
                x = _tzg_normalize_summary_datetime_df(x)
                if x.empty or "symbol" not in x.columns or "datetime" not in x.columns:
                    continue
                x = x.dropna(subset=["symbol", "datetime"])
                if x.empty:
                    continue
                x["_raw1_source"] = label
                norm_parts.append(x)
                stats.append({
                    "source": label,
                    "rows": len(x),
                    "symbols": int(x["symbol"].nunique()) if "symbol" in x.columns else 0,
                    "latest": str(x["datetime"].max()),
                })
            except Exception:
                logger.debug("[TONOSAMA DT TZ GUARD] normalize failed source=%s", label, exc_info=True)

        if not norm_parts:
            logger.warning("[TONOSAMA RAW1 HISTORY] no usable 1m history interval=%s stats=%s", interval, stats)
            return pd.DataFrame()

        out = pd.concat(norm_parts, ignore_index=True, sort=False)
        out = _tzg_normalize_summary_datetime_df(out)
        if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
            return pd.DataFrame()
        out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

        min_bars = _raw1_min_bars_for_interval(interval)
        counts = out.groupby("symbol")["datetime"].transform("count") if not out.empty else pd.Series(dtype="int64")
        enough = out[counts >= min_bars].copy()
        if enough.empty:
            logger.warning(
                "[TONOSAMA RAW1 HISTORY] insufficient 1m history interval=%s rows=%s symbols=%s min_bars=%s stats=%s tz_guard=v2",
                interval,
                len(out),
                int(out["symbol"].nunique()) if "symbol" in out.columns else 0,
                min_bars,
                stats,
            )
            return out
        logger.warning(
            "[TONOSAMA RAW1 HISTORY] loaded interval=%s rows=%s symbols=%s usable_rows=%s usable_symbols=%s min_bars=%s latest=%s stats=%s tz_guard=v2",
            interval,
            len(out),
            int(out["symbol"].nunique()) if "symbol" in out.columns else 0,
            len(enough),
            int(enough["symbol"].nunique()) if "symbol" in enough.columns else 0,
            min_bars,
            enough["datetime"].max(),
            stats,
        )
        return enough.reset_index(drop=True)
    except Exception:
        logger.exception("[TONOSAMA DT TZ GUARD] safe _load_raw1_history failed interval=%s", interval)
        return pd.DataFrame()


# _normalize_dt_jst_naive_series は旧パッチがこのモジュールへ setattr していた
# 名前。外部の読み手は無いが、名前ごと残しておく。
_normalize_dt_jst_naive_series = _tzg_normalize_dt_jst_naive_series


def _load_raw1_history(vs: Any, *, interval: int = 1) -> pd.DataFrame:
    """TZ GUARD 有効時は concat/sort を通らない安全版を使う (旧パッチ v2)。"""
    if _tzg_enabled():
        return _load_raw1_history_tz_safe(vs, interval=interval)
    return _load_raw1_history_core(vs, interval=interval)


def _resample_1m_to_interval(vs: Any, interval: int) -> pd.DataFrame:
    if not _tzg_enabled():
        return _resample_1m_to_interval_core(vs, interval)
    try:
        with _tzg_temporary_safe_to_datetime(pd):
            out = _resample_1m_to_interval_core(vs, interval)
        return _tzg_normalize_summary_datetime_df(out)
    except Exception:
        logger.exception("[TONOSAMA DT TZ GUARD] wrapped function failed name=%s", "_resample_1m_to_interval")
        return pd.DataFrame()


def _compute_surge_features(vs: Any, df: pd.DataFrame, *, interval: int, source_label: str) -> pd.DataFrame:
    if not _tzg_enabled():
        return _compute_surge_features_core(vs, df, interval=interval, source_label=source_label)
    try:
        with _tzg_temporary_safe_to_datetime(pd):
            out = _compute_surge_features_core(vs, df, interval=interval, source_label=source_label)
        return _tzg_normalize_summary_datetime_df(out)
    except Exception:
        logger.exception("[TONOSAMA DT TZ GUARD] wrapped function failed name=%s", "_compute_surge_features")
        return pd.DataFrame()
