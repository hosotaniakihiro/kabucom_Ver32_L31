# ============================================================
# File   : trading/summary/indicators/indicator_calculator.py
# Version: Ver50-PRODUCTION-PUSH-MATURITY-GUARD-RSI-FIX-FINAL
#          -STRICT-MACD-MA75-HISTLEN
# ------------------------------------------------------------
# ✔ Ver49 完全互換ベース
# ✔ open/high/low/close と *_price の両対応維持
# ✔ duplicate columns / duplicate labels 対応維持
# ✔ OHLC alias 自動補完維持
# ✔ close_price all NaN 救済維持
# ✔ per-symbol level alias repair 維持
# ✔ slope diffベース維持
# ✔ pct_change不使用維持
# ✔ pandas groupby/transform 維持
# ✔ VWAP仕様維持（ffillのみ）
# ✔ 価格系NaNの0潰し禁止維持
# ✔ 価格0/負値を無効値扱い維持
# ✔ slope未計算はNaN維持
# ✔ CurrentPrice / current_price / ClosePrice / LastPrice 等の吸収維持
# ✔ drop原因のトレースログ維持
# ✔ symbolごとの履歴本数を明示計算
# ✔ technical_ready を履歴本数ベースで厳格化
# ✔ 1m/3m/5m で maturity guard を導入
# ✔ hist_min / hist_median / hist_max / ready_symbols ログ追加
# ✔ RSI計算を安定化
# ✔ avg_loss=0 && avg_gain>0 → RSI=100
# ✔ avg_loss=0 && avg_gain=0 → RSI=50
# ✔ RSIは未成熟時NaN維持
# ✔ RSI profile log追加
# ✔ FIX: EMA/MACD/signal は min_periods 付きで未成熟を NaN 維持
# ✔ FIX: ma75 は min_periods=75
# ✔ FIX: symbol_hist_len は datetime unique 基準
# ✔ FIX: technical_ready は signal も含めて厳格化
# ✔ production hardened
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
import threading
import time
import time as _ind_perf
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sqlite_cm import committing_connect
from nas_root import nas_path, nas_root

try:
    from pandas.errors import PerformanceWarning
except Exception:  # pragma: no cover
    PerformanceWarning = Warning  # type: ignore

logger = logging.getLogger(__name__)


# ============================================================
# DATAFRAME GUARD
# ============================================================

def _ensure_dataframe(df):
    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            logger.exception("[IND] failed to cast input to DataFrame")
            return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        try:
            df.columns = [
                "_".join([str(x) for x in col if str(x) != ""]).strip("_")
                if isinstance(col, tuple) else str(col)
                for col in df.columns
            ]
            logger.warning("[IND] MultiIndex columns flattened")
        except Exception:
            logger.exception("[IND] MultiIndex flatten failed")

    if df.columns.duplicated().any():
        dup = df.columns[df.columns.duplicated()].tolist()
        logger.warning("[IND] duplicate columns removed: %s", dup)
        df = df.loc[:, ~df.columns.duplicated(keep="last")]

    if not isinstance(df.index, pd.RangeIndex):
        try:
            df = df.reset_index(drop=True)
        except Exception:
            logger.exception("[IND] index reset failed")

    return df


# ============================================================
# SAFE COLUMN
# ============================================================

def _safe_get_series(df: pd.DataFrame, col: str) -> pd.Series | None:
    try:
        if col not in df.columns:
            return None

        value = df[col]

        if isinstance(value, pd.DataFrame):
            if value.shape[1] <= 0:
                return None

            best = None
            best_nonnull = -1

            for i in range(value.shape[1]):
                s = pd.to_numeric(value.iloc[:, i], errors="coerce")
                nonnull = int(s.notna().sum())
                if nonnull > best_nonnull:
                    best = s
                    best_nonnull = nonnull

            if best is not None:
                return best

            return value.iloc[:, 0]

        if isinstance(value, pd.Series):
            return value

        return pd.Series(value, index=df.index)

    except Exception:
        logger.exception("[IND] safe_get_series failed: %s", col)
        return None


def _series_head_list(df: pd.DataFrame, col: str, n: int = 5):
    try:
        s = _safe_get_series(df, col)
        if s is None:
            return None
        return s.head(n).tolist()
    except Exception:
        return None


def _numeric_nonnull_count(df: pd.DataFrame, col: str) -> int:
    try:
        s = _safe_get_series(df, col)
        if s is None:
            return 0
        s = pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan)
        return int(s.notna().sum())
    except Exception:
        return 0


# ============================================================
# NUMERIC HELPERS
# ============================================================

def _to_float(series):
    return pd.to_numeric(series, errors="coerce")


def _safe_div(a, b):
    try:
        return a / b.replace(0, np.nan)
    except Exception:
        try:
            return a / b
        except Exception:
            return pd.Series(
                [np.nan] * len(a),
                index=a.index if hasattr(a, "index") else None,
            )


def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """
    価格系は 0 埋めしない。
    価格以外の数値列のみ inf を NaN に寄せる。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    try:
        numeric = out.select_dtypes(include=[np.number]).columns.tolist()
        if numeric:
            out[numeric] = out[numeric].replace([np.inf, -np.inf], np.nan)
    except Exception:
        logger.exception("[IND] numeric sanitize failed")

    return out


def _sanitize_price_like_series(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    s = s.replace([np.inf, -np.inf], np.nan)
    s = s.mask(s <= 0, np.nan)
    return s


# ============================================================
# OHLC ALIAS REPAIR
# ============================================================

def _pick_best_numeric_series(df: pd.DataFrame, candidates: list[str]) -> pd.Series | None:
    best = None
    best_nonnull = 0

    for c in candidates:
        if c not in df.columns:
            continue

        s = _safe_get_series(df, c)
        if s is None:
            continue

        s = pd.to_numeric(s, errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan)
        nonnull = int(s.notna().sum())

        if nonnull > best_nonnull:
            best = s
            best_nonnull = nonnull

    return best


def _coalesce_two_columns(
    df: pd.DataFrame,
    left: str,
    right: str,
    *,
    numeric: bool = False,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    try:
        left_s = _safe_get_series(df, left) if left in df.columns else None
        right_s = _safe_get_series(df, right) if right in df.columns else None

        if left_s is None and right_s is None:
            return df

        if numeric:
            if left_s is not None:
                left_s = pd.to_numeric(left_s, errors="coerce")
            if right_s is not None:
                right_s = pd.to_numeric(right_s, errors="coerce")

        if left_s is None:
            merged = right_s
        elif right_s is None:
            merged = left_s
        else:
            try:
                merged = left_s.where(left_s.notna(), right_s)
            except Exception:
                merged = left_s.combine_first(right_s)

        df[left] = merged
        df[right] = merged

    except Exception:
        logger.exception("[IND] coalesce failed: %s / %s", left, right)

    return df


def _repair_ohlc_alias(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    df = df.copy()

    alias_map = {
        "open_price": [
            "open_price", "open", "Open", "OpenPrice",
            "opening_price", "OpeningPrice",
            "openvalue", "openValue",
            "始値",
        ],
        "high_price": [
            "high_price", "high", "High", "HighPrice",
            "highvalue", "highValue",
            "高値",
        ],
        "low_price": [
            "low_price", "low", "Low", "LowPrice",
            "lowvalue", "lowValue",
            "安値",
        ],
        "close_price": [
            "close_price", "close", "Close", "ClosePrice",
            "price", "Price",
            "current_price", "CurrentPrice",
            "last_price", "LastPrice",
            "closevalue", "closeValue",
            "現在値", "終値", "約定価格",
        ],
        "volume": [
            "volume", "Volume",
            "volume_total", "出来高",
            "TradingVolume", "trading_volume",
        ],
    }

    reverse_map = {
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "close_price",
    }

    for canon, candidates in alias_map.items():
        best = _pick_best_numeric_series(df, candidates)
        if best is not None:
            df[canon] = best
            logger.debug("[IND] alias canonicalized: %s <- %s", canon, candidates)

    for dst, src in reverse_map.items():
        if src in df.columns:
            try:
                df[dst] = pd.to_numeric(df[src], errors="coerce")
            except Exception:
                logger.exception("[IND] reverse alias apply failed: %s <- %s", dst, src)

    for a, b in (
        ("open", "open_price"),
        ("high", "high_price"),
        ("low", "low_price"),
        ("close", "close_price"),
    ):
        df = _coalesce_two_columns(df, a, b, numeric=True)

    return df


# ============================================================
# REQUIRED COLUMN CHECK
# ============================================================

def _ensure_required_columns(df: pd.DataFrame, required: Iterable[str]) -> tuple[pd.DataFrame, bool]:
    if df is None or df.empty:
        return pd.DataFrame(), False

    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.warning("[IND] missing columns: %s", missing)
        return df, False

    return df, True


# ============================================================
# INTERVAL / MATURITY HELPERS
# ============================================================

def _normalize_interval_name(interval) -> str:
    try:
        s = str(interval).strip().lower()
        if s in ("1", "1m", "1min", "1minute"):
            return "1min"
        if s in ("3", "3m", "3min", "3minute"):
            return "3min"
        if s in ("5", "5m", "5min", "5minute"):
            return "5min"
        return s
    except Exception:
        return "1min"


def _min_history_requirements(interval) -> tuple[int, int, int]:
    """
    Returns:
      min_slope_hist, min_rsi_hist, min_macd_hist
    """
    s = _normalize_interval_name(interval)

    if s == "1min":
        return (5, 14, 26)
    if s == "3min":
        return (3, 6, 10)
    if s == "5min":
        return (3, 5, 8)

    return (5, 14, 26)


def _build_symbol_hist_len(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty or "symbol" not in df.columns:
        return pd.Series(dtype="float64")

    try:
        if "datetime" in df.columns:
            tmp = df[["symbol", "datetime"]].copy()
            tmp["datetime"] = pd.to_datetime(tmp["datetime"], errors="coerce")
            tmp = tmp.dropna(subset=["symbol", "datetime"])
            vc = tmp.groupby("symbol")["datetime"].nunique().astype("float64")
            return df["symbol"].map(vc).astype("float64")
        return (
            df.groupby("symbol")["symbol"]
            .transform("size")
            .astype("float64")
            .reindex(df.index)
        )
    except Exception:
        logger.exception("[IND] build symbol hist len failed")
        return pd.Series(np.nan, index=df.index, dtype="float64")


def _log_hist_profile(df: pd.DataFrame, interval) -> None:
    try:
        if df is None or df.empty or "symbol" not in df.columns:
            logger.info("[IND] hist profile skipped interval=%s empty_or_no_symbol", interval)
            return

        if "datetime" in df.columns:
            hist = (
                df.assign(datetime=pd.to_datetime(df["datetime"], errors="coerce"))
                  .dropna(subset=["datetime"])
                  .groupby("symbol")["datetime"]
                  .nunique()
                  .astype("float64")
            )
        else:
            hist = (
                df.groupby("symbol")["symbol"]
                .size()
                .astype("float64")
            )

        if hist.empty:
            logger.info("[IND] hist profile skipped interval=%s hist_empty", interval)
            return

        logger.info(
            "[IND] hist profile interval=%s symbols=%d hist_min=%d hist_median=%.1f hist_max=%d",
            interval,
            int(hist.size),
            int(hist.min()),
            float(hist.median()),
            int(hist.max()),
        )
    except Exception:
        logger.exception("[IND] hist profile log failed interval=%s", interval)


# ============================================================
# SLOPE
# ============================================================

def _calc_slope_by_symbol(
    df: pd.DataFrame,
    column: str,
    window: int = 5,
):
    if df is None or df.empty:
        return pd.Series(dtype=float)

    if column not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")

    try:
        df_local = df[["symbol", column]].copy()
        df_local[column] = pd.to_numeric(df_local[column], errors="coerce")
        df_local[column] = df_local[column].replace([np.inf, -np.inf], np.nan)

        slope = (
            df_local
            .groupby("symbol")[column]
            .transform(lambda x: x.diff().rolling(window, min_periods=2).mean())
        )

        slope = slope.replace([np.inf, -np.inf], np.nan)
        slope = slope.clip(-1000, 1000)
        slope = slope.reindex(df.index)

        return slope.astype("float64")

    except Exception:
        logger.exception("[IND] slope calculation failed column=%s", column)
        return pd.Series(np.nan, index=df.index, dtype="float64")


# ============================================================
# PER-SYMBOL INDICATORS
# ============================================================

def _build_symbol_indicators(g: pd.DataFrame, symbol: str) -> pd.DataFrame:
    raw = g.copy()

    g = (
        g
        .sort_values("datetime")
        .drop_duplicates(["datetime"], keep="last")
        .reset_index(drop=True)
        .copy()
    )

    g = _repair_ohlc_alias(g)

    for c in (
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
    ):
        if c not in g.columns:
            g[c] = np.nan if c != "volume" else 0.0

    g["open_price"] = _sanitize_price_like_series(g["open_price"])
    g["high_price"] = _sanitize_price_like_series(g["high_price"])
    g["low_price"] = _sanitize_price_like_series(g["low_price"])
    g["close_price"] = _sanitize_price_like_series(g["close_price"])
    g["volume"] = pd.to_numeric(g["volume"], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)

    before_close_drop = len(g)
    g = g.dropna(subset=["close_price"]).copy()
    close_dropped = before_close_drop - len(g)

    if g.empty:
        logger.warning(
            "[IND] symbol=%s dropped because close_price all invalid "
            "raw_rows=%d raw_cols=%s "
            "nonnull(close_price=%d close=%d Close=%d price=%d Price=%d current_price=%d CurrentPrice=%d last_price=%d LastPrice=%d) "
            "head(close_price=%s close=%s CurrentPrice=%s current_price=%s last_price=%s)",
            symbol,
            len(raw),
            list(raw.columns),
            _numeric_nonnull_count(raw, "close_price"),
            _numeric_nonnull_count(raw, "close"),
            _numeric_nonnull_count(raw, "Close"),
            _numeric_nonnull_count(raw, "price"),
            _numeric_nonnull_count(raw, "Price"),
            _numeric_nonnull_count(raw, "current_price"),
            _numeric_nonnull_count(raw, "CurrentPrice"),
            _numeric_nonnull_count(raw, "last_price"),
            _numeric_nonnull_count(raw, "LastPrice"),
            _series_head_list(raw, "close_price"),
            _series_head_list(raw, "close"),
            _series_head_list(raw, "CurrentPrice"),
            _series_head_list(raw, "current_price"),
            _series_head_list(raw, "last_price"),
        )
        return pd.DataFrame()

    if close_dropped > 0:
        logger.debug(
            "[IND] symbol=%s dropped invalid close rows=%d remain=%d",
            symbol, close_dropped, len(g)
        )

    for c in ("open_price", "high_price", "low_price"):
        g[c] = g[c].combine_first(g["close_price"])

    valid_ohlc = (
        g["open_price"].notna()
        & g["high_price"].notna()
        & g["low_price"].notna()
        & g["close_price"].notna()
        & (g["high_price"] >= g["low_price"])
        & (g["high_price"] >= g["open_price"])
        & (g["high_price"] >= g["close_price"])
        & (g["low_price"] <= g["open_price"])
        & (g["low_price"] <= g["close_price"])
    )
    before_ohlc_drop = len(g)
    g = g.loc[valid_ohlc].copy()
    ohlc_dropped = before_ohlc_drop - len(g)

    if g.empty:
        logger.warning(
            "[IND] symbol=%s dropped because OHLC invalid after repair raw_rows=%d dropped_close=%d dropped_ohlc=%d",
            symbol,
            len(raw),
            close_dropped,
            ohlc_dropped,
        )
        return pd.DataFrame()

    # ====================================================
    # Δ価格
    # ====================================================
    g["price_diff"] = g["close_price"].diff()

    # ====================================================
    # MA
    # ====================================================
    g["ma5"] = g["close_price"].rolling(5, min_periods=5).mean()
    g["ma25"] = g["close_price"].rolling(25, min_periods=25).mean()
    g["ma75"] = g["close_price"].rolling(75, min_periods=75).mean()

    # ====================================================
    # EMA / MACD
    # ====================================================
    g["ema12"] = g["close_price"].ewm(span=12, adjust=False, min_periods=12).mean()
    g["ema26"] = g["close_price"].ewm(span=26, adjust=False, min_periods=26).mean()
    g["macd"] = g["ema12"] - g["ema26"]
    g["signal"] = g["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
    g["hist"] = g["macd"] - g["signal"]

    close_valid_count = g["close_price"].notna().cumsum()
    g["macd"] = g["macd"].where(close_valid_count >= 26, np.nan)
    g["signal"] = g["signal"].where(close_valid_count >= 34, np.nan)
    g["hist"] = g["hist"].where(close_valid_count >= 34, np.nan)

    # ====================================================
    # RSI
    # ====================================================
    delta = g["close_price"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    rsi = rsi.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    rsi = rsi.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    rsi = rsi.where(close_valid_count >= 14, np.nan)

    g["rsi"] = pd.to_numeric(rsi, errors="coerce").replace([np.inf, -np.inf], np.nan)

    # ====================================================
    # Bollinger
    # ====================================================
    mid = g["close_price"].rolling(25, min_periods=25).mean()
    std = g["close_price"].rolling(25, min_periods=25).std()

    g["bb_mid"] = mid
    g["bb_upper"] = mid + 2 * std
    g["bb_lower"] = mid - 2 * std
    g["bb_width"] = (g["bb_upper"] - g["bb_lower"]).replace([np.inf, -np.inf], np.nan)

    # ====================================================
    # ATR
    # ====================================================
    tr = pd.concat([
        g["high_price"] - g["low_price"],
        (g["high_price"] - g["close_price"].shift()).abs(),
        (g["low_price"] - g["close_price"].shift()).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(14, min_periods=14).mean()
    atr = atr.replace([np.inf, -np.inf], np.nan).astype("float64")

    g["atr"] = atr
    g["atr_1m"] = atr
    g["atr_3m"] = atr
    g["atr_5m"] = atr

    # ====================================================
    # VWAP
    # ====================================================
    tp = (g["high_price"] + g["low_price"] + g["close_price"]) / 3.0

    vol = g["volume"].replace(0, np.nan)
    cum_vol = vol.cumsum().replace(0, np.nan)

    g["vwap"] = ((tp * vol).cumsum() / cum_vol).replace([np.inf, -np.inf], np.nan).ffill()

    return g


# ============================================================
# PREVDAY WARMUP / MA12
# (旧 core/startup/indicator_fragmentation_runtime_patch.py から移設)
#
# 寄り付き直後に当日分足だけでは ma25/ma75/RSI/MACD/ATR が未成熟になる
# 問題を避けるため、前営業日の summary DB から最後N本を「計算用にだけ」
# 一時連結する。前営業日行は _PREVDAY_WARMUP_COL を付けてメモリ上で連結し、
# 計算後は必ず削除する。DB保存・Discord表示・ENTRY判定へ渡るのは当日
# 入力行だけ。ma12 はSummary-AI/Ranking/Tonosamaがスコア補正に使う補助線。
# ============================================================

_PREVDAY_WARMUP_COL = "__prevday_indicator_warmup"
# 2026-09-28: 当日イントラデイ用の目印は別列にする。
#   同じ列を共有していたため、先に走る当日ウォームアップが印を付けた時点で
#   前営業日ウォームアップが必ず即 return し、3分足/5分足は一度も前日を
#   読めていなかった (RSI が計算できず rsi_neutral_fill になる)。
_INTRADAY_WARMUP_COL = "__intraday_indicator_warmup"
_PREVDAY_ENV_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled"}


def _prevday_env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _PREVDAY_ENV_TRUE
    except Exception:
        return bool(default)


def _prevday_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _table_for_interval(interval: Any) -> str:
    s = _normalize_interval_name(interval)
    if s == "3min":
        return "stock_summary_3min"
    if s == "5min":
        return "stock_summary_5min"
    return "stock_summary_1min"


# summaryYYYYMMDD.db の実体は <root>/raw_data/kabu_station/summary/ に置かれる。
# 旧実装は base 直下しか glob しておらず、prevday warmup が一度も
# DB を見つけられていなかった (2026-08-31: [IND PREVDAY WARMUP] のログが皆無)。
_SUMMARY_DB_SUBDIRS = (
    ("raw_data", "kabu_station", "summary"),
    (),
)


def _summary_base_dirs() -> list[Path]:
    vals = [
        os.getenv("SUMMARY_DB_DIR"),
        os.getenv("AUTO_STOCK_DB_DIR"),
        os.getenv("AUTO_STOCK_BUY_SELL_DIR"),
        os.getenv("AUTOSTOCK_DB_DIR"),
        nas_root(),
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for v in vals:
        if not v:
            continue
        for sub in _SUMMARY_DB_SUBDIRS:
            try:
                p = Path(str(v)).joinpath(*sub) if sub else Path(str(v))
                key = str(p).lower()
                if key not in seen:
                    out.append(p)
                    seen.add(key)
            except Exception:
                pass
    return out


def _prevday_target_date_yyyymmdd(df: pd.DataFrame) -> str:
    try:
        if "datetime" not in df.columns:
            return dt.date.today().strftime("%Y%m%d")
        s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
        if s.empty:
            return dt.date.today().strftime("%Y%m%d")
        return s.max().date().strftime("%Y%m%d")
    except Exception:
        return dt.date.today().strftime("%Y%m%d")


def _find_prev_summary_db(target_yyyymmdd: str) -> str:
    try:
        explicit = os.getenv("PREVDAY_INDICATOR_WARMUP_DB_PATH", "").strip()
        if explicit and Path(explicit).exists():
            return explicit
    except Exception:
        pass

    best_date = ""
    best_path = ""
    pattern = re.compile(r"summary(\d{8})\.db$", re.IGNORECASE)
    for base in _summary_base_dirs():
        try:
            if not base.exists():
                continue
            for p in base.glob("summary*.db"):
                m = pattern.search(p.name)
                if not m:
                    continue
                ymd = m.group(1)
                if ymd < target_yyyymmdd and ymd > best_date:
                    best_date = ymd
                    best_path = str(p)
        except Exception:
            continue
    return best_path


def _prevday_symbol_col(df: pd.DataFrame) -> str | None:
    for c in ("symbol", "Symbol", "code", "Code", "stock_code", "銘柄コード"):
        if c in df.columns:
            return c
    return None


def _symbols_from_df(df: pd.DataFrame) -> tuple[str, ...]:
    try:
        c = _prevday_symbol_col(df)
        if not c:
            return tuple()
        syms = (
            df[c]
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
            .str.replace(r"\.T$", "", regex=True)
        )
        return tuple(sorted(x for x in syms.unique().tolist() if x and x.lower() != "nan"))
    except Exception:
        return tuple()


def _prevday_chunks(xs: Iterable[str], n: int) -> Iterable[list[str]]:
    buf: list[str] = []
    for x in xs:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


@lru_cache(maxsize=64)
def _load_prevday_warmup_cached(db_path: str, table: str, symbols: tuple[str, ...], rows_per_symbol: int) -> pd.DataFrame:
    if not db_path or not symbols:
        return pd.DataFrame()
    if rows_per_symbol <= 0:
        return pd.DataFrame()
    try:
        path = Path(db_path)
        if not path.exists():
            return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    try:
        with committing_connect(str(db_path), timeout=2.0) as conn:
            conn.execute("PRAGMA busy_timeout=2000;")
            try:
                cols = [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
            except Exception:
                cols = []
            if not cols:
                return pd.DataFrame()
            sym_col = "symbol" if "symbol" in cols else ("Symbol" if "Symbol" in cols else None)
            dt_col = "datetime" if "datetime" in cols else ("time" if "time" in cols else None)
            if not sym_col or not dt_col:
                return pd.DataFrame()

            # ------------------------------------------------------------
            # 必要列だけ読む (2026-09-03)
            # ------------------------------------------------------------
            # summary テーブルは 166 列ある。SELECT * だと index で行を
            # 引き当てるたびに行本体を取りに行き、SMB では 1 行 1 往復になる。
            # 実測 241.344s / 32,458 行 = 7.4 ms/row。
            # warm 行は指標計算後に _drop_prevday_warmup_rows で捨てられるので、
            # OHLCV + symbol + datetime だけあればよい。
            # 必要列だけにすると warmup 用 covering index が効き、
            # EXPLAIN が SEARCH ... USING COVERING INDEX になって行フェッチが消える。
            # INTRADAY_INDICATOR_WARMUP_SELECT_ALL=1 で従来動作。
            _want_all = str(
                os.environ.get("INTRADAY_INDICATOR_WARMUP_SELECT_ALL", "")
            ).strip().lower() in {"1", "true", "yes", "on"}

            _need = [sym_col, dt_col]
            for _c in ("open", "high", "low", "close", "volume",
                       "open_price", "high_price", "low_price", "close_price",
                       "date", "time", "interval"):
                if _c in cols and _c not in _need:
                    _need.append(_c)
            if _want_all or len(_need) < 4:
                _sel = "*"
            else:
                _sel = ", ".join('"%s"' % c for c in _need)

            for part in _prevday_chunks(symbols, 300):
                placeholders = ",".join(["?"] * len(part))
                params: list[Any] = list(part) + [int(rows_per_symbol)]
                # CAST を外す: CAST("symbol" AS TEXT) だと index が使えない。
                # symbol 列は TEXT affinity なので、そのまま比較しても
                # 数値で入っている値は型親和性で正しく突き合わされる。
                sql = f'''
                    SELECT {_sel} FROM (
                        SELECT {_sel}, ROW_NUMBER() OVER(PARTITION BY "{sym_col}" ORDER BY "{dt_col}" DESC) AS __rn
                        FROM "{table}"
                        WHERE "{sym_col}" IN ({placeholders})
                    )
                    WHERE __rn <= ?
                    ORDER BY "{sym_col}", "{dt_col}"
                '''
                try:
                    d = pd.read_sql_query(sql, conn, params=params)
                except Exception:
                    # Fallback for old SQLite without window functions.
                    limit = max(int(rows_per_symbol) * max(1, len(part)) * 3, int(rows_per_symbol))
                    sql2 = f'''
                        SELECT * FROM "{table}"
                        WHERE CAST("{sym_col}" AS TEXT) IN ({placeholders})
                        ORDER BY "{dt_col}" DESC
                        LIMIT ?
                    '''
                    d = pd.read_sql_query(sql2, conn, params=list(part) + [limit])
                    if not d.empty:
                        d[dt_col] = pd.to_datetime(d[dt_col], errors="coerce")
                        d = d.dropna(subset=[dt_col]).sort_values([sym_col, dt_col])
                        d = d.groupby(sym_col, as_index=False).tail(int(rows_per_symbol))
                if not d.empty:
                    frames.append(d)
    except Exception:
        logger.debug("[IND PREVDAY WARMUP] DB read failed db=%s table=%s", db_path, table, exc_info=True)
        return pd.DataFrame()

    if not frames:
        return pd.DataFrame()
    try:
        out = pd.concat(frames, ignore_index=True, sort=False)
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        if "__rn" in out.columns:
            out = out.drop(columns=["__rn"], errors="ignore")
        return out
    except Exception:
        return pd.DataFrame()


# ============================================================
# 当日イントラデイの warmup history
# ------------------------------------------------------------
# 3分/5分の指標入力は「最新足だけ」で渡ってくる経路がある
# (実測 2026-08-31: [IND] interval=5min hist_max=1〜6)。
# その本数では slope>=3 / RSI>=5 / MACD>=8 を満たせず、
# slope_nonnull=0 / macd_nonnull=0 / technical_ready=0 になり、
# 後段の IND SHORT PATCH が全部を埋めることになっていた。
#
# ここで当日の summary DB (stock_summary_3min / _5min / _1min) から
# 同 interval の過去足を読み、warmup 行として連結する。
# 連結行は計算後に必ず落とすので、保存・表示・ENTRY へは渡らない。
# 未来足は読まない (入力の最大 datetime 以前だけ)。
# ============================================================

_INTRADAY_WARMUP_CACHE: dict[tuple, tuple[float, "pd.DataFrame"]] = {}
_INTRADAY_WARMUP_LOCK = threading.Lock()


def _intraday_required_bars(interval: Any) -> int:
    """この interval で指標計算に欲しい最低本数。"""
    min_slope, min_rsi, min_macd = _min_history_requirements(interval)
    need = max(int(min_slope), int(min_rsi), int(min_macd))
    # MA25/MA75 もシステムが使うので既定は 75 本以上を確保する。
    default_bars = max(need, _prevday_env_int("INTRADAY_INDICATOR_WARMUP_BARS", 90))
    return max(0, int(default_bars))


def _find_today_summary_db(target_yyyymmdd: str) -> str:
    try:
        explicit = os.getenv("INTRADAY_INDICATOR_WARMUP_DB_PATH", "").strip()
        if explicit and Path(explicit).exists():
            return explicit
    except Exception:
        pass
    for base in _summary_base_dirs():
        try:
            p = base / f"summary{target_yyyymmdd}.db"
            if p.exists():
                return str(p)
        except Exception:
            continue
    try:
        from trading.yahoo.pipeline.complement.db import get_summary_db_path

        p = get_summary_db_path(date_yyyymmdd=target_yyyymmdd)
        if p and Path(p).exists():
            return str(p)
    except Exception:
        logger.debug("[IND INTRADAY WARMUP] get_summary_db_path failed", exc_info=True)
    return ""


def _load_intraday_warmup(db_path: str, table: str, symbols: tuple[str, ...], rows_per_symbol: int) -> pd.DataFrame:
    """当日DBからsymbolごと直近N本。TTLキャッシュ付き (DBは増え続けるのでlru_cache不可)。"""
    if not db_path or not symbols or rows_per_symbol <= 0:
        return pd.DataFrame()

    # ------------------------------------------------------------
    # warmup キャッシュ (2026-09-03 実測で TTL を延長)
    # ------------------------------------------------------------
    # [IND STAGE] の実測:
    #     interval=1 intraday_warmup=78.030s compute=9.164s total=87.368s
    #     interval=3 intraday_warmup=14.328s compute=8.301s total=22.702s
    #     interval=5 intraday_warmup= 7.851s compute=6.905s total=14.824s
    # technical 段のコストはほぼ全部この DB 読みで、指標計算自体は 6〜9 秒。
    #
    # 旧 TTL は 20 秒。ranking summary は interval ごとに約 60 秒周期なので
    # 毎回ミスしていた。warmup 行は「確定した過去足」で不変なうえ、
    # _with_intraday_warmup 側が
    #     warm = warm[warm.datetime <= cur_max]                (未来足を混ぜない)
    #     concat(warm, cur).drop_duplicates(keep="last")        (入力行が後勝ち)
    # としているので、warm が多少古くても欠けるのは新しい足だけで、
    # それは cur が供給する。したがって TTL を延ばしても計算結果は変わらない。
    #
    # キーは symbols の完全一致だったため、ランキング銘柄が 1 つ入れ替わるだけで
    # ミスしていた。順序非依存にして取りこぼしを減らす。
    # INTRADAY_INDICATOR_WARMUP_CACHE_SEC で従来値(20)へ戻せる。
    ttl = max(1.0, float(_prevday_env_int("INTRADAY_INDICATOR_WARMUP_CACHE_SEC", 180)))
    key = (db_path, table, tuple(sorted(symbols)), int(rows_per_symbol))
    now_ts = time.monotonic()
    with _INTRADAY_WARMUP_LOCK:
        hit = _INTRADAY_WARMUP_CACHE.get(key)
        if hit and (now_ts - hit[0]) < ttl:
            logger.info(
                "[IND INTRADAY WARMUP] cache hit table=%s symbols=%s age=%.1fs ttl=%.0fs rows=%s",
                table, len(symbols), now_ts - hit[0], ttl, len(hit[1]),
            )
            return hit[1]

    # 読み出し自体は prevday と同じ SQL 構造を使う。
    _t0 = time.perf_counter()
    out = _read_summary_tail(db_path, table, symbols, rows_per_symbol)
    _el = time.perf_counter() - _t0
    if _el >= 3.0:
        logger.warning(
            "[IND INTRADAY WARMUP] cache miss -> DB read %.3fs table=%s symbols=%s rows=%s ttl=%.0fs",
            _el, table, len(symbols), len(out), ttl,
        )

    with _INTRADAY_WARMUP_LOCK:
        if len(_INTRADAY_WARMUP_CACHE) > 32:
            _INTRADAY_WARMUP_CACHE.clear()
        _INTRADAY_WARMUP_CACHE[key] = (now_ts, out)
    return out


def _read_summary_tail(db_path: str, table: str, symbols: tuple[str, ...], rows_per_symbol: int) -> pd.DataFrame:
    """symbolごとに datetime 降順の直近 rows_per_symbol 本を読む。"""
    if not db_path or not symbols or rows_per_symbol <= 0:
        return pd.DataFrame()
    try:
        if not Path(db_path).exists():
            return pd.DataFrame()
    except Exception:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    # 2026-09-17: この読みが 1 回 20〜100 秒 × 1 日 300 回 (合計 7 時間分) NAS を叩いていた。
    #   原因は CAST(symbol AS TEXT) IN (...) + 全表の ROW_NUMBER で索引が効かず全表走査になること。
    #   (1) 当日 DB はローカルのスナップショット (60 秒以内)、前日以前はローカルミラーを読む
    #   (2) CAST を外して索引を使う (無ければ作る。ローカルなので安全)
    try:
        from trading.summary.summary_db_local_mirror import mirror_one as _mirror_one
        local_path = _mirror_one(str(db_path))
    except Exception:
        local_path = str(db_path)
    try:
        with committing_connect(str(local_path), timeout=2.0) as conn:
            conn.execute("PRAGMA busy_timeout=2000;")
            try:
                cols = [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
            except Exception:
                cols = []
            if not cols:
                return pd.DataFrame()
            sym_col = "symbol" if "symbol" in cols else ("Symbol" if "Symbol" in cols else None)
            dt_col = "datetime" if "datetime" in cols else ("time" if "time" in cols else None)
            if not sym_col or not dt_col:
                return pd.DataFrame()
            if str(local_path) != str(db_path):
                try:
                    conn.execute(f'CREATE INDEX IF NOT EXISTS "idx_{table}_sym_dt" ON "{table}"("{sym_col}", "{dt_col}")')
                    conn.commit()
                except Exception:
                    logger.debug("[IND INTRADAY WARMUP] local index create failed table=%s", table, exc_info=True)

            for part in _prevday_chunks(symbols, 300):
                placeholders = ",".join(["?"] * len(part))
                params: list[Any] = list(part) + [int(rows_per_symbol)]
                sql = f'''
                    SELECT * FROM (
                        SELECT *, ROW_NUMBER() OVER(PARTITION BY "{sym_col}" ORDER BY "{dt_col}" DESC) AS __rn
                        FROM "{table}"
                        WHERE "{sym_col}" IN ({placeholders})
                    )
                    WHERE __rn <= ?
                    ORDER BY "{sym_col}", "{dt_col}"
                '''
                try:
                    d = pd.read_sql_query(sql, conn, params=params)
                except Exception:
                    logger.debug("[IND INTRADAY WARMUP] window query failed table=%s", table, exc_info=True)
                    continue
                if d is not None and not d.empty:
                    frames.append(d)
    except Exception:
        logger.debug("[IND INTRADAY WARMUP] DB read failed db=%s table=%s", db_path, table, exc_info=True)
        return pd.DataFrame()

    if not frames:
        return pd.DataFrame()
    try:
        out = pd.concat(frames, ignore_index=True, sort=False)
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        return out.drop(columns=["__rn"], errors="ignore")
    except Exception:
        return pd.DataFrame()


def _hist_len_per_symbol(df: pd.DataFrame) -> pd.Series:
    try:
        d = df.assign(__dt=pd.to_datetime(df["datetime"], errors="coerce")).dropna(subset=["__dt"])
        if d.empty:
            return pd.Series(dtype="int64")
        return d.groupby(d["symbol"].astype(str))["__dt"].nunique()
    except Exception:
        return pd.Series(dtype="int64")


def _with_intraday_warmup(df: pd.DataFrame, *, interval: Any) -> pd.DataFrame:
    """当日DBの同 interval 過去足を warmup として連結する。"""
    if not _prevday_env_bool("INTRADAY_INDICATOR_WARMUP_ENABLED", True):
        return df
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    if _INTRADAY_WARMUP_COL in df.columns:
        return df
    if "datetime" not in df.columns or "symbol" not in df.columns:
        return df

    need_bars = _intraday_required_bars(interval)
    if need_bars <= 0:
        return df

    counts = _hist_len_per_symbol(df)
    min_slope, min_rsi, min_macd = _min_history_requirements(interval)
    required_min = max(int(min_slope), int(min_rsi), int(min_macd))
    if not counts.empty and int(counts.min()) >= required_min:
        # 既に全銘柄が必要本数を満たしている。DBを読まない。
        return df

    symbols = _symbols_from_df(df)
    if not symbols:
        return df

    target_ymd = _prevday_target_date_yyyymmdd(df)
    db_path = _find_today_summary_db(target_ymd)
    if not db_path:
        logger.warning(
            "[IND INTRADAY WARMUP] today summary db not found date=%s interval=%s -> warmup skipped",
            target_ymd, _normalize_interval_name(interval),
        )
        return df

    table = _table_for_interval(interval)
    warm = _load_intraday_warmup(db_path, table, symbols, need_bars)
    if warm is None or warm.empty:
        logger.warning(
            "[IND INTRADAY WARMUP] no rows from today db interval=%s table=%s symbols=%s",
            _normalize_interval_name(interval), table, len(symbols),
        )
        return df

    try:
        cur = df.copy()
        cur["datetime"] = pd.to_datetime(cur["datetime"], errors="coerce")
        cur = cur.dropna(subset=["datetime"])
        if cur.empty:
            return df

        warm = warm.copy()
        warm["datetime"] = pd.to_datetime(warm["datetime"], errors="coerce")
        warm = warm.dropna(subset=["datetime"])
        if warm.empty:
            return df

        # 未来足を混ぜない: 入力の最新時刻より後の行は捨てる。
        cur_max = cur["datetime"].max()
        warm = warm.loc[warm["datetime"] <= cur_max].copy()
        if warm.empty:
            return df

        warm[_INTRADAY_WARMUP_COL] = True
        cur[_INTRADAY_WARMUP_COL] = False

        out = pd.concat([warm, cur], ignore_index=True, sort=False)
        out["symbol"] = out["symbol"].astype(str).str.strip()
        # 同 symbol+datetime は入力行 (後勝ち) を採用する。
        out = out.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

        after = _hist_len_per_symbol(out)
        logger.warning(
            "[IND INTRADAY WARMUP] applied interval=%s table=%s db=%s symbols=%s warm_rows=%s current_rows=%s "
            "hist_before=(min=%s,max=%s) hist_after=(min=%s,max=%s) need=%s bars=%s save=0",
            _normalize_interval_name(interval),
            table,
            db_path,
            len(symbols),
            len(warm),
            len(cur),
            int(counts.min()) if not counts.empty else "-",
            int(counts.max()) if not counts.empty else "-",
            int(after.min()) if not after.empty else "-",
            int(after.max()) if not after.empty else "-",
            required_min,
            need_bars,
        )
        return out
    except Exception:
        logger.exception("[IND INTRADAY WARMUP] concat failed interval=%s", interval)
        return df


def _prevday_warmup_skip_intervals() -> frozenset[str]:
    """前日ウォームアップを走らせない interval。既定は 1 分足。

    2026-09-29: 0a6f222 で目印列を分離した副作用で 1 分足も前日ウォームアップ
    が走るようになり、1 銘柄 90 本 (当日) + 120 本 (前日) になった。
    push_runner が平均 34s / 最大 49s (予算 45s) で毎回破棄され、summary が
    800 秒古いまま [SUMMARY STALE DROP] after=0 になって発注が全滅した。
    1 分足は intraday ウォームアップ 90 本で MA75 まで足りるので前日分は冗長。
    3 分/5 分は当日の足が中央値 5 本 / 3 本しかないので必ず残す。
    """
    raw = os.getenv("PREVDAY_WARMUP_SKIP_INTERVALS", "1min")
    out = set()
    for part in str(raw or "").replace(";", ",").split(","):
        s = part.strip().lower()
        if s:
            out.add(_normalize_interval_name(s))
    return frozenset(out)


# ============================================================
# 2026-09-30: 前日ウォームアップを銘柄単位でキャッシュする
# ------------------------------------------------------------
# _load_prevday_warmup_cached は「銘柄の組 (tuple) そのもの」がキーの
# lru_cache で、監視銘柄が入れ替わるたびに NAS の前日 DB を読み直していた
# (100 銘柄で 1 分足 8.8 秒 / 3 分足 4.3 秒 / 5 分足 3.7 秒)。9/29 に 1 分足で
# 前日ウォームアップを有効にしたとき push_runner の予算 45 秒を超えて summary が
# 全部捨てられたのはこれが効いていた。前日 DB は日中に変わらないので、
# (db, table, 本数) ごとに銘柄単位で持ち、未取得の銘柄だけを読む。
# ============================================================
_PREVDAY_SYMBOL_CACHE: dict[tuple, dict[str, "pd.DataFrame"]] = {}
_PREVDAY_SYMBOL_CACHE_LOCK = threading.Lock()


# ============================================================
# 2026-09-30: 前日の足を 3 つの出所から合わせる
# ------------------------------------------------------------
# 前日 summary DB だけでは足がまばら (監視銘柄 100 のうち 3 分足で 25 本未満が
# 19 銘柄、5 分足は全銘柄 75 本未満)。前日の Yahoo 1 分足 (本物の OHLC、
# 約 20 分遅れで取得したものが引けまで揃う) と前日の ranking スナップショット
# (毎分の現在値) も同じ (銘柄, 足) で重ね、summary > yahoo > ranking の順で採る。
# どれも同じ前営業日 (summary DB のファイル日付) のもの。
# PREVDAY_WARMUP_EXTRA_SOURCES=0 で summary DB だけ (従来)。
# ============================================================
def _prevday_extra_sources_enabled() -> bool:
    return _prevday_env_bool("PREVDAY_WARMUP_EXTRA_SOURCES", True)


def _interval_minutes_from_table(table: str) -> int:
    m = re.search(r"(\d+)min", str(table))
    return int(m.group(1)) if m else 1


def _prevday_resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """1 分足 (symbol, datetime, open, high, low, close, volume) を minutes 分足へ。"""
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
    d = d.dropna(subset=["datetime", "close"])
    if d.empty:
        return pd.DataFrame()
    if minutes <= 1:
        return d[["symbol", "datetime", "open", "high", "low", "close", "volume"]]
    d["slot"] = d["datetime"].dt.floor(f"{minutes}min")
    g = d.sort_values(["symbol", "datetime"]).groupby(["symbol", "slot"], sort=False)
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(),
    }).reset_index().rename(columns={"slot": "datetime"})
    return out


def _prevday_load_yahoo(ymd: str, symbols: tuple, minutes: int) -> pd.DataFrame:
    try:
        path = Path(nas_root()) / "raw_data" / "yahoo" / "intraday" / f"yahoo_1min_{ymd}.db"
        if not path.exists():
            return pd.DataFrame()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            frames = []
            for part in _prevday_chunks(symbols, 300):
                ph = ",".join("?" * len(part))
                frames.append(pd.read_sql_query(
                    f'SELECT symbol, datetime, open, high, low, close, volume FROM yahoo_1min WHERE symbol IN ({ph})',
                    conn, params=list(part)))
        finally:
            conn.close()
        d = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if d.empty:
            return d
        d["symbol"] = d["symbol"].astype(str)
        for c in ("open", "high", "low", "close", "volume"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        return _prevday_resample(d[d["close"] > 0], minutes)
    except Exception:
        logger.debug("[IND PREVDAY WARMUP] yahoo load failed ymd=%s", ymd, exc_info=True)
        return pd.DataFrame()


def _prevday_load_ranking(ymd: str, symbols: tuple, minutes: int) -> pd.DataFrame:
    try:
        from trading.ranking.ranking_db_location import ranking_db_path

        path = ranking_db_path(dt.datetime.strptime(ymd, "%Y%m%d").date())
        if path is None or not Path(path).exists():
            return pd.DataFrame()
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            frames = []
            for part in _prevday_chunks(symbols, 300):
                ph = ",".join("?" * len(part))
                frames.append(pd.read_sql_query(
                    f'SELECT symbol, datetime, COALESCE(NULLIF(current_price, 0), price) AS close, volume '
                    f'FROM ranking_snapshot_1min WHERE symbol IN ({ph})',
                    conn, params=list(part)))
        finally:
            conn.close()
        d = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if d.empty:
            return d
        d["symbol"] = d["symbol"].astype(str)
        d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
        d["close"] = pd.to_numeric(d["close"], errors="coerce")
        d = d.dropna(subset=["datetime", "close"])
        d = d[d["close"] > 0].sort_values(["symbol", "datetime"]).drop_duplicates(["symbol", "datetime"], keep="last")
        # 出来高は累計なので差分にする
        cum = pd.to_numeric(d["volume"], errors="coerce")
        d["volume"] = cum.groupby(d["symbol"]).diff().fillna(0).clip(lower=0)
        d["open"] = d["close"]
        d["high"] = d["close"]
        d["low"] = d["close"]
        return _prevday_resample(d, minutes)
    except Exception:
        logger.debug("[IND PREVDAY WARMUP] ranking load failed ymd=%s", ymd, exc_info=True)
        return pd.DataFrame()


def _prevday_load_union(db_path: str, table: str, symbols: tuple, rows_per_symbol: int) -> pd.DataFrame:
    base = _load_prevday_warmup_cached.__wrapped__(db_path, table, symbols, rows_per_symbol)
    if not _prevday_extra_sources_enabled():
        return base
    m = re.search(r"summary(\d{8})\.db$", str(db_path), re.IGNORECASE)
    if not m:
        return base
    ymd = m.group(1)
    minutes = _interval_minutes_from_table(table)
    parts = []
    if isinstance(base, pd.DataFrame) and not base.empty:
        b = pd.DataFrame({"symbol": base["symbol"].astype(str),
                          "datetime": pd.to_datetime(base["datetime"], errors="coerce")})
        for k in ("open", "high", "low", "close"):
            src = f"{k}_price" if f"{k}_price" in base.columns else k
            b[k] = pd.to_numeric(base[src], errors="coerce") if src in base.columns else np.nan
        b["volume"] = pd.to_numeric(base["volume"], errors="coerce") if "volume" in base.columns else 0.0
        b["__prio"] = 0
        parts.append(b)
    y = _prevday_load_yahoo(ymd, symbols, minutes)
    if not y.empty:
        parts.append(y.assign(__prio=1))
    r = _prevday_load_ranking(ymd, symbols, minutes)
    if not r.empty:
        parts.append(r.assign(__prio=2))
    if not parts:
        return base
    u = pd.concat(parts, ignore_index=True, sort=False)
    u = u.dropna(subset=["datetime", "close"])
    u = u[u["datetime"] < pd.Timestamp(dt.datetime.strptime(ymd, "%Y%m%d").date() + dt.timedelta(days=1))]
    u = u.sort_values(["symbol", "datetime", "__prio"]).drop_duplicates(["symbol", "datetime"], keep="first")
    u = u.groupby("symbol", sort=False).tail(int(rows_per_symbol)).drop(columns=["__prio"])
    for k in ("open", "high", "low", "close"):
        u[f"{k}_price"] = u[k]
    return u.reset_index(drop=True)


def _load_prevday_warmup_per_symbol(db_path: str, table: str, symbols: tuple[str, ...], rows_per_symbol: int) -> pd.DataFrame:
    if not db_path or not symbols or rows_per_symbol <= 0:
        return pd.DataFrame()
    key = (str(db_path), str(table), int(rows_per_symbol))
    with _PREVDAY_SYMBOL_CACHE_LOCK:
        if len(_PREVDAY_SYMBOL_CACHE) > 16:
            _PREVDAY_SYMBOL_CACHE.clear()
        per = _PREVDAY_SYMBOL_CACHE.setdefault(key, {})
        missing = tuple(sorted({str(x) for x in symbols} - set(per)))
    if missing:
        t0 = time.perf_counter()
        loaded = _prevday_load_union(db_path, table, missing, rows_per_symbol)
        got: dict[str, pd.DataFrame] = {}
        if isinstance(loaded, pd.DataFrame) and not loaded.empty:
            sc = "symbol" if "symbol" in loaded.columns else ("Symbol" if "Symbol" in loaded.columns else None)
            if sc is not None:
                for sym, g in loaded.groupby(loaded[sc].astype(str), sort=False):
                    got[str(sym)] = g
        with _PREVDAY_SYMBOL_CACHE_LOCK:
            per = _PREVDAY_SYMBOL_CACHE.setdefault(key, {})
            for sym in missing:
                # 前日 DB に無い銘柄も空で覚えて、毎回読みに行かない
                per[sym] = got.get(sym, pd.DataFrame())
        logger.info(
            "[IND PREVDAY WARMUP] loaded new symbols table=%s n=%s hit=%s elapsed=%.2fs db=%s",
            table, len(missing), len(got), time.perf_counter() - t0, db_path,
        )
    with _PREVDAY_SYMBOL_CACHE_LOCK:
        per = _PREVDAY_SYMBOL_CACHE.get(key, {})
        frames = [per[str(x)] for x in symbols if str(x) in per and not per[str(x)].empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True, sort=False)


def _with_prevday_warmup(df: pd.DataFrame, *, interval: Any) -> pd.DataFrame:
    if not _prevday_env_bool("PREVDAY_INDICATOR_WARMUP_ENABLED", True):
        return df
    _iv_name = _normalize_interval_name(interval)
    if _iv_name in _prevday_warmup_skip_intervals():
        logger.debug(
            "[IND PREVDAY WARMUP] skipped interval=%s (PREVDAY_WARMUP_SKIP_INTERVALS)",
            _iv_name,
        )
        return df
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    if _PREVDAY_WARMUP_COL in df.columns:
        return df
    if "datetime" not in df.columns:
        return df
    symbols = _symbols_from_df(df)
    if not symbols:
        return df
    rows_per_symbol = max(0, _prevday_env_int("PREVDAY_INDICATOR_WARMUP_BARS", 120))
    if rows_per_symbol <= 0:
        return df
    # 2026-09-30: 全銘柄一括の呼び出し (起動時の ranking summary bootstrap
    #   31,560 行 / 約 800 銘柄) では前日を読まない。1 分足 120 本 x 800 銘柄を
    #   NAS から一度に読みに行った直後に main.py が 0xC0000005 で落ちた
    #   (11:55:01、原因は未確定)。監視銘柄 (約 100) の計算には前日を使う。
    _max_syms = _prevday_env_int("PREVDAY_WARMUP_MAX_SYMBOLS", 200)
    if _max_syms > 0 and len(symbols) > _max_syms:
        logger.info(
            "[IND PREVDAY WARMUP] skipped symbols=%s > max=%s interval=%s",
            len(symbols), _max_syms, _iv_name,
        )
        return df
    target_ymd = _prevday_target_date_yyyymmdd(df)
    db_path = _find_prev_summary_db(target_ymd)
    if not db_path:
        return df
    table = _table_for_interval(interval)
    warm = _load_prevday_warmup_per_symbol(db_path, table, symbols, rows_per_symbol)
    if warm.empty:
        return df
    try:
        cur = df.copy()
        cur[_PREVDAY_WARMUP_COL] = False
        warm = warm.copy()
        warm[_PREVDAY_WARMUP_COL] = True
        # Keep only warmup rows older than today's earliest input row per symbol/date.
        warm["datetime"] = pd.to_datetime(warm["datetime"], errors="coerce")
        cur["datetime"] = pd.to_datetime(cur["datetime"], errors="coerce")
        warm = warm.dropna(subset=["datetime"])
        cur = cur.dropna(subset=["datetime"])
        if warm.empty or cur.empty:
            return df
        # 2026-09-28: 上のコメントどおりのフィルタが実装されていなかった。
        #   9/25 のプロセスが 66 時間動き続けた結果 summary20260925.db に
        #   9/28 08:41 までの行が入っており、loader は datetime desc で読むので
        #   「前営業日」ではなく今朝の行が返る。当日行の二重投入を防ぐ。
        if _prevday_env_bool("PREVDAY_WARMUP_STRICT_OLDER", True):
            _cur_min = cur["datetime"].min()
            _before = len(warm)
            warm = warm.loc[warm["datetime"] < _cur_min].copy()
            if len(warm) != _before:
                logger.warning(
                    "[IND PREVDAY WARMUP] dropped non-older warmup rows interval=%s before=%s after=%s cur_min=%s",
                    _normalize_interval_name(interval), _before, len(warm), _cur_min,
                )
            if warm.empty:
                return df
        out = pd.concat([warm, cur], ignore_index=True, sort=False)
        logger.warning(
            "[IND PREVDAY WARMUP] applied interval=%s db=%s table=%s symbols=%s warm_rows=%s current_rows=%s bars=%s save=0",
            _normalize_interval_name(interval),
            db_path,
            table,
            len(symbols),
            len(warm),
            len(cur),
            rows_per_symbol,
        )
        return out
    except Exception:
        logger.debug("[IND PREVDAY WARMUP] concat failed interval=%s", interval, exc_info=True)
        return df


def _drop_prevday_warmup_rows(out: Any) -> Any:
    try:
        # 2026-09-28: 当日イントラデイと前営業日で別の列になったので両方見る。
        #   どちらかの印が付いた行を落とす (従来も当日行は落としていた)。
        _cols = [c for c in (_PREVDAY_WARMUP_COL, _INTRADAY_WARMUP_COL) if isinstance(out, pd.DataFrame) and c in out.columns]
        if _cols:
            before = len(out)
            keep = None
            for _c in _cols:
                _flag = out[_c].fillna(False).astype(bool)
                keep = ~_flag if keep is None else (keep & ~_flag)
            out = out.loc[keep].copy()
            out = out.drop(columns=_cols, errors="ignore")
            dropped = before - len(out)
            if dropped > 0:
                logger.warning("[IND PREVDAY WARMUP] dropped warmup rows after calculation dropped=%s output_rows=%s save=0", dropped, len(out))
            return out
    except Exception:
        logger.debug("[IND PREVDAY WARMUP] drop warmup rows failed", exc_info=True)
    return out


def _add_ma12_features(out: Any) -> Any:
    try:
        if not _prevday_env_bool("SUMMARY_MA12_FEATURES_ENABLED", True):
            return out
        if not isinstance(out, pd.DataFrame) or out.empty:
            return out
        sym_col = _prevday_symbol_col(out)
        if not sym_col:
            return out
        price_col = None
        for c in ("close", "close_price", "price", "current_price"):
            if c in out.columns:
                price_col = c
                break
        if price_col is None:
            return out

        x = out.copy()
        x["__ma12_order"] = range(len(x))
        if "datetime" in x.columns:
            x["__ma12_dt"] = pd.to_datetime(x["datetime"], errors="coerce")
            x = x.sort_values([sym_col, "__ma12_dt", "__ma12_order"], na_position="last")
        else:
            x = x.sort_values([sym_col, "__ma12_order"], na_position="last")

        price = pd.to_numeric(x[price_col], errors="coerce")
        ma12 = price.groupby(x[sym_col], sort=False).transform(lambda s: s.rolling(12, min_periods=1).mean())
        x["ma12"] = ma12
        x["ma12_slope"] = ma12.groupby(x[sym_col], sort=False).diff().fillna(0.0)
        denom = ma12.replace(0, pd.NA)
        x["ma12_slope_pct"] = (x["ma12_slope"] / denom).fillna(0.0)
        x["price_above_ma12"] = (price > ma12).astype(int)
        x["price_below_ma12"] = (price < ma12).astype(int)
        x["ma12_up"] = (x["ma12_slope"] > 0).astype(int)
        x["ma12_down"] = (x["ma12_slope"] < 0).astype(int)
        if "ma5" in x.columns:
            ma5 = pd.to_numeric(x["ma5"], errors="coerce")
            x["ma5_above_ma12"] = (ma5 > ma12).astype(int)
            x["ma5_below_ma12"] = (ma5 < ma12).astype(int)
            x["ma5_ma12_gap_pct"] = ((ma5 - ma12) / denom).fillna(0.0)
        if "ma25" in x.columns:
            ma25 = pd.to_numeric(x["ma25"], errors="coerce")
            x["ma12_above_ma25"] = (ma12 > ma25).astype(int)
            x["ma12_below_ma25"] = (ma12 < ma25).astype(int)
            x["ma12_ma25_gap_pct"] = ((ma12 - ma25) / ma25.replace(0, pd.NA)).fillna(0.0)
        x = x.sort_values("__ma12_order").drop(columns=["__ma12_order", "__ma12_dt"], errors="ignore")
        logger.warning("[MA12 SUMMARY] attached rows=%s symbols=%s cols=ma12,ma12_slope,ma12_slope_pct", len(x), x[sym_col].nunique())
        return x
    except Exception:
        logger.exception("[MA12 SUMMARY] attach failed")
        return out


# ============================================================
# MAIN
# ============================================================

# ============================================================
# profiling ヘルパ (2026-09-03)
# ============================================================
def _ind_safe_len(df) -> int:
    try:
        return int(len(df))
    except Exception:
        return -1


def _ind_safe_nunique(df, col: str) -> int:
    try:
        return int(df[col].nunique())
    except Exception:
        return -1


def _ind_mem_mb(df) -> float:
    """DataFrame の概算メモリ (MB)。deep=True は object 列があると重いので使わない。"""
    try:
        return float(df.memory_usage(index=True).sum()) / 1024.0 / 1024.0
    except Exception:
        return -1.0


def add_all_indicators(
    df: pd.DataFrame,
    *,
    interval="1min",
):
    """warmup (当日イントラデイ + 前営業日) で計算精度を確保し、計算後にwarmup行を落としてma12を付与する。"""
    # ------------------------------------------------------------
    # 区間 profiling (2026-09-03)
    #   [RANKING TECH STAGE] external_indicators=28.923s の内訳を出す。
    #   どこが遅いのかログだけで確定できるようにする。
    # ------------------------------------------------------------
    _p0 = _ind_perf.perf_counter()
    _rows_in = _ind_safe_len(df)
    _syms_in = _ind_safe_nunique(df, "symbol")

    df = _with_intraday_warmup(df, interval=interval)
    _p1 = _ind_perf.perf_counter()
    _rows_intraday = _ind_safe_len(df)

    df = _with_prevday_warmup(df, interval=interval)
    _p2 = _ind_perf.perf_counter()
    _rows_prevday = _ind_safe_len(df)

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            category=PerformanceWarning,
            message=".*DataFrame is highly fragmented.*",
        )
        out = _compute_all_indicators(df, interval=interval)
    _p3 = _ind_perf.perf_counter()

    out = _drop_prevday_warmup_rows(out)
    out = _add_ma12_features(out)
    _p4 = _ind_perf.perf_counter()

    try:
        _total = _p4 - _p0
        _log = logger.warning if _total >= 3.0 else logger.info
        _log(
            "[IND STAGE] interval=%s rows_in=%s symbols_in=%s "
            "intraday_warmup=%.3fs(rows=%s) prevday_warmup=%.3fs(rows=%s) "
            "compute=%.3fs drop_warmup_and_ma12=%.3fs total=%.3fs "
            "rows_out=%s mem_in=%.1fMB mem_out=%.1fMB",
            interval, _rows_in, _syms_in,
            _p1 - _p0, _rows_intraday,
            _p2 - _p1, _rows_prevday,
            _p3 - _p2, _p4 - _p3, _total,
            _ind_safe_len(out), _ind_mem_mb(df), _ind_mem_mb(out),
        )
    except Exception:
        logger.debug("[IND STAGE] ログに失敗", exc_info=True)

    return out


def _compute_all_indicators(
    df: pd.DataFrame,
    *,
    interval="1min",
):
    interval_name = _normalize_interval_name(interval)

    df = _ensure_dataframe(df)

    if df.empty:
        logger.warning("[IND] input dataframe empty")
        return df

    df = df.copy()
    df = _repair_ohlc_alias(df)

    required = {
        "symbol",
        "datetime",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
    }

    df, ok = _ensure_required_columns(df, required)
    if not ok:
        logger.warning(
            "[IND] indicator skipped interval=%s cols=%s",
            interval_name,
            list(df.columns),
        )
        return df

    try:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        try:
            df["datetime"] = df["datetime"].dt.tz_localize(None)
        except Exception:
            pass
    except Exception:
        logger.exception("[IND] datetime cast failed")
        return df

    before_drop = len(df)
    df = df.dropna(subset=["datetime"]).copy()
    dropped = before_drop - len(df)

    if dropped > 0:
        logger.warning("[IND] dropped NaT datetime rows=%s", dropped)

    if df.empty:
        logger.warning("[IND] all rows dropped after datetime sanitize")
        return df

    try:
        df["symbol"] = (
            df["symbol"]
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
        )
    except Exception:
        logger.exception("[IND] symbol normalize failed")

    df = (
        df.sort_values(["symbol", "datetime"], kind="mergesort")
          .drop_duplicates(["symbol", "datetime"], keep="last")
          .reset_index(drop=True)
    )

    out = []

    # ------------------------------------------------------------
    # ここが external_indicators の主コスト (2026-09-03 実測)。
    # 817 銘柄の Python ループ + 817 フレームの concat で 28.6 秒。
    # 1 銘柄あたり約 35ms。銘柄数に完全に比例する。
    # ------------------------------------------------------------
    _loop_t0 = _ind_perf.perf_counter()
    _loop_n = 0
    _loop_slowest = (0.0, "")
    for symbol, g in df.groupby("symbol", sort=False):
        _sym_t0 = _ind_perf.perf_counter()
        built = _build_symbol_indicators(g, str(symbol))
        _sym_dt = _ind_perf.perf_counter() - _sym_t0
        if _sym_dt > _loop_slowest[0]:
            _loop_slowest = (_sym_dt, str(symbol))
        _loop_n += 1
        if built is not None and not built.empty:
            out.append(built)
    _loop_t1 = _ind_perf.perf_counter()

    if not out:
        logger.warning("[IND] no symbol outputs built")
        return df

    df_out = pd.concat(out, ignore_index=True)
    _loop_t2 = _ind_perf.perf_counter()
    df_out = _sanitize_numeric(df_out)
    _loop_t3 = _ind_perf.perf_counter()

    try:
        _loop_total = _loop_t1 - _loop_t0
        _log = logger.warning if (_loop_t3 - _loop_t0) >= 3.0 else logger.info
        _log(
            "[IND COMPUTE] interval=%s symbols=%s rows=%s "
            "symbol_loop=%.3fs (avg=%.1fms slowest=%s@%.0fms) concat=%.3fs sanitize=%.3fs",
            interval_name, _loop_n, len(df),
            _loop_total,
            (_loop_total / _loop_n * 1000.0) if _loop_n else 0.0,
            _loop_slowest[1], _loop_slowest[0] * 1000.0,
            _loop_t2 - _loop_t1, _loop_t3 - _loop_t2,
        )
    except Exception:
        logger.debug("[IND COMPUTE] ログに失敗", exc_info=True)

    # ====================================================
    # symbol history
    # ====================================================
    df_out["symbol_hist_len"] = _build_symbol_hist_len(df_out)
    _log_hist_profile(df_out, interval_name)

    # ====================================================
    # slope
    # ====================================================
    df_out["ma75_slope"] = _calc_slope_by_symbol(df_out, "ma75", 3)
    df_out["vwap_slope"] = _calc_slope_by_symbol(df_out, "vwap", 3)
    df_out["volume_slope"] = _calc_slope_by_symbol(df_out, "volume", 3)

    # ====================================================
    # ATR正規化
    # ====================================================
    atr_s = pd.to_numeric(df_out["atr"], errors="coerce").replace([np.inf, -np.inf], np.nan)
    ma75_slope_s = pd.to_numeric(df_out["ma75_slope"], errors="coerce").replace([np.inf, -np.inf], np.nan)

    df_out["slope_atr_scaled"] = ma75_slope_s / atr_s.replace(0, np.nan)
    df_out["slope_atr_scaled"] = (
        pd.to_numeric(df_out["slope_atr_scaled"], errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .clip(-5, 5)
        .astype("float64")
    )

    # ------------------------------------------------
    # MTF compatibility
    # ------------------------------------------------
    df_out["slope_atr_scaled_1m"] = df_out["slope_atr_scaled"]
    df_out["slope_atr_scaled_3m"] = df_out["slope_atr_scaled"]
    df_out["slope_atr_scaled_5m"] = df_out["slope_atr_scaled"]

    # ====================================================
    # 互換列
    # ====================================================
    df_out["close"] = df_out["close_price"]
    df_out["open"] = df_out["open_price"]
    df_out["high"] = df_out["high_price"]
    df_out["low"] = df_out["low_price"]

    # summary_jobs 互換
    df_out["slope"] = pd.to_numeric(df_out["slope_atr_scaled"], errors="coerce")

    # ====================================================
    # maturity guard
    # ====================================================
    min_slope_hist, min_rsi_hist, min_macd_hist = _min_history_requirements(interval_name)

    hist_len = pd.to_numeric(df_out["symbol_hist_len"], errors="coerce")
    slope_s = pd.to_numeric(df_out["slope_atr_scaled"], errors="coerce").replace([np.inf, -np.inf], np.nan)
    rsi_s = pd.to_numeric(df_out["rsi"], errors="coerce").replace([np.inf, -np.inf], np.nan)
    macd_s = pd.to_numeric(df_out["macd"], errors="coerce").replace([np.inf, -np.inf], np.nan)
    signal_s = pd.to_numeric(df_out["signal"], errors="coerce").replace([np.inf, -np.inf], np.nan)
    close_s = pd.to_numeric(df_out["close_price"], errors="coerce").replace([np.inf, -np.inf], np.nan)

    slope_ready = hist_len >= float(min_slope_hist)
    rsi_ready = hist_len >= float(min_rsi_hist)
    macd_ready = hist_len >= float(min_macd_hist)

    informative_slope = slope_ready & slope_s.notna() & slope_s.ne(0)
    informative_rsi = rsi_ready & rsi_s.notna()
    informative_macd = macd_ready & (
        (macd_s.notna() & macd_s.ne(0))
        | (signal_s.notna() & signal_s.ne(0))
    )

    df_out["technical_ready"] = (
        close_s.notna()
        & (
            informative_slope
            | informative_rsi
            | informative_macd
        )
    ).fillna(False).astype(bool)

    # 実履歴から計算できた行に印を付ける。
    # 後段の IND SHORT PATCH が埋めた行と区別して依存度を測るため。
    df_out["technical_source"] = np.where(
        df_out["technical_ready"].to_numpy(dtype=bool),
        "NATIVE_HISTORY",
        "NONE",
    )

    # ====================================================
    # sanitize / final
    # ====================================================
    df_out = _sanitize_numeric(df_out)

    if "volume" in df_out.columns:
        df_out["volume"] = pd.to_numeric(df_out["volume"], errors="coerce").fillna(0.0)

    for c in ("open_price", "high_price", "low_price", "close_price", "open", "high", "low", "close"):
        if c in df_out.columns:
            df_out[c] = _sanitize_price_like_series(df_out[c])

    try:
        ready_symbols = 0
        if "symbol" in df_out.columns and "technical_ready" in df_out.columns:
            ready_symbols = int(
                df_out.loc[df_out["technical_ready"].fillna(False).astype(bool), "symbol"]
                .astype(str)
                .nunique()
            )

        hist_profile = None
        try:
            hist = (
                df_out.assign(datetime=pd.to_datetime(df_out["datetime"], errors="coerce"))
                     .dropna(subset=["datetime"])
                     .groupby("symbol")["datetime"]
                     .nunique()
                     .astype("float64")
            )
            if not hist.empty:
                hist_profile = (
                    int(hist.min()),
                    float(hist.median()),
                    int(hist.max()),
                )
        except Exception:
            hist_profile = None

        logger.info(
            "[IND] interval=%s rows=%d symbols=%d close_nonnull=%d slope_nonnull=%d slope_nonzero=%d macd_nonnull=%d signal_nonnull=%d technical_ready=%d ready_symbols=%d hist_min=%s hist_median=%s hist_max=%s min_slope=%d min_rsi=%d min_macd=%d",
            interval_name,
            len(df_out),
            int(df_out["symbol"].nunique()) if "symbol" in df_out.columns else 0,
            int(pd.to_numeric(df_out["close_price"], errors="coerce").notna().sum()) if "close_price" in df_out.columns else 0,
            int(pd.to_numeric(df_out["slope_atr_scaled"], errors="coerce").notna().sum()) if "slope_atr_scaled" in df_out.columns else 0,
            int((pd.to_numeric(df_out["slope_atr_scaled"], errors="coerce").fillna(0) != 0).sum()) if "slope_atr_scaled" in df_out.columns else 0,
            int(pd.to_numeric(df_out["macd"], errors="coerce").notna().sum()) if "macd" in df_out.columns else 0,
            int(pd.to_numeric(df_out["signal"], errors="coerce").notna().sum()) if "signal" in df_out.columns else 0,
            int(pd.Series(df_out["technical_ready"]).fillna(False).astype(bool).sum()) if "technical_ready" in df_out.columns else 0,
            ready_symbols,
            hist_profile[0] if hist_profile else "-",
            hist_profile[1] if hist_profile else "-",
            hist_profile[2] if hist_profile else "-",
            min_slope_hist,
            min_rsi_hist,
            min_macd_hist,
        )
    except Exception:
        logger.exception("[IND] final profile log failed")

    try:
        native = int((df_out.get("technical_source") == "NATIVE_HISTORY").sum()) if "technical_source" in df_out.columns else 0
        logger.info(
            "[IND][TECHNICAL SOURCE] interval=%s rows=%d technical_source=NATIVE_HISTORY count=%d none=%d",
            interval_name,
            len(df_out),
            native,
            len(df_out) - native,
        )
    except Exception:
        logger.debug("[IND] technical source log failed", exc_info=True)

    try:
        rsi_series = pd.to_numeric(df_out["rsi"], errors="coerce") if "rsi" in df_out.columns else pd.Series(dtype="float64")
        logger.info(
            "[IND][RSI] interval=%s rsi_nonnull=%d rsi_zero=%d rsi_min=%s rsi_max=%s",
            interval_name,
            int(rsi_series.notna().sum()),
            int((rsi_series.fillna(-999999) == 0).sum()),
            float(rsi_series.min()) if not rsi_series.dropna().empty else float("nan"),
            float(rsi_series.max()) if not rsi_series.dropna().empty else float("nan"),
        )
    except Exception:
        logger.exception("[IND] RSI profile log failed")

    return df_out


# ============================================================
# backward compatible aliases
# ============================================================

def calculate_indicators(df: pd.DataFrame, *, interval="1min"):
    return add_all_indicators(df, interval=interval)


def add_indicators(df: pd.DataFrame, *, interval="1min"):
    return add_all_indicators(df, interval=interval)


__all__ = [
    "add_all_indicators",
    "calculate_indicators",
    "add_indicators",
]