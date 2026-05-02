# ============================================================
# File   : trading/scoring/flags/time_window_flags.py
# Version: Ver1.0-PRODUCTION-TIME-WINDOW-FLAGS
# ------------------------------------------------------------
# Function:
#   - 日本株向け時間帯フラグ生成
#   - 寄り付き / 前引け前 / 後場寄り / 14時台 / 引け前
#   - 時間帯押し目シグナルの土台を作る
#   - pullback / rebound / vwap / breakout 系と組み合わせやすい
# ------------------------------------------------------------
# Main flags:
#   ✔ flag_open_0900_0905
#   ✔ flag_open_0900_0910
#   ✔ flag_open_pullback_0910_0930
#   ✔ flag_morning_0930_1030
#   ✔ flag_pre_lunch_1100_1130
#   ✔ flag_pre_lunch_pullback
#   ✔ flag_afternoon_open_1230_1300
#   ✔ flag_afternoon_open_reclaim
#   ✔ flag_afternoon_1300_1400
#   ✔ flag_reentry_1400_1500
#   ✔ flag_close_retry_1500_1530
#   ✔ flag_first_pullback_after_open
#   ✔ flag_lunch_break_hold
#   ✔ flag_time_decay_pullback_resolved
# ------------------------------------------------------------
# Notes:
#   - datetime が必要
#   - datetime は naive / aware どちらでも可
#   - timezone は入力値をそのまま使う
#   - 日本株の前場 09:00-11:30 / 後場 12:30-15:30 前提
# ============================================================

from __future__ import annotations

import pandas as pd
import numpy as np


# ============================================================
# safe helpers
# ============================================================

def _safe_datetime(series):
    if series is None:
        return None
    try:
        return pd.to_datetime(series, errors="coerce")
    except Exception:
        return None


def _safe_numeric(series):
    if series is None:
        return None
    try:
        s = pd.to_numeric(series, errors="coerce")
        if isinstance(s, pd.Series):
            s = s.replace([np.inf, -np.inf], np.nan)
        return s
    except Exception:
        return None


def _col(df, *names):
    lower_map = {str(c).lower(): c for c in df.columns}
    for n in names:
        if n in df.columns:
            return df[n]
        key = str(n).lower()
        if key in lower_map:
            return df[lower_map[key]]
    return None


def _flag(expr, index=None):
    try:
        return expr.fillna(False).astype(int)
    except Exception:
        if index is None:
            return pd.Series(dtype="int64")
        return pd.Series(0, index=index, dtype="int64")


# ============================================================
# time helpers
# ============================================================

def _hhmm_from_dt(dt_series: pd.Series) -> pd.Series:
    return dt_series.dt.hour * 100 + dt_series.dt.minute


def _between_hhmm(hhmm: pd.Series, start_hhmm: int, end_hhmm: int) -> pd.Series:
    return (hhmm >= start_hhmm) & (hhmm < end_hhmm)


def _session_flags(dt_series: pd.Series) -> dict[str, pd.Series]:
    hhmm = _hhmm_from_dt(dt_series)

    is_morning = _between_hhmm(hhmm, 900, 1130)
    is_lunch = _between_hhmm(hhmm, 1130, 1230)
    is_afternoon = _between_hhmm(hhmm, 1230, 1530)
    is_market = is_morning | is_afternoon

    return {
        "hhmm": hhmm,
        "is_morning": is_morning,
        "is_lunch": is_lunch,
        "is_afternoon": is_afternoon,
        "is_market": is_market,
    }


# ============================================================
# per symbol/day engine
# ============================================================

def _apply_time_flags_per_group(g: pd.DataFrame) -> pd.DataFrame:
    g = g.copy()

    dt = _safe_datetime(g["datetime"])
    if dt is None:
        # flag列だけ保証
        for c in [
            "flag_open_0900_0905",
            "flag_open_0900_0910",
            "flag_open_pullback_0910_0930",
            "flag_morning_0930_1030",
            "flag_pre_lunch_1100_1130",
            "flag_pre_lunch_pullback",
            "flag_afternoon_open_1230_1300",
            "flag_afternoon_open_reclaim",
            "flag_afternoon_1300_1400",
            "flag_reentry_1400_1500",
            "flag_close_retry_1500_1530",
            "flag_first_pullback_after_open",
            "flag_lunch_break_hold",
            "flag_time_decay_pullback_resolved",
            "flag_market_open_window",
            "flag_market_close_window",
            "flag_morning_session",
            "flag_afternoon_session",
            "flag_lunch_break",
        ]:
            g[c] = 0
        return g

    sf = _session_flags(dt)
    hhmm = sf["hhmm"]

    close = _safe_numeric(_col(g, "close", "close_price", "price", "last_price"))
    open_p = _safe_numeric(_col(g, "open", "open_price"))
    high = _safe_numeric(_col(g, "high", "high_price"))
    low = _safe_numeric(_col(g, "low", "low_price"))
    volume = _safe_numeric(_col(g, "volume"))
    vwap = _safe_numeric(_col(g, "vwap"))
    ma25 = _safe_numeric(_col(g, "ma25"))
    ma5 = _safe_numeric(_col(g, "ma5"))

    idx = g.index

    # fallback
    if close is None:
        close = pd.Series(np.nan, index=idx)
    if open_p is None:
        open_p = pd.Series(np.nan, index=idx)
    if high is None:
        high = pd.Series(np.nan, index=idx)
    if low is None:
        low = pd.Series(np.nan, index=idx)
    if volume is None:
        volume = pd.Series(np.nan, index=idx)
    if vwap is None:
        vwap = pd.Series(np.nan, index=idx)
    if ma25 is None:
        ma25 = pd.Series(np.nan, index=idx)
    if ma5 is None:
        ma5 = pd.Series(np.nan, index=idx)

    # --------------------------------------------------------
    # session basic
    # --------------------------------------------------------
    g["flag_morning_session"] = _flag(sf["is_morning"], idx)
    g["flag_afternoon_session"] = _flag(sf["is_afternoon"], idx)
    g["flag_lunch_break"] = _flag(sf["is_lunch"], idx)

    # --------------------------------------------------------
    # open / close windows
    # --------------------------------------------------------
    g["flag_open_0900_0905"] = _flag(_between_hhmm(hhmm, 900, 905), idx)
    g["flag_open_0900_0910"] = _flag(_between_hhmm(hhmm, 900, 910), idx)
    g["flag_market_open_window"] = _flag(_between_hhmm(hhmm, 900, 930), idx)

    g["flag_morning_0930_1030"] = _flag(_between_hhmm(hhmm, 930, 1030), idx)
    g["flag_pre_lunch_1100_1130"] = _flag(_between_hhmm(hhmm, 1100, 1130), idx)

    g["flag_afternoon_open_1230_1300"] = _flag(_between_hhmm(hhmm, 1230, 1300), idx)
    g["flag_afternoon_1300_1400"] = _flag(_between_hhmm(hhmm, 1300, 1400), idx)
    g["flag_reentry_1400_1500"] = _flag(_between_hhmm(hhmm, 1400, 1500), idx)

    g["flag_close_retry_1500_1530"] = _flag(_between_hhmm(hhmm, 1500, 1530), idx)
    g["flag_market_close_window"] = _flag(_between_hhmm(hhmm, 1450, 1530), idx)

    # --------------------------------------------------------
    # open impulse / post-open pullback
    # --------------------------------------------------------
    # 寄り直後急騰の簡易判定
    ret1 = close.pct_change()
    ret3 = close.pct_change(3)
    vol_ma10 = volume.rolling(10, min_periods=3).mean()

    open_impulse = (
        _between_hhmm(hhmm, 900, 910) &
        (
            (ret1 > 0.01) |
            (ret3 > 0.015)
        ) &
        (
            (volume > vol_ma10 * 1.5) |
            volume.isna()
        )
    )

    # 9:10～9:30 押し目
    # 朝に急騰したあと、MA/VWAP近辺まで押して再浮上しやすい帯
    touch_support = (
        ((close > ma25) & (close.shift(1) <= ma25.shift(1))) |
        ((close > ma5) & (close.shift(1) <= ma5.shift(1))) |
        ((close > vwap) & (close.shift(1) <= vwap.shift(1)))
    )

    g["flag_open_pullback_0910_0930"] = _flag(
        _between_hhmm(hhmm, 910, 930) &
        (
            open_impulse.shift(1).rolling(5, min_periods=1).max().fillna(False).astype(bool) |
            open_impulse.shift(2).rolling(5, min_periods=1).max().fillna(False).astype(bool) |
            open_impulse.shift(3).rolling(5, min_periods=1).max().fillna(False).astype(bool)
        ) &
        (
            touch_support |
            ((ret1 > 0) & (close > open_p))
        ),
        idx,
    )

    # --------------------------------------------------------
    # first pullback after open
    # 初動後の最初の押しだけを狙う土台
    # 寄り後30分以内で、直前まで上昇 → 今バーで小反落 → 次バー以降再浮上を想定
    # --------------------------------------------------------
    rising_before = (close.shift(1) > close.shift(2)) & (close.shift(2) > close.shift(3))
    mild_pullback = (close < close.shift(1)) & (close >= close.shift(1) * 0.985)

    g["flag_first_pullback_after_open"] = _flag(
        _between_hhmm(hhmm, 905, 930) &
        rising_before &
        mild_pullback,
        idx,
    )

    # --------------------------------------------------------
    # pre-lunch pullback
    # 前引け前の押し
    # --------------------------------------------------------
    g["flag_pre_lunch_pullback"] = _flag(
        _between_hhmm(hhmm, 1100, 1130) &
        (
            (close < close.shift(1)) |
            ((low <= ma25) & (close >= ma25)) |
            ((low <= vwap) & (close >= vwap))
        ),
        idx,
    )

    # --------------------------------------------------------
    # afternoon open reclaim
    # 後場寄りGU/GD否定から反発 / 後場寄り回復
    # --------------------------------------------------------
    afternoon_open_reclaim = (
        _between_hhmm(hhmm, 1230, 1300) &
        (
            ((close > vwap) & (close.shift(1) <= vwap.shift(1))) |
            ((close > ma25) & (close.shift(1) <= ma25.shift(1))) |
            ((close > open_p) & (close.shift(1) < close.shift(1)))
        )
    )

    g["flag_afternoon_open_reclaim"] = _flag(afternoon_open_reclaim, idx)

    # --------------------------------------------------------
    # lunch break hold
    # 昼休み跨ぎでも5分足トレンド維持 の簡易土台
    # 11:25〜11:30 強く、12:30以降も崩れていない
    # --------------------------------------------------------
    pre_lunch_strong = (
        _between_hhmm(hhmm, 1125, 1130) &
        (close >= ma25) &
        ((close >= vwap) | vwap.isna())
    )

    g["flag_lunch_break_hold"] = _flag(
        _between_hhmm(hhmm, 1230, 1300) &
        pre_lunch_strong.shift(1).rolling(10, min_periods=1).max().fillna(False).astype(bool) &
        (close >= ma25) &
        ((close >= vwap) | vwap.isna()),
        idx,
    )

    # --------------------------------------------------------
    # 14時台の再資金流入
    # --------------------------------------------------------
    g["flag_reentry_1400_1500"] = _flag(
        _between_hhmm(hhmm, 1400, 1500) &
        (
            (volume > vol_ma10 * 1.5) |
            (close > close.shift(1))
        ) &
        (
            ((close > vwap) & (close.shift(1) <= vwap.shift(1))) |
            ((close > ma25) & (close.shift(1) <= ma25.shift(1))) |
            (close > high.shift(1))
        ),
        idx,
    )

    # --------------------------------------------------------
    # 引け前の高値再トライ
    # --------------------------------------------------------
    rolling_high_20 = high.shift(1).rolling(20, min_periods=5).max()
    g["flag_close_retry_1500_1530"] = _flag(
        _between_hhmm(hhmm, 1500, 1530) &
        (
            (close >= rolling_high_20 * 0.995) |
            (high >= rolling_high_20)
        ) &
        (
            (close > open_p) |
            (volume > vol_ma10)
        ),
        idx,
    )

    # --------------------------------------------------------
    # 朝高後の押しで11時まで崩れない
    # --------------------------------------------------------
    morning_high_early = high.where(_between_hhmm(hhmm, 900, 930)).ffill()
    g["flag_morning_high_hold_to_1100"] = _flag(
        _between_hhmm(hhmm, 930, 1100) &
        (close >= ma25) &
        (
            close >= morning_high_early * 0.985
        ),
        idx,
    )

    # --------------------------------------------------------
    # 時間経過で押しが消化される
    # 押し発生後、数本で再上昇
    # 長時間だらだら下げない
    # --------------------------------------------------------
    pullback_now = (
        (close < close.shift(1)) &
        (close >= close.shift(1) * 0.985)
    )
    rebound_soon = (
        (close > close.shift(1)) &
        (
            (close > ma5) |
            (close > vwap) |
            (close > open_p)
        )
    )

    g["flag_time_decay_pullback_resolved"] = _flag(
        pullback_now.shift(1).rolling(3, min_periods=1).max().fillna(False).astype(bool) &
        rebound_soon,
        idx,
    )

    return g


# ============================================================
# main
# ============================================================

def generate_time_window_flags(df: pd.DataFrame) -> pd.DataFrame:
    """
    時間帯フラグを生成する。

    想定入力:
      - datetime
      - close / open / high / low / volume
      - vwap, ma5, ma25 があれば活用
    """
    if df is None or df.empty:
        return df

    df = df.copy()

    dt = _safe_datetime(_col(df, "datetime", "inserted_at", "updated_at", "timestamp"))
    if dt is None:
        return df

    df["datetime"] = dt
    df = df.sort_values(["datetime"]).copy()

    # symbol 列があるなら symbol + 日付単位で見る
    if "symbol" in df.columns:
        work = df.copy()
        work["__date__"] = work["datetime"].dt.date
        out = (
            work.groupby(["symbol", "__date__"], group_keys=False)
            .apply(_apply_time_flags_per_group)
            .drop(columns=["__date__"], errors="ignore")
        )
        return out

    # symbol 無しでも日付単位では処理
    work = df.copy()
    work["__date__"] = work["datetime"].dt.date
    out = (
        work.groupby(["__date__"], group_keys=False)
        .apply(_apply_time_flags_per_group)
        .drop(columns=["__date__"], errors="ignore")
    )
    return out