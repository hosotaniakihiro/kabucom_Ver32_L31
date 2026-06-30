# ============================================================
# File   : core/startup/tonosama_history_missing_guard_patch.py
# Version: V5.2-PUSH-RAW-DB-HISTORY-FOR-5M-RATIO
# ------------------------------------------------------------
# 目的:
#   1) 3m/5m summary が stale/empty でも、1m summary 履歴が新鮮なら
#      raw1 履歴から 3m/5m を即時 resample して出来高急増率を作る。
#   2) sitecustomize の controlled fail-open 設定を OFF に戻さない。
#   3) history_missing/failopen 行でも、低出来高・低変動・5秒無反応の
#      弱い候補はDROPする。
#   4) ratio_nonnull=0 の擬似3m/5mは本物の急増履歴として採用しない。
#   5) 5mだけ raw1 必要本数を緩和し、3mの判定は維持する。
#   6) merged_latest 1本だけではなく pushYYYYMMDD.db から直近1分足履歴を作る。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_BUILD = None
_ORIGINAL_ADD_SURGE = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


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
    _setdefault_env("TONOSAMA_PUSH_RAW_DB_HISTORY_LOOKBACK_MIN", "45")
    _setdefault_env("TONOSAMA_PUSH_RAW_DB_HISTORY_LIMIT", "120000")


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


def _patch_volatility_filter_thresholds() -> None:
    try:
        import trading.filters.volatility_filter as vf
        atr_min = _env_float("ENTRY_ATR_1M_MIN_RATIO", 0.0010)
        range5_min = _env_float("ENTRY_RANGE_5M_MIN_PCT", 0.008)
        row_range_min = _env_float("ENTRY_ROW_RANGE_MIN_PCT", 0.003)
        vf.DEFAULT_ATR_1M_MIN_RATIO = atr_min
        vf.DEFAULT_RANGE_5M_MIN_PCT = range5_min
        vf.DEFAULT_ENTRY_ROW_RANGE_MIN_PCT = row_range_min
        for name, defaults in (("_atr_1m_filter_from_entry_row", (atr_min,)), ("_range_5m_filter_from_entry_row", (range5_min,)), ("_entry_row_range_ok", (row_range_min,))):
            try:
                getattr(vf, name).__defaults__ = defaults
            except Exception:
                pass
        try:
            if getattr(vf.atr_1m_filter, "__kwdefaults__", None):
                vf.atr_1m_filter.__kwdefaults__["min_ratio"] = atr_min
        except Exception:
            pass
        try:
            if getattr(vf.range_5m_filter, "__kwdefaults__", None):
                vf.range_5m_filter.__kwdefaults__["min_pct"] = range5_min
        except Exception:
            pass
        logger.warning("[VOL FILTER THRESHOLD PATCH] installed atr_min_ratio=%.6f range5_min_pct=%.6f entry_row_range_min_pct=%.6f", atr_min, range5_min, row_range_min)
    except Exception:
        logger.exception("[VOL FILTER THRESHOLD PATCH] install failed")


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
        out = pd.Series(default, index=df.index, dtype="float64")
        for n in found:
            out = pd.concat([out, _num(df, n, default)], axis=1).max(axis=1).fillna(default)
        return out
    except Exception:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")


def _load_push_raw_db_1m_history() -> pd.DataFrame:
    if not _env_bool("TONOSAMA_PUSH_RAW_DB_HISTORY_ENABLED", True):
        return pd.DataFrame()
    path = _push_db_path()
    p = Path(path)
    if not p.exists():
        logger.warning("[TONOSAMA PUSH RAW DB HISTORY] db not found path=%s", path)
        return pd.DataFrame()
    now_i = dt.datetime.now().replace(microsecond=0)
    lookback_min = max(10, _env_int("TONOSAMA_PUSH_RAW_DB_HISTORY_LOOKBACK_MIN", 45))
    limit = max(1000, _env_int("TONOSAMA_PUSH_RAW_DB_HISTORY_LIMIT", 120000))
    since = now_i - dt.timedelta(minutes=lookback_min)
    timeout_sec = max(0.05, _env_float("TONOSAMA_PUSH_RAW_DB_HISTORY_TIMEOUT_SEC", 0.8))
    busy_ms = int(max(50, _env_float("TONOSAMA_PUSH_RAW_DB_HISTORY_BUSY_TIMEOUT_MS", 500)))
    try:
        with sqlite3.connect(str(p), timeout=timeout_sec) as conn:
            conn.execute(f"PRAGMA busy_timeout={busy_ms};")
            table = "stream_data_raw" if _table_exists(conn, "stream_data_raw") else "stream_data"
            cols = _table_cols(conn, table)
            if not {"symbol", "datetime", "price"}.issubset(cols):
                logger.warning("[TONOSAMA PUSH RAW DB HISTORY] required cols missing table=%s cols=%s path=%s", table, sorted(cols), path)
                return pd.DataFrame()
            wanted = ["symbol", "symbolname", "datetime", "date", "time", "price", "volume", "trading_value", "vwap", "opening_price", "high_price", "low_price", "received_at"]
            select_cols = [c for c in wanted if c in cols]
            where_parts: list[str] = []
            params: list[Any] = []
            if "date" in cols:
                where_parts.append("date = ?")
                params.append(now_i.strftime("%Y-%m-%d"))
            if "received_at" in cols:
                where_parts.append("received_at >= ?")
                params.append(since.isoformat())
            else:
                where_parts.append("datetime >= ?")
                params.append(since.isoformat())
            where = " AND ".join(where_parts) if where_parts else "1=1"
            sql = f"SELECT {','.join(_qident(c) for c in select_cols)} FROM {_qident(table)} WHERE {where} ORDER BY datetime ASC LIMIT ?"
            params.append(limit)
            df = pd.read_sql_query(sql, conn, params=params)
    except Exception:
        logger.debug("[TONOSAMA PUSH RAW DB HISTORY] load failed path=%s", path, exc_info=True)
        return pd.DataFrame()
    if df.empty:
        logger.warning("[TONOSAMA PUSH RAW DB HISTORY] empty path=%s since=%s lookback_min=%s", path, since, lookback_min)
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
        df["slot"] = df["datetime"].dt.floor("1min")
        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
        else:
            df["volume"] = 0.0
        if "trading_value" in df.columns:
            df["trading_value"] = pd.to_numeric(df["trading_value"], errors="coerce").fillna(0.0)
        else:
            df["trading_value"] = 0.0
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
        bad = (out["volume"] < 0) | out["volume"].isna()
        out.loc[bad, "volume"] = 0.0
        raw_positive = pd.to_numeric(out["cum_volume"], errors="coerce").fillna(0.0) > 0
        delta_positive = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0) > 0
        if int(delta_positive.sum()) < max(3, int(raw_positive.sum() * 0.05)):
            # volume が累積ではなくバー値のDBにも対応する保険。ただし通常は差分を使う。
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
        out = out.dropna(subset=["datetime", "close"])
        logger.warning(
            "[TONOSAMA PUSH RAW DB HISTORY] loaded rows=%s symbols=%s latest=%s lookback_min=%s volume_nonzero=%s volume_mode=%s path=%s",
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
            lookback_min,
            int((pd.to_numeric(out["volume"], errors="coerce").fillna(0) > 0).sum()) if "volume" in out.columns else 0,
            volume_mode,
            path,
        )
        return out.reset_index(drop=True)
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


def _load_raw1_history(vs: Any, *, interval: int = 1) -> pd.DataFrame:
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


def _resample_1m_to_interval(vs: Any, interval: int) -> pd.DataFrame:
    try:
        if not _env_bool("TONOSAMA_RAW1_RESAMPLE_FALLBACK", True):
            return pd.DataFrame()
        interval_i = int(interval)
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
        out_parts: list[pd.DataFrame] = []
        rule = f"{interval_i}min"
        for symbol, g in x.sort_values("datetime").groupby("symbol"):
            try:
                gg = g.set_index("datetime")
                rr = gg.resample(rule, label="right", closed="right").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum")).dropna(subset=["close"])
                if rr.empty:
                    continue
                rr = rr.reset_index()
                rr["symbol"] = symbol
                try:
                    rr["symbolname"] = str(g["symbolname"].dropna().iloc[-1]) if "symbolname" in g.columns and not g["symbolname"].dropna().empty else symbol
                except Exception:
                    rr["symbolname"] = symbol
                rr["source"] = f"tonosama_raw1_history_resample_{interval_i}m"
                rr["interval"] = interval_i
                out_parts.append(rr)
            except Exception:
                logger.debug("[TONOSAMA RAW1 RESAMPLE] symbol failed interval=%s symbol=%s", interval_i, symbol, exc_info=True)
        out = pd.concat(out_parts, ignore_index=True) if out_parts else pd.DataFrame()
        if not out.empty:
            logger.warning("[TONOSAMA RAW1 RESAMPLE] built interval=%sm rows=%s symbols=%s latest=%s volume_nonzero=%s raw1_min_bars=%s", interval_i, len(out), out["symbol"].nunique() if "symbol" in out.columns else 0, out["datetime"].max() if "datetime" in out.columns else None, int((pd.to_numeric(out.get("volume", 0), errors="coerce").fillna(0) > 0).sum()) if "volume" in out.columns else 0, _raw1_min_bars_for_interval(interval_i))
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


def _compute_surge_features(vs: Any, df: pd.DataFrame, *, interval: int, source_label: str) -> pd.DataFrame:
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
        logger.warning("[TONOSAMA SURGE ROLLING PATCH] %sm latest rows=%s source=%s ratio_nonnull=%s up_ge3=%s down_ge3=%s head=%s", interval_i, len(latest), source_label, ratio_nonnull, int((latest[up_streak_col] >= 3).sum()), int((latest[down_streak_col] >= 3).sum()), latest[[c for c in ["symbol", f"close_{interval_i}m", f"volume_{interval_i}m", avg_col, up_streak_col, down_streak_col, last_delta_col, ratio_col, price_chg_col] if c in latest.columns]].head(12).to_dict("records"))
        return latest.reset_index(drop=True)
    except Exception:
        logger.exception("[TONOSAMA SURGE ROLLING PATCH] compute failed interval=%s source=%s", interval, source_label)
        return pd.DataFrame()


def _patch_volume_surge_rolling_before_recent() -> None:
    global _ORIGINAL_ADD_SURGE
    try:
        import trading.entry.tonosama.volume_surge as vs
        cur = getattr(vs, "add_volume_surge_features", None)
        if not callable(cur):
            logger.warning("[TONOSAMA SURGE ROLLING PATCH] target add_volume_surge_features not callable")
            return
        if getattr(cur, "_raw1_resample_fallback_patch_v52", False):
            return
        _ORIGINAL_ADD_SURGE = cur
        def _patched_add_volume_surge_features(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
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
                original = _ORIGINAL_ADD_SURGE(df, interval=interval_i) if callable(_ORIGINAL_ADD_SURGE) else pd.DataFrame()
                if not original.empty and not _has_real_surge_ratio(original, interval_i):
                    logger.warning("[TONOSAMA SURGE ROLLING PATCH] suppress original no-ratio failopen source interval=%sm rows=%s", interval_i, len(original))
                    return pd.DataFrame()
                return original
            except Exception:
                logger.exception("[TONOSAMA SURGE ROLLING PATCH] patched add_volume_surge_features failed interval=%s", interval_i)
                return _ORIGINAL_ADD_SURGE(df, interval=interval_i) if callable(_ORIGINAL_ADD_SURGE) else pd.DataFrame()
        _patched_add_volume_surge_features._raw1_resample_fallback_patch_v52 = True  # type: ignore[attr-defined]
        _patched_add_volume_surge_features._original = cur  # type: ignore[attr-defined]
        vs.add_volume_surge_features = _patched_add_volume_surge_features
        logger.warning("[TONOSAMA SURGE ROLLING PATCH] installed raw1 history resample fallback v5.2")
    except Exception:
        logger.exception("[TONOSAMA SURGE ROLLING PATCH] install failed")


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
        logger.warning("[TONOSAMA HISTORY GUARD] drop weak history-missing/failopen rows stage=%s before=%s after=%s dropped=%s thresholds=%s sample=%s", stage, len(x), len(kept), dropped, {"min_volume": _env_float("TONOSAMA_HISTORY_MISSING_MIN_VOLUME", 500000.0), "min_turnover": _env_float("TONOSAMA_HISTORY_MISSING_MIN_TURNOVER", 10000000.0), "min_price_change_pct": _env_float("TONOSAMA_HISTORY_MISSING_MIN_PRICE_CHANGE_PCT", 0.50), "min_body_pct": _env_float("TONOSAMA_HISTORY_MISSING_MIN_BODY_PCT", 0.30), "min_range_pct": _env_float("TONOSAMA_HISTORY_MISSING_MIN_RANGE_PCT", 1.00), "min_5sec_change_pct": _env_float("TONOSAMA_HISTORY_MISSING_MIN_5SEC_CHANGE_PCT", 0.05)}, _sample(x.loc[drop_mask].copy()))
    logger.warning("[TONOSAMA HISTORY GUARD] pass-through history-missing/failopen rows stage=%s rows=%s affected=%s kept=%s dropped=%s sample=%s", stage, len(x), affected, int((affected_mask & quality_ok).sum()), dropped, _sample(kept.loc[affected_mask.reindex(kept.index, fill_value=False)].copy()) if not kept.empty else [])
    return kept


def _patched_build_scalping_feature_df(*args, **kwargs):
    df = _ORIGINAL_BUILD(*args, **kwargs) if callable(_ORIGINAL_BUILD) else pd.DataFrame()
    if isinstance(df, pd.DataFrame):
        return _pass_history_missing_failopen(df, stage="build_scalping_feature_df")
    return df


def install() -> bool:
    global _PATCHED, _ORIGINAL_BUILD
    if _PATCHED:
        return True
    _enable_controlled_history_fallback_defaults()
    _patch_volatility_filter_thresholds()
    _patch_volume_surge_rolling_before_recent()
    if not _env_bool("TONOSAMA_HISTORY_MISSING_GUARD_ENABLED", True):
        logger.warning("[TONOSAMA HISTORY GUARD] disabled by env")
        return False
    try:
        import trading.entry.tonosama.runner as runner
    except Exception:
        logger.exception("[TONOSAMA HISTORY GUARD] import runner failed")
        return False
    try:
        cur = getattr(runner, "build_scalping_feature_df", None)
        if not callable(cur):
            logger.warning("[TONOSAMA HISTORY GUARD] target build_scalping_feature_df not callable")
            return False
        if getattr(cur, "_tonosama_history_guard_patch_v52", False):
            _PATCHED = True
            return True
        _ORIGINAL_BUILD = cur
        _patched_build_scalping_feature_df._tonosama_history_guard_patch_v52 = True  # type: ignore[attr-defined]
        _patched_build_scalping_feature_df._original = cur  # type: ignore[attr-defined]
        runner.build_scalping_feature_df = _patched_build_scalping_feature_df
        _PATCHED = True
        logger.warning("[TONOSAMA HISTORY GUARD] installed v5.2 raw1_resample=%s raw1_history=%s push_raw_db_history=%s lookback_min=%s min_bars_3m=%s min_bars_5m=%s ratio_min_periods=%s allow_history_missing=%s drop_history_missing=%s quality_guard=%s min_volume=%.0f min_price_change=%.3f min_range=%.3f", _env_bool("TONOSAMA_RAW1_RESAMPLE_FALLBACK", True), _env_bool("TONOSAMA_RAW1_HISTORY_RESAMPLE", True), _env_bool("TONOSAMA_PUSH_RAW_DB_HISTORY_ENABLED", True), _env_int("TONOSAMA_PUSH_RAW_DB_HISTORY_LOOKBACK_MIN", 45), _raw1_min_bars_for_interval(3), _raw1_min_bars_for_interval(5), _env_int("TONOSAMA_SURGE_RATIO_MIN_PERIODS", 1), _env_bool("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", True), _env_bool("TONOSAMA_DROP_HISTORY_MISSING_ENTRY", False), _env_bool("TONOSAMA_HISTORY_MISSING_QUALITY_GUARD", True), _env_float("TONOSAMA_HISTORY_MISSING_MIN_VOLUME", 500000.0), _env_float("TONOSAMA_HISTORY_MISSING_MIN_PRICE_CHANGE_PCT", 0.50), _env_float("TONOSAMA_HISTORY_MISSING_MIN_RANGE_PCT", 1.00))
        return True
    except Exception:
        logger.exception("[TONOSAMA HISTORY GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA HISTORY GUARD] auto install failed")


__all__ = ["install"]
