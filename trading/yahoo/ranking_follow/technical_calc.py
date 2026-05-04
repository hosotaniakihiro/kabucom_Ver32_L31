# ============================================================
# File   : trading/yahoo/ranking_follow/technical_calc.py
# Version: PRODUCTION-STABLE-YAHOO-RANKING-FOLLOW-TECH-REV1.0
# ------------------------------------------------------------
# Purpose:
#   Yahoo 1分足DFからテクニカル指標を計算する薄い互換層。
#   既存の indicator_calculator.add_all_indicators があれば優先利用。
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)

PRICE_COLS = ["open", "high", "low", "close", "volume"]


def normalize_ohlcv(df: pd.DataFrame, *, source: str = "yahoo_ranking_follow") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out[out["datetime"].notna()].copy()

    alias_pairs = [
        ("open", "open_price"),
        ("high", "high_price"),
        ("low", "low_price"),
        ("close", "close_price"),
    ]
    for a, b in alias_pairs:
        if a not in out.columns and b in out.columns:
            out[a] = out[b]
        if b not in out.columns and a in out.columns:
            out[b] = out[a]

    for col in ["open", "high", "low", "close", "open_price", "high_price", "low_price", "close_price", "volume"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    if "date" not in out.columns and "datetime" in out.columns:
        out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    if "time" not in out.columns and "datetime" in out.columns:
        out["time"] = out["datetime"].dt.strftime("%H:%M:%S")
    if "source" not in out.columns:
        out["source"] = source

    req = ["symbol", "datetime", "open", "high", "low", "close"]
    for c in req:
        if c not in out.columns:
            out[c] = pd.NA
    out = out[out["symbol"].notna() & out["datetime"].notna() & out["close"].notna()].copy()
    out = out.sort_values(["symbol", "datetime"]).drop_duplicates(["symbol", "datetime"], keep="last")
    return out.reset_index(drop=True)


def _fallback_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = normalize_ohlcv(df)
    if out.empty:
        return out

    frames = []
    for symbol, g in out.groupby("symbol", sort=False):
        x = g.sort_values("datetime").copy()
        close = pd.to_numeric(x["close"], errors="coerce")
        high = pd.to_numeric(x["high"], errors="coerce")
        low = pd.to_numeric(x["low"], errors="coerce")

        x["ma5"] = close.rolling(5, min_periods=1).mean()
        x["ma25"] = close.rolling(25, min_periods=1).mean()
        x["ma75"] = close.rolling(75, min_periods=1).mean()

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
        loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
        rs = gain / loss.replace(0, pd.NA)
        x["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0)

        ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
        x["macd"] = ema12 - ema26
        x["signal"] = x["macd"].ewm(span=9, adjust=False, min_periods=1).mean()
        x["hist"] = x["macd"] - x["signal"]

        prev_close = close.shift(1)
        tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        x["atr_1m"] = tr.rolling(14, min_periods=1).mean().fillna(0.0)
        x["slope"] = close.diff().fillna(0.0)
        x["slope_atr_scaled"] = (x["slope"] / x["atr_1m"].replace(0, pd.NA)).fillna(0.0)
        x["score_slope"] = x["slope_atr_scaled"]
        x["score_mtf"] = x.get("score_mtf", 0.0)
        x["mtf"] = x.get("mtf", 0.0)

        # 最低限のスコア。既存 scoring pipeline が後段で走る場合は上書きされる。
        x["score"] = x["score_slope"].fillna(0.0)
        x["score_buy"] = x["score"].clip(lower=0)
        x["score_sell"] = (-x["score"]).clip(lower=0)
        x["score_total"] = x["score"]
        x["final_score"] = x["score_total"]
        x["display_score"] = x["final_score"]
        x["technical_ready"] = (x["rsi"].notna() & x["macd"].notna()).astype(int)
        frames.append(x)

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def add_technicals(df: pd.DataFrame) -> pd.DataFrame:
    base = normalize_ohlcv(df)
    if base.empty:
        return base

    # 既存の本番インジケータを優先。
    for import_path in (
        "trading.summary.indicators.indicator_calculator",
        "trading.summary.indicator_calculator",
        "indicator_calculator",
    ):
        try:
            mod = __import__(import_path, fromlist=["add_all_indicators"])
            fn = getattr(mod, "add_all_indicators", None)
            if callable(fn):
                out = fn(base.copy())
                out = normalize_ohlcv(out)
                logger.info("[YAHOO RANKING FOLLOW] technicals by %s rows=%s", import_path, len(out))
                return out
        except Exception as e:
            logger.debug("[YAHOO RANKING FOLLOW] indicator import/calc skipped path=%s err=%s", import_path, e)

    out = _fallback_indicators(base)
    logger.info("[YAHOO RANKING FOLLOW] technicals by fallback rows=%s", len(out))
    return out


def filter_calc_output_window(df: pd.DataFrame, *, start, end) -> pd.DataFrame:
    if df is None or df.empty or "datetime" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    return out[(out["datetime"] >= start_ts) & (out["datetime"] <= end_ts)].copy().reset_index(drop=True)
