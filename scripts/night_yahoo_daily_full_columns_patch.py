# ============================================================
# File   : scripts/night_yahoo_daily_full_columns_patch.py
# Version: V1-NIGHT-YAHOO-DAILY-FULL-COLUMNS
# ------------------------------------------------------------
# 夜間Yahoo日足DBに、テクニカル・ローソク足・売買シグナル・
# ランキング指標の主要列を必ず作成・保存するための補強パッチ。
#
# 目的:
#   - 外部モジュール未配置/未import時でも主要指標列を作る
#   - 既存DBに列が足りない場合は ALTER TABLE で列を追加する
#   - up-to-date 銘柄でも最新行の主要列が欠ける場合は直近履歴から再計算する
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

LOG = logging.getLogger("night_yahoo_daily_full_columns_patch")
VERSION = "V1-NIGHT-YAHOO-DAILY-FULL-COLUMNS"
_INSTALLED = False

BASE_COLS = [
    "stock_code", "stock_name", "market", "date",
    "open", "high", "low", "close", "adj_close", "volume",
]

MA_WINDOWS = [5, 10, 20, 25, 50, 75, 200]

CANDLE_COLS = [
    "Candle_Doji", "Candle_DragonflyDoji", "Candle_GravestoneDoji",
    "Candle_BullishMarubozu", "Candle_BearishMarubozu",
    "Candle_Hammer", "Candle_InvertedHammer", "Candle_ShootingStar",
    "Candle_LargeBullish", "Candle_SmallBullish",
    "Candle_LargeBearish", "Candle_SmallBearish",
    "Pattern_ThreeWhiteSoldiers", "Pattern_ThreeBlackCrows",
    "Pattern_MorningStar", "Pattern_EveningStar",
    "Pattern_BullishEngulfing", "Pattern_BearishEngulfing",
    "Pattern_PiercingPattern", "Pattern_DarkCloudCover",
    "Pattern_GapUp", "Pattern_GapDown",
]

BUY_SIGNAL_COLS = [
    "Signal_Golden_Cross", "Signal_New_High_10", "Signal_Break_Res_10",
    "Signal_Double_Bottom", "Signal_Inverse_3Troughs", "Signal_Trendline_Break",
    "Signal_Post_Crash_Rally", "Signal_First_Pullback", "Signal_Half_Eight_Two",
    "Signal_MA25_Lower_Dev", "Signal_Three_Gaps_Down", "Signal_Ake_no_Myojo",
    "Signal_Three_White_Soldiers", "Signal_Rising_Three_Method", "Signal_Abandoned_Child",
    "Signal_Gap_Up", "Signal_Takuri", "Signal_Forces_Line", "Signal_Yagura_Bottom",
    "Signal_Counterattack", "Signal_Engulfing_Bull", "Signal_Oppression", "Signal_Narabired",
    "Signal_Long_Lower_Shadow", "Signal_Tweezers_Bottom", "Signal_Three_Big_Bear",
    "Signal_Last_Bear_Engulf", "Signal_Harami_Cross_Bottom", "Signal_Three_Star_Bottom",
    "Signal_Morning_Gap",
]

SELL_SIGNAL_COLS = [
    "Signal_Dead_Cross", "Signal_New_Low_10", "Signal_Break_Support_10",
    "Signal_MA25_Upper_Dev", "Signal_Three_Gaps_Up", "Signal_Evening_Star",
    "Signal_Three_Black_Crows", "Signal_Piercing_Line", "Signal_Falling_Three_Method",
    "Signal_Spike_Bull_as_Bear", "Signal_Shooting_Star_as_Bear", "Signal_Tweezers_Top",
    "Signal_Gap_Down", "Signal_New_High8", "Signal_Three_Pulling_Do",
    "Signal_Stalled_Line", "Signal_Hanging_Man",
]

RANKING_COLS = [
    "prev_close", "change_1d_pct", "change_3d_pct", "change_5d_pct",
    "date_prev_week", "close_prev_week", "change_last_week_pct",
    "date_prev_month_end", "close_prev_month", "change_last_month_pct",
    "date_prev_year_end", "close_prev_year", "change_last_year_pct",
    "date_prev_same_1y", "close_prev_same_1y", "change_1y_pct",
    "change_from_ytd_low", "change_from_ytd_high",
    "turnover", "limit_range", "limit_up_price", "limit_down_price",
    "Limit_Up_Permanent", "Limit_Up_Touched", "Limit_Down_Permanent", "Limit_Down_Touched",
    "Expand_Limit_Up_Tomorrow", "Expand_Limit_Down_Tomorrow",
]

EXTRA_TECH_COLS = [
    "High_10D", "Low_10D", "High_Value_10Y", "Low_Value_10Y",
    "BB_Mid", "BB_Upper", "BB_Lower", "BB_Std", "BB_Above_Upper", "BB_Below_Lower",
    "RSI", "RSI_Above_70", "RSI_Below_30",
    "Stoch_K", "Stoch_D", "Stoch_Overbought", "Stoch_Oversold",
    "Fast_Stoch_K", "Fast_Stoch_D", "Fast_Stoch_Overbought", "Fast_Stoch_Oversold",
    "Slow_Stoch_K", "Slow_Stoch_D", "Slow_Stoch_Overbought", "Slow_Stoch_Oversold",
    "MACD", "MACD_Signal", "MACD_Diff", "MACD_GC", "MACD_DC", "MACD_Crossover",
    "PSAR", "PSAR_Up_Signal", "PSAR_Down_Signal",
    "OBV", "OBV_MA5", "OBV_MA20",
    "Volume_Ratio_1D", "Volume_Ratio_5D_MA", "Volume_Ratio_20D_MA",
    "High_Reversal_Signal", "Low_Reversal_Signal",
    "PNF_Trend_X", "PNF_Trend_O", "PNF_Reversal_Up", "PNF_Reversal_Down",
    "Short_Perfect_Order", "Short_Reverse_Perfect_Order",
    "Long_Perfect_Order", "Long_Reverse_Perfect_Order",
    "GC_5_25", "DC_5_25",
]

for w in MA_WINDOWS:
    EXTRA_TECH_COLS += [
        f"MA_{w}", f"MA_{w}_Deviation_Pct", f"MA_{w}_Slope",
        f"Breakout_MA_{w}_Above", f"Breakout_MA_{w}_Below",
        f"WMA_{w}", f"SMMA_{w}",
    ]

EXPECTED_COLS = list(dict.fromkeys(BASE_COLS + EXTRA_TECH_COLS + CANDLE_COLS + BUY_SIGNAL_COLS + SELL_SIGNAL_COLS + RANKING_COLS))
SENTINEL_COLS = ["MA_5", "MA_25", "RSI", "MACD", "turnover", "Signal_Golden_Cross", "Signal_Dead_Cross"]


def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=span).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    avg_gain = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = down.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _limit_width(price: Any) -> float:
    try:
        p = float(price)
    except Exception:
        return 20000.0
    thresholds = [
        (0, 100, 30), (100, 200, 50), (200, 500, 80), (500, 700, 100),
        (700, 1000, 150), (1000, 1500, 300), (1500, 2000, 400),
        (2000, 3000, 500), (3000, 5000, 700), (5000, 7000, 1000),
        (7000, 10000, 1500), (10000, 15000, 3000), (15000, 20000, 4000),
        (20000, 30000, 5000), (30000, 50000, 7000), (50000, 70000, 10000),
        (70000, 100000, 15000),
    ]
    for lo, hi, w in thresholds:
        if lo <= p < hi:
            return float(w)
    return 20000.0


def _ensure_numeric(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in ["open", "high", "low", "close", "adj_close", "volume"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    if "adj_close" not in out.columns and "close" in out.columns:
        out["adj_close"] = out["close"]
    return out


def enrich_daily_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    out = _ensure_numeric(out)
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    c = out["close"]
    h = out["high"]
    l = out["low"]
    o = out["open"]
    v = out["volume"].fillna(0)

    for w in MA_WINDOWS:
        ma = _sma(c, w)
        out[f"MA_{w}"] = ma
        out[f"MA_{w}_Deviation_Pct"] = (c - ma) / ma.replace(0, np.nan) * 100
        out[f"MA_{w}_Slope"] = ma.diff()
        out[f"Breakout_MA_{w}_Above"] = ((c.shift(1) <= ma.shift(1)) & (c > ma)).astype(int)
        out[f"Breakout_MA_{w}_Below"] = ((c.shift(1) >= ma.shift(1)) & (c < ma)).astype(int)
        weights = np.arange(1, w + 1)
        out[f"WMA_{w}"] = c.rolling(w, min_periods=w).apply(lambda x, ww=weights: float(np.dot(x, ww) / ww.sum()), raw=True)
        out[f"SMMA_{w}"] = c.ewm(alpha=1 / w, adjust=False, min_periods=w).mean()

    out["High_10D"] = h.rolling(10, min_periods=10).mean()
    out["Low_10D"] = l.rolling(10, min_periods=10).mean()
    out["High_Value_10Y"] = h.rolling(2520, min_periods=1).max()
    out["Low_Value_10Y"] = l.rolling(2520, min_periods=1).min()

    out["BB_Mid"] = c.rolling(10, min_periods=10).mean()
    out["BB_Std"] = c.rolling(10, min_periods=10).std()
    out["BB_Upper"] = out["BB_Mid"] + out["BB_Std"] * 2
    out["BB_Lower"] = out["BB_Mid"] - out["BB_Std"] * 2
    out["BB_Above_Upper"] = (c >= out["BB_Upper"]).astype(int)
    out["BB_Below_Lower"] = (c <= out["BB_Lower"]).astype(int)

    out["RSI"] = _rsi(c, 14)
    out["RSI_Above_70"] = (out["RSI"] >= 70).astype(int)
    out["RSI_Below_30"] = (out["RSI"] <= 30).astype(int)

    low14 = l.rolling(14, min_periods=14).min()
    high14 = h.rolling(14, min_periods=14).max()
    stoch_k = (c - low14) / (high14 - low14).replace(0, np.nan) * 100
    out["Stoch_K"] = stoch_k
    out["Stoch_D"] = stoch_k.rolling(3, min_periods=3).mean()
    out["Stoch_Overbought"] = (out["Stoch_K"] >= 80).astype(int)
    out["Stoch_Oversold"] = (out["Stoch_K"] <= 20).astype(int)
    out["Fast_Stoch_K"] = stoch_k
    out["Fast_Stoch_D"] = out["Fast_Stoch_K"].rolling(3, min_periods=3).mean()
    out["Fast_Stoch_Overbought"] = (out["Fast_Stoch_K"] >= 80).astype(int)
    out["Fast_Stoch_Oversold"] = (out["Fast_Stoch_K"] <= 20).astype(int)
    out["Slow_Stoch_K"] = out["Fast_Stoch_D"]
    out["Slow_Stoch_D"] = out["Slow_Stoch_K"].rolling(3, min_periods=3).mean()
    out["Slow_Stoch_Overbought"] = (out["Slow_Stoch_K"] >= 80).astype(int)
    out["Slow_Stoch_Oversold"] = (out["Slow_Stoch_K"] <= 20).astype(int)

    macd_line = _ema(c, 12) - _ema(c, 26)
    macd_sig = macd_line.ewm(span=9, adjust=False, min_periods=9).mean()
    out["MACD"] = macd_line
    out["MACD_Signal"] = macd_sig
    out["MACD_Diff"] = macd_line - macd_sig
    out["MACD_GC"] = ((macd_line > macd_sig) & (macd_line.shift(1) <= macd_sig.shift(1))).astype(int)
    out["MACD_DC"] = ((macd_line < macd_sig) & (macd_line.shift(1) >= macd_sig.shift(1))).astype(int)
    out["MACD_Crossover"] = ((out["MACD_GC"] == 1) | (out["MACD_DC"] == 1)).astype(int)

    # 簡易PSAR代替: 直近5日安値/高値を方向別に使用
    out["PSAR"] = np.where(c >= c.rolling(5, min_periods=1).mean(), l.rolling(5, min_periods=1).min(), h.rolling(5, min_periods=1).max())
    out["PSAR_Up_Signal"] = ((c > out["PSAR"]) & (c.shift(1) <= out["PSAR"].shift(1))).astype(int)
    out["PSAR_Down_Signal"] = ((c < out["PSAR"]) & (c.shift(1) >= out["PSAR"].shift(1))).astype(int)

    direction = np.sign(c.diff()).fillna(0)
    out["OBV"] = (direction * v).cumsum()
    out["OBV_MA5"] = out["OBV"].rolling(5, min_periods=5).mean()
    out["OBV_MA20"] = out["OBV"].rolling(20, min_periods=20).mean()
    out["Volume_Ratio_1D"] = v / v.shift(1).replace(0, np.nan)
    out["Volume_Ratio_5D_MA"] = v / v.rolling(5, min_periods=5).mean().replace(0, np.nan)
    out["Volume_Ratio_20D_MA"] = v / v.rolling(20, min_periods=20).mean().replace(0, np.nan)
    out["High_Reversal_Signal"] = (h > h.rolling(5, min_periods=5).max().shift(1)).astype(int)
    out["Low_Reversal_Signal"] = (l < l.rolling(5, min_periods=5).min().shift(1)).astype(int)

    step = (c.diff() / 1.0).round()
    out["PNF_Trend_X"] = (step >= 3).astype(int)
    out["PNF_Trend_O"] = (step <= -3).astype(int)
    out["PNF_Reversal_Up"] = ((out["PNF_Trend_O"].shift(1) == 1) & (step >= 3)).astype(int)
    out["PNF_Reversal_Down"] = ((out["PNF_Trend_X"].shift(1) == 1) & (step <= -3)).astype(int)

    out["Short_Perfect_Order"] = ((out["MA_5"] > out["MA_20"]) & (out["MA_20"] > out["MA_50"])).astype(int)
    out["Short_Reverse_Perfect_Order"] = ((out["MA_5"] < out["MA_20"]) & (out["MA_20"] < out["MA_50"])).astype(int)
    out["Long_Perfect_Order"] = ((out["MA_25"] > out["MA_75"]) & (out["MA_75"] > out["MA_200"])).astype(int)
    out["Long_Reverse_Perfect_Order"] = ((out["MA_25"] < out["MA_75"]) & (out["MA_75"] < out["MA_200"])).astype(int)
    out["GC_5_25"] = ((out["MA_5"] > out["MA_25"]) & (out["MA_5"].shift(1) <= out["MA_25"].shift(1))).astype(int)
    out["DC_5_25"] = ((out["MA_5"] < out["MA_25"]) & (out["MA_5"].shift(1) >= out["MA_25"].shift(1))).astype(int)

    real_body = (c - o).abs()
    total_range = (h - l).replace(0, np.nan)
    upper_shadow = h - pd.concat([c, o], axis=1).max(axis=1)
    lower_shadow = pd.concat([c, o], axis=1).min(axis=1) - l
    is_bull = c > o
    is_bear = c < o
    for col in CANDLE_COLS:
        out[col] = out.get(col, 0)
    out["Candle_Doji"] = ((real_body / total_range < 0.05) & (upper_shadow > 0) & (lower_shadow > 0)).astype(int)
    out["Candle_DragonflyDoji"] = ((o == c) & (c == l)).astype(int)
    out["Candle_GravestoneDoji"] = ((o == c) & (c == h)).astype(int)
    out["Candle_BullishMarubozu"] = ((o == l) & (c == h) & is_bull).astype(int)
    out["Candle_BearishMarubozu"] = ((o == h) & (c == l) & is_bear).astype(int)
    out["Candle_Hammer"] = ((lower_shadow > real_body * 2) & (upper_shadow < real_body * 0.5)).astype(int)
    out["Candle_InvertedHammer"] = ((upper_shadow > real_body * 2) & (lower_shadow < real_body * 0.5)).astype(int)
    out["Candle_ShootingStar"] = (is_bear & (upper_shadow > real_body * 2)).astype(int)
    out["Candle_LargeBullish"] = (is_bull & (real_body / total_range > 0.5)).astype(int)
    out["Candle_LargeBearish"] = (is_bear & (real_body / total_range > 0.5)).astype(int)
    out["Candle_SmallBullish"] = (is_bull & (real_body / total_range <= 0.5)).astype(int)
    out["Candle_SmallBearish"] = (is_bear & (real_body / total_range <= 0.5)).astype(int)
    out["Pattern_ThreeWhiteSoldiers"] = (is_bull & is_bull.shift(1) & is_bull.shift(2)).astype(int)
    out["Pattern_ThreeBlackCrows"] = (is_bear & is_bear.shift(1) & is_bear.shift(2)).astype(int)
    out["Pattern_GapUp"] = (o > c.shift(1)).astype(int)
    out["Pattern_GapDown"] = (o < c.shift(1)).astype(int)
    out["Pattern_BullishEngulfing"] = ((c > o) & (c.shift(1) < o.shift(1)) & (o < c.shift(1)) & (c > o.shift(1))).astype(int)
    out["Pattern_BearishEngulfing"] = ((c < o) & (c.shift(1) > o.shift(1)) & (o > c.shift(1)) & (c < o.shift(1))).astype(int)

    out["Signal_Golden_Cross"] = out["GC_5_25"]
    out["Signal_Dead_Cross"] = out["DC_5_25"]
    out["Signal_New_High_10"] = (h == h.rolling(10, min_periods=1).max()).astype(int)
    out["Signal_New_Low_10"] = (l == l.rolling(10, min_periods=1).min()).astype(int)
    out["Signal_Break_Res_10"] = (c > c.rolling(10, min_periods=1).max().shift(1)).astype(int)
    out["Signal_Break_Support_10"] = (c < c.rolling(10, min_periods=1).min().shift(1)).astype(int)
    out["Signal_MA25_Lower_Dev"] = (((out["MA_25"] - c) / out["MA_25"].replace(0, np.nan)) >= 0.05).astype(int)
    out["Signal_MA25_Upper_Dev"] = (((c - out["MA_25"]) / out["MA_25"].replace(0, np.nan)) >= 0.05).astype(int)
    out["Signal_Three_Gaps_Down"] = sum([(o.shift(i) < c.shift(i + 1)) for i in range(4)]).ge(3).astype(int)
    out["Signal_Three_Gaps_Up"] = sum([(o.shift(i) > c.shift(i + 1)) for i in range(4)]).ge(3).astype(int)
    out["Signal_Three_White_Soldiers"] = out["Pattern_ThreeWhiteSoldiers"]
    out["Signal_Three_Black_Crows"] = out["Pattern_ThreeBlackCrows"]
    out["Signal_Gap_Up"] = (o > h.shift(1)).astype(int)
    out["Signal_Gap_Down"] = (o < h.shift(1)).astype(int)
    out["Signal_Post_Crash_Rally"] = (((c / c.shift(3) - 1) <= -0.10) & (c > o)).astype(int)
    out["Signal_Shooting_Star_as_Bear"] = (((c.shift(3) < c.shift(2)) & (c.shift(2) < c.shift(1))) & ((h - c) > (c - o).abs() * 2)).astype(int)
    out["Signal_Tweezers_Bottom"] = ((l == l.shift(1)) & (c > o)).astype(int)
    out["Signal_Tweezers_Top"] = (((h - h.shift(1)).abs() / h.shift(1).replace(0, np.nan) < 0.005) & (c < o)).astype(int)

    # 未実装のシグナル列もDB列としては必ず作る
    for col in BUY_SIGNAL_COLS + SELL_SIGNAL_COLS + CANDLE_COLS:
        if col not in out.columns:
            out[col] = 0

    out["prev_close"] = c.shift(1)
    out["change_1d_pct"] = (c / out["prev_close"] - 1) * 100
    out["change_3d_pct"] = (c / c.shift(3) - 1) * 100
    out["change_5d_pct"] = (c / c.shift(5) - 1) * 100
    out["turnover"] = c * v
    ytd_low = l.groupby(out["date"].dt.year).cummin()
    ytd_high = h.groupby(out["date"].dt.year).cummax()
    out["change_from_ytd_low"] = (c / ytd_low.replace(0, np.nan) - 1) * 100
    out["change_from_ytd_high"] = (c / ytd_high.replace(0, np.nan) - 1) * 100
    out["limit_range"] = out["prev_close"].apply(_limit_width)
    out["limit_up_price"] = out["prev_close"] + out["limit_range"]
    out["limit_down_price"] = out["prev_close"] - out["limit_range"]
    out["Limit_Up_Permanent"] = (c >= out["limit_up_price"]).astype(int)
    out["Limit_Up_Touched"] = (h >= out["limit_up_price"]).astype(int)
    out["Limit_Down_Permanent"] = (c <= out["limit_down_price"]).astype(int)
    out["Limit_Down_Touched"] = (l <= out["limit_down_price"]).astype(int)
    out["Expand_Limit_Up_Tomorrow"] = 0
    out["Expand_Limit_Down_Tomorrow"] = 0

    # 週/月/年の比較日は簡易版。値が取れる場合だけ埋める。
    out["date_prev_week"] = out["date"] - pd.Timedelta(days=7)
    out["date_prev_month_end"] = (out["date"] - pd.offsets.MonthEnd(1)).dt.normalize()
    out["date_prev_year_end"] = (out["date"] - pd.offsets.YearEnd(1)).dt.normalize()
    out["date_prev_same_1y"] = out["date"] - pd.DateOffset(years=1)
    for col in ["close_prev_week", "close_prev_month", "close_prev_year", "close_prev_same_1y", "change_last_week_pct", "change_last_month_pct", "change_last_year_pct", "change_1y_pct"]:
        if col not in out.columns:
            out[col] = np.nan

    for col in EXPECTED_COLS:
        if col not in out.columns:
            out[col] = np.nan

    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return out


def _sqlite_type_from_series(s: pd.Series) -> str:
    if pd.api.types.is_integer_dtype(s) or pd.api.types.is_bool_dtype(s):
        return "INTEGER"
    if pd.api.types.is_float_dtype(s):
        return "REAL"
    return "TEXT"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: list[str], sample: Optional[pd.DataFrame] = None) -> None:
    if not _table_exists(conn, table):
        return
    existing = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col in columns:
        if col in existing:
            continue
        typ = "REAL"
        if sample is not None and col in sample.columns:
            typ = _sqlite_type_from_series(sample[col])
        if col in BASE_COLS or col.startswith("date_"):
            typ = "TEXT"
        conn.execute(f'ALTER TABLE {table} ADD COLUMN "{col}" {typ}')
        existing.add(col)


def _needs_latest_repair(db_path: Path, symbol: str, daily_mod: Any) -> bool:
    try:
        if not Path(db_path).exists():
            return False
        latest_table = getattr(daily_mod, "DB_TABLE_LATEST", "stock_analysis_latest")
        with sqlite3.connect(str(db_path), timeout=10) as conn:
            if not _table_exists(conn, latest_table):
                return False
            cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({latest_table})").fetchall()}
            if any(c not in cols for c in SENTINEL_COLS):
                return True
            select_cols = ", ".join([f'"{c}"' for c in SENTINEL_COLS])
            row = conn.execute(f'SELECT {select_cols} FROM {latest_table} WHERE "stock_code"=?', (symbol,)).fetchone()
            if not row:
                return False
            return any(v is None or v == "" for v in row)
    except Exception:
        LOG.debug("[NIGHT YAHOO DAILY FULLCOL] latest repair check failed symbol=%s", symbol, exc_info=True)
        return False


def _load_recent_prices(db_path: Path, symbol: str, daily_mod: Any, rows: int) -> pd.DataFrame:
    history_table = getattr(daily_mod, "DB_TABLE_HISTORY", "stock_analysis_history")
    with sqlite3.connect(str(db_path), timeout=20) as conn:
        if not _table_exists(conn, history_table):
            return pd.DataFrame()
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({history_table})").fetchall()}
        required = ["stock_code", "date", "open", "high", "low", "close", "volume"]
        if not all(c in cols for c in required):
            return pd.DataFrame()
        adj_expr = '"adj_close"' if "adj_close" in cols else '"close" AS "adj_close"'
        sql = f'''
            SELECT "stock_code" AS "symbol", "date", "open", "high", "low", "close", {adj_expr}, "volume"
            FROM {history_table}
            WHERE "stock_code"=?
            ORDER BY "date" DESC
            LIMIT ?
        '''
        df = pd.read_sql_query(sql, conn, params=(symbol, int(rows)))
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ["open", "high", "low", "close", "adj_close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date")


def _repair_recent_rows(db_path: Path, symbol: str, rec: dict[str, str], daily_mod: Any) -> int:
    rows = int(float(os.environ.get("NIGHT_YAHOO_DAILY_FULLCOL_REPAIR_ROWS", "320")))
    prices = _load_recent_prices(db_path, symbol, daily_mod, rows)
    if prices.empty:
        return 0
    computed = daily_mod._run_indicator_pipeline(prices, rec)
    if computed is None or computed.empty:
        return 0
    hist, _lat = daily_mod._save_symbol_df(db_path, computed)
    return int(hist or 0)


def install(daily_mod: Any) -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if daily_mod is None:
        return False
    if getattr(daily_mod, "_NIGHT_YAHOO_DAILY_FULL_COLUMNS_PATCHED", False):
        _INSTALLED = True
        return True

    original_pipeline = getattr(daily_mod, "_run_indicator_pipeline", None)
    original_save = getattr(daily_mod, "_save_symbol_df", None)
    original_process = getattr(daily_mod, "process_symbol", None)
    if not callable(original_pipeline) or not callable(original_save) or not callable(original_process):
        LOG.warning("[NIGHT YAHOO DAILY FULLCOL] install failed: required functions missing")
        return False

    def pipeline_full_columns(price_df: pd.DataFrame, rec: dict[str, str]) -> pd.DataFrame:
        try:
            out = original_pipeline(price_df, rec)
        except Exception:
            LOG.warning("[NIGHT YAHOO DAILY FULLCOL] original pipeline failed, using fallback", exc_info=True)
            # 元パイプライン失敗時も最低限の形を作る
            p = price_df.copy()
            p["date"] = pd.to_datetime(p["date"], errors="coerce")
            out = p.copy()
            out.insert(0, "market", rec.get("market", ""))
            out.insert(0, "stock_name", rec.get("symbolname", ""))
            out.insert(0, "stock_code", rec.get("symbol", ""))
        try:
            return enrich_daily_columns(out)
        except Exception:
            LOG.warning("[NIGHT YAHOO DAILY FULLCOL] enrich failed", exc_info=True)
            return out

    def save_full_columns(db_path: Path, df: pd.DataFrame):
        if df is not None and not df.empty:
            df = enrich_daily_columns(df)
        result = original_save(db_path, df)
        try:
            with sqlite3.connect(str(db_path), timeout=20) as conn:
                _ensure_columns(conn, getattr(daily_mod, "DB_TABLE_HISTORY", "stock_analysis_history"), EXPECTED_COLS, df)
                _ensure_columns(conn, getattr(daily_mod, "DB_TABLE_LATEST", "stock_analysis_latest"), EXPECTED_COLS, df)
                conn.commit()
        except Exception:
            LOG.warning("[NIGHT YAHOO DAILY FULLCOL] schema ensure failed db=%s", db_path, exc_info=True)
        return result

    def process_symbol_full_columns(rec: dict[str, str], *, period: str, start: Optional[str], db_path: Path):
        symbol, success, msg, nrows = original_process(rec, period=period, start=start, db_path=db_path)
        repair_enabled = str(os.environ.get("NIGHT_YAHOO_DAILY_FULLCOL_REPAIR", "1")).strip().lower() not in {"0", "false", "no", "off"}
        if repair_enabled and success and int(nrows or 0) == 0:
            sym = str(symbol or rec.get("symbol", "")).strip()
            if sym and _needs_latest_repair(Path(db_path), sym, daily_mod):
                try:
                    repaired = _repair_recent_rows(Path(db_path), sym, rec, daily_mod)
                    if repaired > 0:
                        return symbol, True, f"{msg}; full-columns repaired rows={repaired}", repaired
                except Exception:
                    LOG.warning("[NIGHT YAHOO DAILY FULLCOL] repair failed symbol=%s", sym, exc_info=True)
        return symbol, success, msg, nrows

    daily_mod._run_indicator_pipeline = pipeline_full_columns
    daily_mod._save_symbol_df = save_full_columns
    daily_mod.process_symbol = process_symbol_full_columns
    daily_mod._NIGHT_YAHOO_DAILY_FULL_COLUMNS_PATCHED = True
    try:
        daily_mod.VERSION = "V4-NIGHT-YAHOO-DAILY-INCREMENTAL-FULL-COLUMNS"
    except Exception:
        pass

    _INSTALLED = True
    LOG.warning(
        "[NIGHT YAHOO DAILY FULLCOL] installed version=%s expected_cols=%s sentinel=%s repair=%s",
        VERSION,
        len(EXPECTED_COLS),
        ",".join(SENTINEL_COLS),
        os.environ.get("NIGHT_YAHOO_DAILY_FULLCOL_REPAIR", "1"),
    )
    return True
