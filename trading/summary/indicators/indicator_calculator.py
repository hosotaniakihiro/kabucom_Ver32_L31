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

import logging
from typing import Iterable

import numpy as np
import pandas as pd

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
# MAIN
# ============================================================

def add_all_indicators(
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

    for symbol, g in df.groupby("symbol", sort=False):
        built = _build_symbol_indicators(g, str(symbol))
        if built is not None and not built.empty:
            out.append(built)

    if not out:
        logger.warning("[IND] no symbol outputs built")
        return df

    df_out = pd.concat(out, ignore_index=True)
    df_out = _sanitize_numeric(df_out)

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