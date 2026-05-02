# ============================================================
# File   : trading/ranking/summary/technicals.py
# Version: Ver1.4-PRODUCTION-RANKING-SUMMARY-TECHNICALS
#          -SAFE-NUMERIC-SERIES-FIX
# ------------------------------------------------------------
# ranking summary 用 technical indicator / fallback indicator 群
# ranking_summary_engine.py から安全に切り出すためのモジュール
# ------------------------------------------------------------
# 🔥 Ver1.4 修正:
# ✔ x.get("sum_volume") 等による scalar 化を全面回避
# ✔ pd.NA + float64 問題を回避
# ✔ _safe_numeric_series を本番用に強化
# ✔ open/high/low/close/volume 生成を安全化
# ✔ volume 欠損時も numpy.float64.fillna エラーを出さない
# ✔ external indicator input の数値変換を安全化
# ✔ fallback indicator 内の pd.NA 数値演算を np.nan ベースへ安全化
# ✔ slope / rsi / macd / hist の thin history 制御は維持
# ✔ technical_ready / hist_len / best_rank 補修は維持
# ✔ 機能削除ゼロ
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from trading.ranking.summary.symbol_metadata import (
    _ensure_symbolname,
)
from trading.ranking.summary.snapshot_normalizer import (
    _sort_if_possible,
)

logger = logging.getLogger(__name__)

TECH_MIN_BARS_FOR_SLOPE = {
    1: 2,
    3: 2,
    5: 2,
}

TECH_MIN_BARS_FOR_RSI = {
    1: 5,
    3: 3,
    5: 3,
}

TECH_MIN_BARS_FOR_MACD = {
    1: 5,
    3: 3,
    5: 3,
}

TECHNICAL_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ma5",
    "ma25",
    "ma75",
    "rsi",
    "macd",
    "signal",
    "hist",
    "atr",
    "vwap",
    "slope_atr_scaled",
    "slope",
]

_LAST_INDICATOR_MODE = "unresolved"  # external / fallback / unresolved
_EXTERNAL_INDICATOR_FN = None


def set_indicator_mode(mode: str) -> None:
    global _LAST_INDICATOR_MODE
    try:
        m = str(mode).strip().lower()
        if not m:
            return
        _LAST_INDICATOR_MODE = m
    except Exception:
        logger.exception("[RANKING SUMMARY] set indicator mode failed")


def get_indicator_mode() -> str:
    try:
        return str(_LAST_INDICATOR_MODE)
    except Exception:
        return "unresolved"


def _resolve_external_indicator_fn():
    global _EXTERNAL_INDICATOR_FN

    if _EXTERNAL_INDICATOR_FN is not None:
        return _EXTERNAL_INDICATOR_FN

    candidate_imports = [
        ("trading.summary.indicators.indicator_calculator", "calculate_indicators"),
        ("trading.summary.indicators.indicator_calculator", "apply_indicators"),
        ("trading.summary.indicators.indicator_calculator", "calculate_summary_indicators"),
        ("trading.summary.indicators.indicator_calculator", "add_all_indicators"),
        ("trading.summary.indicator_calculator", "calculate_indicators"),
        ("trading.summary.indicator_calculator", "apply_indicators"),
        ("trading.summary.indicator_calculator", "calculate_summary_indicators"),
        ("trading.summary.indicator_calculator", "add_all_indicators"),
        ("trading.summary.calculator.indicator_calculator", "calculate_indicators"),
        ("trading.summary.calculator.indicator_calculator", "apply_indicators"),
        ("trading.summary.calculator.indicator_calculator", "add_all_indicators"),
    ]

    for mod_name, fn_name in candidate_imports:
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                _EXTERNAL_INDICATOR_FN = fn
                logger.info(
                    "[RANKING SUMMARY] external indicator resolved: %s.%s",
                    mod_name,
                    fn_name,
                )
                return _EXTERNAL_INDICATOR_FN
        except Exception:
            continue

    _EXTERNAL_INDICATOR_FN = False
    logger.warning("[RANKING SUMMARY] external indicator_calculator not found -> fallback mode")
    return None


def _ensure_columns(df: pd.DataFrame, cols) -> pd.DataFrame:
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()

    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA

    return out


def _safe_numeric_series(
    df: pd.DataFrame,
    col: str,
    *,
    default: float | None = np.nan,
    fill: bool = False,
) -> pd.Series:
    """
    df[col] を安全に float64 Series として返す。

    重要:
      - df.get(col) は使わない
      - 列がない場合でも scalar を返さず Series を返す
      - 重複列により DataFrame が返る場合は先頭列を採用
      - pd.NA + dtype=float64 問題を避ける
      - 文字列数値 / カンマ / 円 / 空文字 / <NA> を安全に処理する
    """
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.Series(dtype="float64")

    if col not in df.columns:
        if default is None:
            return pd.Series(np.nan, index=df.index, dtype="float64")
        return pd.Series(float(default), index=df.index, dtype="float64")

    try:
        s = df[col]

        if isinstance(s, pd.DataFrame):
            if s.shape[1] == 0:
                if default is None:
                    return pd.Series(np.nan, index=df.index, dtype="float64")
                return pd.Series(float(default), index=df.index, dtype="float64")
            s = s.iloc[:, 0]

        if getattr(s, "dtype", None) == object:
            s = (
                s.astype(str)
                .str.replace(",", "", regex=False)
                .str.replace("円", "", regex=False)
                .str.strip()
                .replace(
                    {
                        "": np.nan,
                        "None": np.nan,
                        "none": np.nan,
                        "NULL": np.nan,
                        "null": np.nan,
                        "nan": np.nan,
                        "NaN": np.nan,
                        "<NA>": np.nan,
                        "pd.NA": np.nan,
                    }
                )
            )

        out = pd.to_numeric(s, errors="coerce")

        if not isinstance(out, pd.Series):
            out = pd.Series(out, index=df.index, dtype="float64")

        out = out.astype("float64")

        if fill:
            if default is None:
                out = out.fillna(np.nan)
            else:
                out = out.fillna(float(default))

        return out

    except Exception:
        logger.exception("[RANKING SUMMARY] _safe_numeric_series failed col=%s", col)
        if default is None:
            return pd.Series(np.nan, index=df.index, dtype="float64")
        return pd.Series(float(default), index=df.index, dtype="float64")


def _safe_datetime_series(
    df: pd.DataFrame,
    col: str,
) -> pd.Series:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.Series(dtype="datetime64[ns]")

    if col not in df.columns:
        return pd.Series(pd.NaT, index=df.index)

    try:
        s = df[col]
        if isinstance(s, pd.DataFrame):
            if s.shape[1] == 0:
                return pd.Series(pd.NaT, index=df.index)
            s = s.iloc[:, 0]
        return pd.to_datetime(s, errors="coerce")
    except Exception:
        logger.exception("[RANKING SUMMARY] _safe_datetime_series failed col=%s", col)
        return pd.Series(pd.NaT, index=df.index)


def _build_ohlcv_compatible(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking snapshot 由来データを OHLCV compatible に変換する。

    方針:
      - ランキングsnapshotは約定足ではない
      - したがって擬似OHLCはすべて同値にする
      - open = high = low = close = snapshot時点価格
      - volume は可能な候補列から取得
      - PUSH由来サマリーとの補完は後段で行う

    価格候補:
      current_price
      last_price
      price
      close
      現在値

    出来高候補:
      trading_volume
      volume
      sum_volume
      売買高
    """
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()

    # --------------------------------------------------------
    # price: ranking snapshot 時点価格
    # --------------------------------------------------------
    price = pd.Series(np.nan, index=x.index, dtype="float64")

    for col in [
        "current_price",
        "last_price",
        "price",
        "close",
        "現在値",
        "株価",
    ]:
        if col not in x.columns:
            continue

        s = _safe_numeric_series(x, col, default=np.nan)
        price = price.where(price.notna(), s)

    # 既存の first/max/min/last_price 系がある場合の保険
    for col in [
        "first_price",
        "max_price",
        "min_price",
    ]:
        if col not in x.columns:
            continue

        s = _safe_numeric_series(x, col, default=np.nan)
        price = price.where(price.notna(), s)

    # --------------------------------------------------------
    # OHLC はすべて同じ price にする
    # --------------------------------------------------------
    x["open"] = price
    x["high"] = price
    x["low"] = price
    x["close"] = price

    # --------------------------------------------------------
    # volume
    # --------------------------------------------------------
    if "trading_volume" in x.columns:
        x["volume"] = _safe_numeric_series(x, "trading_volume", default=0.0, fill=True)
    elif "volume" in x.columns:
        x["volume"] = _safe_numeric_series(x, "volume", default=0.0, fill=True)
    elif "sum_volume" in x.columns:
        x["volume"] = _safe_numeric_series(x, "sum_volume", default=0.0, fill=True)
    elif "売買高" in x.columns:
        x["volume"] = _safe_numeric_series(x, "売買高", default=0.0, fill=True)
    else:
        x["volume"] = pd.Series(0.0, index=x.index, dtype="float64")

    # --------------------------------------------------------
    # datetime
    # --------------------------------------------------------
    x["datetime"] = _safe_datetime_series(x, "datetime")

    if "end_time" in x.columns:
        x["end_time"] = _safe_datetime_series(x, "end_time")
        x["datetime"] = x["datetime"].fillna(x["end_time"])

    if "start_time" in x.columns:
        x["start_time"] = _safe_datetime_series(x, "start_time")

    if "date" not in x.columns:
        x["date"] = pd.to_datetime(x["datetime"], errors="coerce").dt.date

    if "time" not in x.columns:
        x["time"] = pd.to_datetime(x["datetime"], errors="coerce").dt.time

    if "time_range" not in x.columns and "start_time" in x.columns and "end_time" in x.columns:
        try:
            x["time_range"] = (
                pd.to_datetime(x["start_time"], errors="coerce").dt.strftime("%H:%M")
                + " - "
                + pd.to_datetime(x["end_time"], errors="coerce").dt.strftime("%H:%M")
            )
        except Exception:
            x["time_range"] = pd.NA

    x = _ensure_symbolname(x)

    try:
        logger.info(
            "[RANKING SUMMARY] pseudo OHLCV built rows=%d price_nonnull=%d volume_nonnull=%d ohlc_same=1",
            len(x),
            int(pd.to_numeric(x["close"], errors="coerce").notna().sum()),
            int(pd.to_numeric(x["volume"], errors="coerce").notna().sum()),
        )
    except Exception:
        logger.exception("[RANKING SUMMARY] pseudo OHLCV log failed")

    return x


def _prepare_external_indicator_input(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()

    required = [
        "symbol",
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
    x = _ensure_columns(x, required)

    x["datetime"] = _safe_datetime_series(x, "datetime")
    x["open"] = _safe_numeric_series(x, "open", default=np.nan)
    x["high"] = _safe_numeric_series(x, "high", default=np.nan)
    x["low"] = _safe_numeric_series(x, "low", default=np.nan)
    x["close"] = _safe_numeric_series(x, "close", default=np.nan)
    x["volume"] = _safe_numeric_series(x, "volume", default=0.0, fill=True)

    x["open"] = x["open"].fillna(x["close"])
    x["high"] = x["high"].fillna(x["close"])
    x["low"] = x["low"].fillna(x["close"])
    x["close"] = x["close"].fillna(x["open"])

    x["high"] = pd.concat([x["high"], x["open"], x["close"]], axis=1).max(axis=1)
    x["low"] = pd.concat([x["low"], x["open"], x["close"]], axis=1).min(axis=1)

    if "date" not in x.columns:
        x["date"] = x["datetime"].dt.date
    if "time" not in x.columns:
        x["time"] = x["datetime"].dt.time

    x = x.dropna(subset=["symbol", "datetime", "close"]).copy()
    x = _ensure_symbolname(x)
    x = _sort_if_possible(x, ["symbol", "datetime"])
    x = x.drop_duplicates(subset=["symbol", "datetime"], keep="last")
    return x.reset_index(drop=True)


def _fallback_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    close = pd.to_numeric(close, errors="coerce").astype("float64")

    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    rsi = 100 - (100 / (1 + rs))

    rsi = rsi.where(~avg_loss.eq(0), 100.0)
    rsi = rsi.where(~avg_gain.eq(0), 0.0)
    rsi = pd.to_numeric(rsi, errors="coerce").fillna(50.0)

    return rsi.clip(lower=0.0, upper=100.0).astype("float64")


def _fallback_macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    close = pd.to_numeric(close, errors="coerce").astype("float64")

    ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=1).mean()
    hist = macd - signal

    return (
        pd.to_numeric(macd, errors="coerce").astype("float64"),
        pd.to_numeric(signal, errors="coerce").astype("float64"),
        pd.to_numeric(hist, errors="coerce").astype("float64"),
    )


def _fallback_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = _safe_numeric_series(df, "high", default=np.nan)
    low = _safe_numeric_series(df, "low", default=np.nan)
    close = _safe_numeric_series(df, "close", default=np.nan)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
    return pd.to_numeric(atr, errors="coerce").fillna(0.0).astype("float64")


def _fallback_vwap(df: pd.DataFrame) -> pd.Series:
    price = _safe_numeric_series(df, "close", default=0.0, fill=True)
    volume = _safe_numeric_series(df, "volume", default=0.0, fill=True)

    try:
        cum_pv = (price * volume).groupby(df["symbol"]).cumsum()
        cum_v = volume.groupby(df["symbol"]).cumsum()
        vwap = cum_pv / cum_v.replace(0, np.nan)
        return pd.to_numeric(vwap, errors="coerce").fillna(price).astype("float64")
    except Exception:
        logger.exception("[RANKING SUMMARY] fallback VWAP failed")
        return price.astype("float64")


def _apply_fallback_indicators(df: pd.DataFrame) -> pd.DataFrame:
    set_indicator_mode("fallback")

    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()

    interval_val = 1
    try:
        if "interval" in x.columns and not x["interval"].dropna().empty:
            interval_val = int(pd.to_numeric(x["interval"], errors="coerce").dropna().iloc[-1])
    except Exception:
        interval_val = 1

    min_slope = TECH_MIN_BARS_FOR_SLOPE.get(interval_val, 2)
    min_rsi = TECH_MIN_BARS_FOR_RSI.get(interval_val, 3)
    min_macd = TECH_MIN_BARS_FOR_MACD.get(interval_val, 3)

    out_frames = []
    thin_symbols: list[tuple[str, int]] = []

    for symbol, g in x.groupby("symbol", sort=False):
        gg = g.copy()
        gg = _sort_if_possible(
            gg,
            ["symbol", "end_time" if "end_time" in gg.columns else "datetime"],
        )

        gg["close"] = _safe_numeric_series(gg, "close", default=np.nan)
        gg["open"] = _safe_numeric_series(gg, "open", default=np.nan)
        gg["high"] = _safe_numeric_series(gg, "high", default=np.nan)
        gg["low"] = _safe_numeric_series(gg, "low", default=np.nan)
        gg["volume"] = _safe_numeric_series(gg, "volume", default=0.0, fill=True)

        gg["open"] = gg["open"].fillna(gg["close"])
        gg["high"] = gg["high"].fillna(gg["close"])
        gg["low"] = gg["low"].fillna(gg["close"])
        gg["close"] = gg["close"].fillna(gg["open"])

        gg["high"] = pd.concat([gg["high"], gg["open"], gg["close"]], axis=1).max(axis=1)
        gg["low"] = pd.concat([gg["low"], gg["open"], gg["close"]], axis=1).min(axis=1)

        gg["hist_len"] = range(1, len(gg) + 1)

        gg["ma5"] = gg["close"].rolling(5, min_periods=1).mean()
        gg["ma25"] = gg["close"].rolling(25, min_periods=1).mean()
        gg["ma75"] = gg["close"].rolling(75, min_periods=1).mean()

        gg["rsi"] = pd.Series(np.nan, index=gg.index, dtype="float64")
        gg["macd"] = pd.Series(np.nan, index=gg.index, dtype="float64")
        gg["signal"] = pd.Series(np.nan, index=gg.index, dtype="float64")
        gg["hist"] = pd.Series(np.nan, index=gg.index, dtype="float64")

        if len(gg) >= min_rsi:
            try:
                gg["rsi"] = _fallback_rsi(gg["close"], period=14)
            except Exception:
                logger.exception("[RANKING SUMMARY] fallback RSI failed symbol=%s", symbol)

        if len(gg) >= min_macd:
            try:
                macd, signal, hist = _fallback_macd(gg["close"])
                gg["macd"] = macd
                gg["signal"] = signal
                gg["hist"] = hist
            except Exception:
                logger.exception("[RANKING SUMMARY] fallback MACD failed symbol=%s", symbol)

        gg["atr"] = _fallback_atr(gg, period=14)
        gg["vwap"] = _fallback_vwap(gg)

        diff = gg["close"].diff()
        atr_safe = gg["atr"].replace(0, np.nan)

        slope_atr = (diff / atr_safe).replace([float("inf"), float("-inf")], np.nan)
        fallback_slope = diff.clip(-5.0, 5.0)

        gg["slope_atr_scaled"] = pd.to_numeric(slope_atr, errors="coerce").fillna(fallback_slope)
        gg["slope"] = pd.to_numeric(gg["slope_atr_scaled"], errors="coerce")

        if len(gg) < min_slope:
            gg["slope_atr_scaled"] = np.nan
            gg["slope"] = np.nan

        gg["technical_ready"] = pd.to_numeric(gg["hist_len"], errors="coerce").fillna(0).ge(min_slope)

        if len(gg) < max(min_rsi, min_macd):
            thin_symbols.append((str(symbol), int(len(gg))))

        out_frames.append(gg)

    if not out_frames:
        return x

    out = pd.concat(out_frames, ignore_index=True)
    out = _ensure_symbolname(out)
    out = _sort_if_possible(
        out,
        ["symbol", "end_time" if "end_time" in out.columns else "datetime"],
    )

    try:
        if thin_symbols:
            thin_count = len(thin_symbols)
            sample = ", ".join([f"{s}:{n}" for s, n in thin_symbols[:10]])
            logger.warning(
                "[RANKING SUMMARY] fallback indicators thin history interval=%s thin_symbols=%d sample=%s min_rsi=%d min_macd=%d",
                interval_val,
                thin_count,
                sample,
                min_rsi,
                min_macd,
            )
    except Exception:
        logger.exception("[RANKING SUMMARY] thin history aggregate log failed")

    return out


def _apply_external_indicators(df: pd.DataFrame) -> pd.DataFrame:
    fn = _resolve_external_indicator_fn()
    if not callable(fn):
        return _apply_fallback_indicators(df)

    try:
        payload = _prepare_external_indicator_input(df)
        if payload.empty:
            logger.warning("[RANKING SUMMARY] external indicator input empty -> fallback")
            return _apply_fallback_indicators(df)

        try:
            interval_arg = None
            if "interval" in payload.columns:
                s = pd.to_numeric(payload["interval"], errors="coerce").dropna()
                if not s.empty:
                    interval_arg = int(s.iloc[-1])

            if interval_arg is not None:
                out = fn(payload.copy(), interval=interval_arg)
            else:
                out = fn(payload.copy())
        except Exception:
            try:
                out = fn(payload.copy())
            except Exception:
                logger.exception("[RANKING SUMMARY] external indicator calculator failed -> fallback")
                return _apply_fallback_indicators(df)

        if isinstance(out, pd.DataFrame) and not out.empty:
            out = _ensure_columns(out, TECHNICAL_COLUMNS)
            out = _ensure_symbolname(out)

            try:
                probe_cols = [c for c in ["rsi", "macd", "slope_atr_scaled", "slope"] if c in out.columns]
                informative = 0.0

                if probe_cols:
                    tmp = out[probe_cols].copy()
                    for c in probe_cols:
                        tmp[c] = pd.to_numeric(tmp[c], errors="coerce")
                    informative = float(
                        ((tmp.notna() & tmp.ne(0)).any(axis=1).mean())
                    ) if len(tmp) > 0 else 0.0

                logger.info(
                    "[RANKING SUMMARY] external indicator probe rows=%d informative_ratio=%.4f rsi_nonnull=%d macd_nonnull=%d slope_nonnull=%d",
                    len(out),
                    informative,
                    int(pd.to_numeric(out["rsi"], errors="coerce").notna().sum()) if "rsi" in out.columns else 0,
                    int(pd.to_numeric(out["macd"], errors="coerce").notna().sum()) if "macd" in out.columns else 0,
                    int(pd.to_numeric(out["slope"], errors="coerce").notna().sum()) if "slope" in out.columns else 0,
                )

                if informative <= 0.01:
                    logger.warning(
                        "[RANKING SUMMARY] external indicator looks uninformative ratio=%.4f -> fallback",
                        informative,
                    )
                    return _apply_fallback_indicators(df)

            except Exception:
                logger.exception("[RANKING SUMMARY] external indicator quality check failed")

            set_indicator_mode("external")
            return out

        logger.warning("[RANKING SUMMARY] external indicator returned empty -> fallback")
    except Exception:
        logger.exception("[RANKING SUMMARY] external indicator calculator failed -> fallback")

    return _apply_fallback_indicators(df)


def _merge_indicator_output(base_df: pd.DataFrame, ind_df: pd.DataFrame) -> pd.DataFrame:
    if base_df is None or base_df.empty:
        return pd.DataFrame()

    if ind_df is None or ind_df.empty:
        x = _ensure_columns(base_df.copy(), TECHNICAL_COLUMNS)
        x = _ensure_symbolname(x)
        return x

    base = base_df.copy()
    ind = ind_df.copy()

    if "datetime" in base.columns:
        base["datetime"] = pd.to_datetime(base["datetime"], errors="coerce")
    if "datetime" in ind.columns:
        ind["datetime"] = pd.to_datetime(ind["datetime"], errors="coerce")

    if "end_time" in base.columns:
        base["end_time"] = pd.to_datetime(base["end_time"], errors="coerce")
    if "end_time" in ind.columns:
        ind["end_time"] = pd.to_datetime(ind["end_time"], errors="coerce")

    merge_cols = [c for c in ["symbol", "datetime"] if c in base.columns and c in ind.columns]
    if len(merge_cols) < 2 and all(c in base.columns and c in ind.columns for c in ["symbol", "end_time"]):
        merge_cols = ["symbol", "end_time"]

    if len(merge_cols) < 2:
        x = base.copy()
        for c in TECHNICAL_COLUMNS:
            if c in ind.columns and c not in x.columns:
                try:
                    x[c] = ind[c].values
                except Exception:
                    x[c] = pd.NA
        x = _ensure_columns(x, TECHNICAL_COLUMNS)
        x = _ensure_symbolname(x)
        return x

    ind_keep = merge_cols + [c for c in TECHNICAL_COLUMNS if c in ind.columns]
    ind = ind[ind_keep].copy()
    ind = ind.drop_duplicates(subset=merge_cols, keep="last")

    out = pd.merge(
        base,
        ind,
        on=merge_cols,
        how="left",
        suffixes=("", "_ind"),
    )

    for c in TECHNICAL_COLUMNS:
        alt = f"{c}_ind"
        if alt in out.columns:
            if c in out.columns:
                try:
                    out[c] = out[c].combine_first(out[alt])
                except Exception:
                    out[c] = out[c].where(out[c].notna(), out[alt])
                out.drop(columns=[alt], inplace=True, errors="ignore")
            else:
                out.rename(columns={alt: c}, inplace=True)

    if "slope" not in out.columns:
        if "slope_atr_scaled" in out.columns:
            out["slope"] = pd.to_numeric(out["slope_atr_scaled"], errors="coerce")
        else:
            out["slope"] = pd.Series(np.nan, index=out.index, dtype="float64")
    else:
        slope = pd.to_numeric(out["slope"], errors="coerce")
        if "slope_atr_scaled" in out.columns:
            slope_alt = pd.to_numeric(out["slope_atr_scaled"], errors="coerce")
            out["slope"] = slope.combine_first(slope_alt)
        else:
            out["slope"] = slope

    out = _ensure_columns(out, TECHNICAL_COLUMNS)
    out = _ensure_symbolname(out)
    return out


def _repair_best_rank_for_display(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    x = df.copy()

    if "best_rank_position" not in x.columns:
        x["best_rank_position"] = pd.Series(np.nan, index=x.index, dtype="float64")

    try:
        x["best_rank_position"] = pd.to_numeric(x["best_rank_position"], errors="coerce")
    except Exception:
        x["best_rank_position"] = pd.Series(np.nan, index=x.index, dtype="float64")

    for fallback_col in ("last_rank_position", "avg_rank_position", "rank_position"):
        if fallback_col in x.columns:
            try:
                alt = pd.to_numeric(x[fallback_col], errors="coerce")
                x["best_rank_position"] = x["best_rank_position"].combine_first(alt)
            except Exception:
                logger.exception("[RANKING SUMMARY] best_rank fallback failed col=%s", fallback_col)

    try:
        x.loc[x["best_rank_position"] <= 0, "best_rank_position"] = np.nan
    except Exception:
        pass

    return x


def _apply_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        from trading.ranking.summary.aggregation import AGG_KEEP_ORDER

        base = _build_ohlcv_compatible(df)
        base = _sort_if_possible(base, ["symbol", "datetime"])
        base["hist_len"] = base.groupby("symbol").cumcount() + 1

        ind = _apply_external_indicators(base)
        x = _merge_indicator_output(base, ind)

        x = _sort_if_possible(x, ["symbol", "datetime"])

        if "hist_len" not in x.columns:
            x["hist_len"] = x.groupby("symbol").cumcount() + 1
        else:
            x["hist_len"] = pd.to_numeric(x["hist_len"], errors="coerce")
            miss = x["hist_len"].isna()
            if miss.any():
                x.loc[miss, "hist_len"] = x.loc[miss].groupby("symbol").cumcount() + 1

        interval_val = 1
        try:
            if "interval" in x.columns and not x["interval"].dropna().empty:
                interval_val = int(pd.to_numeric(x["interval"], errors="coerce").dropna().iloc[-1])
        except Exception:
            interval_val = 1

        min_slope = TECH_MIN_BARS_FOR_SLOPE.get(interval_val, 2)
        min_rsi = TECH_MIN_BARS_FOR_RSI.get(interval_val, 3)
        min_macd = TECH_MIN_BARS_FOR_MACD.get(interval_val, 3)

        hist_len_num = pd.to_numeric(x["hist_len"], errors="coerce").fillna(0)

        x["technical_ready"] = hist_len_num.ge(min_slope)

        for c in ["ma5", "ma25", "ma75", "atr", "vwap"]:
            if c in x.columns:
                x[c] = pd.to_numeric(x[c], errors="coerce")

        if "slope_atr_scaled" in x.columns:
            x["slope_atr_scaled"] = pd.to_numeric(x["slope_atr_scaled"], errors="coerce")
            x.loc[hist_len_num < min_slope, "slope_atr_scaled"] = np.nan
        else:
            x["slope_atr_scaled"] = pd.Series(np.nan, index=x.index, dtype="float64")

        if "slope" not in x.columns:
            x["slope"] = pd.to_numeric(x["slope_atr_scaled"], errors="coerce")
        else:
            x["slope"] = pd.to_numeric(x["slope"], errors="coerce").combine_first(
                pd.to_numeric(x["slope_atr_scaled"], errors="coerce")
            )
        x.loc[hist_len_num < min_slope, "slope"] = np.nan

        if "rsi" in x.columns:
            x["rsi"] = pd.to_numeric(x["rsi"], errors="coerce")
            x.loc[hist_len_num < min_rsi, "rsi"] = np.nan
        else:
            x["rsi"] = pd.Series(np.nan, index=x.index, dtype="float64")

        for c in ["macd", "signal", "hist"]:
            if c in x.columns:
                x[c] = pd.to_numeric(x[c], errors="coerce")
                x.loc[hist_len_num < min_macd, c] = np.nan
            else:
                x[c] = pd.Series(np.nan, index=x.index, dtype="float64")

        x = _repair_best_rank_for_display(x)

        try:
            logger.info(
                "[RANKING SUMMARY] technical merge probe rows=%d close_nonnull=%d slope_nonnull=%d rsi_nonnull=%d macd_nonnull=%d hist_ready=%d mode=%s",
                len(x),
                int(_safe_numeric_series(x, "close").notna().sum()),
                int(_safe_numeric_series(x, "slope").notna().sum()),
                int(_safe_numeric_series(x, "rsi").notna().sum()),
                int(_safe_numeric_series(x, "macd").notna().sum()),
                int(pd.to_numeric(x["technical_ready"], errors="coerce").fillna(0).astype(bool).sum()),
                get_indicator_mode(),
            )
        except Exception:
            logger.exception("[RANKING SUMMARY] technical merge probe failed")

        x = _ensure_columns(x, AGG_KEEP_ORDER)
        x = x[AGG_KEEP_ORDER].copy()
        x = _ensure_symbolname(x)

        try:
            hist_stats = pd.to_numeric(x["hist_len"], errors="coerce").dropna()
            if not hist_stats.empty:
                logger.info(
                    "[RANKING SUMMARY] technical applied interval=%s rows=%d hist_len min=%d median=%d max=%d ready=%d mode=%s",
                    interval_val,
                    len(x),
                    int(hist_stats.min()),
                    int(hist_stats.median()),
                    int(hist_stats.max()),
                    int(pd.to_numeric(x["technical_ready"], errors="coerce").fillna(0).astype(bool).sum()),
                    get_indicator_mode(),
                )
        except Exception:
            logger.exception("[RANKING SUMMARY] technical stats log failed")

        return x

    except Exception:
        logger.exception("[RANKING SUMMARY] apply technical indicators failed")
        try:
            from trading.ranking.summary.aggregation import AGG_KEEP_ORDER
            x = _ensure_columns(df.copy(), AGG_KEEP_ORDER)[AGG_KEEP_ORDER].copy()
        except Exception:
            x = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
        x = _ensure_symbolname(x)
        x = _repair_best_rank_for_display(x)
        return x


__all__ = [
    "TECH_MIN_BARS_FOR_SLOPE",
    "TECH_MIN_BARS_FOR_RSI",
    "TECH_MIN_BARS_FOR_MACD",
    "TECHNICAL_COLUMNS",
    "set_indicator_mode",
    "get_indicator_mode",
    "_resolve_external_indicator_fn",
    "_build_ohlcv_compatible",
    "_prepare_external_indicator_input",
    "_fallback_rsi",
    "_fallback_macd",
    "_fallback_atr",
    "_fallback_vwap",
    "_apply_fallback_indicators",
    "_apply_external_indicators",
    "_merge_indicator_output",
    "_repair_best_rank_for_display",
    "_apply_technical_indicators",
]