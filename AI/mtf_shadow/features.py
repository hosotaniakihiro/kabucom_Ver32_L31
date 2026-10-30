# ============================================================
# AI/mtf_shadow/features.py
# ------------------------------------------------------------
# 学習 (tv_csv2) と場中 (mtf_local_bars.get_bars) で共通の特徴量。
#
# 入力は mtf_local_bars._add_indicators を通した足
#   (datetime, open, high, low, close, ma5, ma25, ma75, rsi, macd, signal, hist)。
# 学習側も同じ _add_indicators を import して使う。
#
# - 出来高は使わない (tv_csv2 に出来高の列が無い。6 列目は別銘柄の終値)。
# - 値幅や騰落の窓は「その日の足だけ」で数える。場中の get_bars は当日分しか
#   返さない (前日足は指標の計算にだけ使って捨てる) ので、学習も揃える。
# - 価格の水準に依存しないよう、すべて終値などで割った比にする。
# ============================================================
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURES = [
    "dist_ma5", "dist_ma25", "dist_ma75", "ma5_ma25", "ma25_ma75",
    "rsi", "macd_n", "signal_n", "hist_n", "hist_chg",
    "ret1", "ret3", "ret6", "ret12",
    "body", "range_n", "upper_wick", "lower_wick", "range_mean5",
    "pos20", "tod",
]

# 何分後の騰落を当てるか。1 分足は 15 本、3 分足は 5 本、5 分足は 3 本先。
HORIZON_MIN = 15

# ラベル: 0=下げ 1=変わらず 2=上げ
CLASSES = ["down", "flat", "up"]


def horizon_bars(tf: int) -> int:
    return max(1, HORIZON_MIN // int(tf))


def _div(a, b):
    b = b.replace(0, np.nan) if isinstance(b, pd.Series) else (np.nan if b == 0 else b)
    return a / b


def build_features(bars: pd.DataFrame) -> pd.DataFrame:
    """指標付きの足 → 特徴量 (行は足と同じ並び)。datetime は Timestamp で、日が混ざってよい。"""
    b = bars
    close = b["close"].astype(float)
    open_ = b["open"].astype(float)
    high = b["high"].astype(float)
    low = b["low"].astype(float)
    out = pd.DataFrame(index=b.index)

    out["dist_ma5"] = _div(close, b["ma5"]) - 1
    out["dist_ma25"] = _div(close, b["ma25"]) - 1
    out["dist_ma75"] = _div(close, b["ma75"]) - 1
    out["ma5_ma25"] = _div(b["ma5"], b["ma25"]) - 1
    out["ma25_ma75"] = _div(b["ma25"], b["ma75"]) - 1
    out["rsi"] = b["rsi"].astype(float)
    out["macd_n"] = _div(b["macd"], close)
    out["signal_n"] = _div(b["signal"], close)
    out["hist_n"] = _div(b["hist"], close)

    day = pd.to_datetime(b["datetime"]).dt.normalize()
    g_close = close.groupby(day)
    out["hist_chg"] = _div(b["hist"].groupby(day).diff(), close)
    for n in (1, 3, 6, 12):
        out[f"ret{n}"] = g_close.pct_change(n, fill_method=None)

    out["body"] = _div(close - open_, open_)
    out["range_n"] = _div(high - low, open_)
    out["upper_wick"] = _div(high - np.maximum(open_, close), open_)
    out["lower_wick"] = _div(np.minimum(open_, close) - low, open_)
    out["range_mean5"] = out["range_n"].groupby(day).transform(lambda s: s.rolling(5, min_periods=5).mean())
    hi20 = high.groupby(day).transform(lambda s: s.rolling(20, min_periods=5).max())
    lo20 = low.groupby(day).transform(lambda s: s.rolling(20, min_periods=5).min())
    out["pos20"] = _div(close - lo20, hi20 - lo20)

    t = pd.to_datetime(b["datetime"])
    out["tod"] = (t.dt.hour * 60 + t.dt.minute - 9 * 60).astype(float)

    return out[FEATURES].replace([np.inf, -np.inf], np.nan).astype("float32")


def build_label(bars: pd.DataFrame, tf: int) -> pd.Series:
    """HORIZON_MIN 分後 (同じ日の中だけ) の終値の向き。日をまたぐ所は NaN。"""
    h = horizon_bars(tf)
    close = bars["close"].astype(float)
    day = pd.to_datetime(bars["datetime"]).dt.normalize()
    fwd = close.groupby(day).shift(-h)
    y = pd.Series(np.nan, index=bars.index)
    y[fwd < close] = 0
    y[fwd == close] = 1
    y[fwd > close] = 2
    return y


def build_fwd_return(bars: pd.DataFrame, tf: int) -> pd.Series:
    h = horizon_bars(tf)
    close = bars["close"].astype(float)
    day = pd.to_datetime(bars["datetime"]).dt.normalize()
    return close.groupby(day).shift(-h) / close - 1
