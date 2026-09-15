# ============================================================
# trading/indicators/flag_builder_extra.py
# ------------------------------------------------------------
# score_config.ini にあるのに一度も算出されていなかったフラグを、
# 既存の列 (close / open / volume / ma / vwap / macd / rsi / rci / bb / 時刻)
# から **ベクトル化して** 生やす。
#
# 2026-09-15 ユーザー指示: 「score_config.ini の項目はすべて算出して
# エントリーの要素に組み込みたい」
#
# 実測 (同日): ini 183 項目のうち算出されていたのは 71 (39%)。
# 残り 112 のうち、既存列だけで計算できる約 60 個をここで生やす。
# 板データ (bid/ask_stack 等)、ランキング履歴 (ranking_top10 等)、
# 指数/セクター、約定回数、AI 出力を要するものは対象外 (列を作らない)。
#
# 入力は symbol ごとの履歴を含むフレーム (1 分足でも 3/5 分足でも可)。
# 出力は入力と同じ index に flag_* 列 (0/1 int) を足したもの。
# 既存の列は上書きしない (同名列があればそのまま)。
#
# 前の足との比較 (クロス系) は groupby(symbol).shift で行うので、
# 銘柄ごとに最低 2 本、rolling 系は 10〜20 本あると全て評価できる。
# 本数が足りない場合そのフラグは 0 (立たない) になる。fail-closed。
# ============================================================
from __future__ import annotations

import logging
from typing import Dict, List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1.0-FLAG-BUILDER-EXTRA-INI-COVERAGE"

# 生やすフラグ名 (ini のキーと同じ)。参照用。
EXTRA_FLAGS: List[str] = [
    # buy_entry
    "flag_slope_positive", "flag_trend_strength", "flag_breakout_high", "flag_range_breakout",
    "flag_volatility_breakout", "flag_macd_cross", "flag_rsi_midline_cross", "flag_vwap_breakout",
    "flag_vwap_reclaim", "flag_opening_range_break", "flag_opening_range_break_volume",
    "flag_buy_over_sell_cross", "flag_fakeout_reclaim", "flag_vwap_fakeout_reclaim",
    "flag_break_low_reclaim", "flag_open_pullback_0910_0930", "flag_pre_lunch_pullback",
    "flag_afternoon_open_reclaim", "flag_reentry_1400_1500", "flag_close_retry_1500_1530",
    "flag_first_pullback_after_open",
    # buy_bonus
    "flag_ma25_above_ma75", "flag_above_ma75", "flag_macd_hist_expand", "flag_rci_rising",
    "flag_rsi_oversold_30", "flag_above_vwap", "flag_vwap_support", "flag_vwap_trend_up",
    "flag_volume_spike", "flag_volume_expansion", "flag_bull_candle_volume", "flag_range_expansion",
    "flag_volatility_expansion", "flag_structure_higher_high", "flag_structure_higher_low",
    "flag_structure_break_up", "flag_structure_range_expansion", "flag_bb_lower_touch",
    "flag_ma_fakeout_reclaim", "flag_support_reclaim", "flag_open_0900_0905", "flag_open_0900_0910",
    "flag_market_open_window", "flag_morning_0930_1030", "flag_pre_lunch_1100_1130",
    "flag_afternoon_open_1230_1300", "flag_afternoon_1300_1400", "flag_market_close_window",
    "flag_morning_high_hold_to_1100",
    # sell_entry
    "flag_slope_negative", "flag_ma_dead_cross", "flag_macd_dead_cross", "flag_vwap_reject",
    "flag_vwap_resistance", "flag_opening_range_fail",
    # sell_bonus
    "flag_macd_hist_contract", "flag_vwap_trend_down", "flag_volume_exhaustion",
    "flag_structure_lower_high", "flag_structure_lower_low", "flag_structure_break_down",
    "flag_structure_range_compression",
]


def _num(df: pd.DataFrame, *names: str) -> pd.Series:
    for n in names:
        if n in df.columns:
            return pd.to_numeric(df[n], errors="coerce")
    return pd.Series(np.nan, index=df.index, dtype="float64")


def _g(df: pd.DataFrame, s: pd.Series):
    return s.groupby(df["symbol"], sort=False)


def _minute(dt_: pd.Series) -> pd.Series:
    return dt_.dt.hour * 60 + dt_.dt.minute


def _window(mod: pd.Series, h1: int, m1: int, h2: int, m2: int) -> pd.Series:
    """[h1:m1, h2:m2) の時間帯。"""
    return (mod >= h1 * 60 + m1) & (mod < h2 * 60 + m2)


def build_extra_flags(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    if "symbol" not in df.columns or "datetime" not in df.columns:
        return df

    out = df.copy()
    out["symbol"] = out["symbol"].astype(str)
    dt_ = pd.to_datetime(out["datetime"], errors="coerce")
    try:
        dt_ = dt_.dt.tz_localize(None)
    except Exception:
        pass
    order = np.lexsort((dt_.values, out["symbol"].values))
    out = out.iloc[order]
    dt_ = dt_.iloc[order]
    sym = out["symbol"]
    day = dt_.dt.date

    c = _num(out, "close", "close_price", "current_price")
    o = _num(out, "open", "open_price")
    v = _num(out, "volume").fillna(0.0)
    ma5, ma25, ma75 = _num(out, "ma5"), _num(out, "ma25"), _num(out, "ma75")
    vwap = _num(out, "vwap")
    macd, sig, hist = _num(out, "macd"), _num(out, "signal"), _num(out, "hist")
    rsi, rci = _num(out, "rsi"), _num(out, "rci")
    bb_up, bb_lo, bb_w = _num(out, "bb_upper"), _num(out, "bb_lower"), _num(out, "bb_width")
    vwap_slope = _num(out, "vwap_slope")
    slope = _num(out, "slope")
    sb, ss = _num(out, "score_buy"), _num(out, "score_sell")
    mod = _minute(dt_)

    G = lambda s: s.groupby(sym, sort=False)  # noqa: E731
    sh = lambda s, n=1: G(s).shift(n)  # noqa: E731

    def roll_max(s, n):
        return G(s).transform(lambda x: x.shift(1).rolling(n, min_periods=n).max())

    def roll_min(s, n):
        return G(s).transform(lambda x: x.shift(1).rolling(n, min_periods=n).min())

    def roll_mean(s, n):
        return G(s).transform(lambda x: x.shift(1).rolling(n, min_periods=max(3, n // 2)).mean())

    c1, c2 = sh(c, 1), sh(c, 2)
    ma5_1, ma25_1 = sh(ma5, 1), sh(ma25, 1)
    macd1, sig1, hist1 = sh(macd, 1), sh(sig, 1), sh(hist, 1)
    rsi1, rci1 = sh(rsi, 1), sh(rci, 1)
    v1, v2 = sh(v, 1), sh(v, 2)
    vwap1 = sh(vwap, 1)
    vmean10 = roll_mean(v, 10)
    cmax10, cmin10 = roll_max(c, 10), roll_min(c, 10)
    cmax20, cmin20 = roll_max(c, 20), roll_min(c, 20)
    body = (c - o).abs()
    body_mean10 = roll_mean(body, 10)
    bbw_mean10 = roll_mean(bb_w, 10)
    bbw1 = sh(bb_w, 1)
    max5 = G(c).transform(lambda x: x.rolling(5, min_periods=5).max())
    min5 = G(c).transform(lambda x: x.rolling(5, min_periods=5).min())
    max5_5, min5_5 = sh(max5, 5), sh(min5, 5)
    rng20 = G(c).transform(lambda x: x.rolling(20, min_periods=20).max() - x.rolling(20, min_periods=20).min())
    rng20_20 = sh(rng20, 20)

    # 寄り付きレンジ (09:00-09:15 の close の高安) を symbol×日 ごとに
    key = pd.Series(list(zip(sym, day)), index=out.index)
    in_or = _window(mod, 9, 0, 9, 15)
    or_hi = c.where(in_or).groupby(key, sort=False).transform("max")
    or_lo = c.where(in_or).groupby(key, sort=False).transform("min")
    after_or = mod >= 9 * 60 + 15
    # 前場の高値 (09:00〜今) の累積最大
    morning_max = c.where(mod < 11 * 60 + 30).groupby(key, sort=False).cummax()

    f: Dict[str, pd.Series] = {}
    # ---------------- buy_entry ----------------
    f["flag_slope_positive"] = slope > 0
    f["flag_trend_strength"] = (ma5 > ma25) & (ma25 > ma75) & (slope > 0)
    f["flag_breakout_high"] = c > cmax20
    f["flag_range_breakout"] = (c > cmax10) & (c > bb_up)
    f["flag_volatility_breakout"] = (bb_w > bbw_mean10 * 1.5) & (c > bb_up)
    f["flag_macd_cross"] = (macd1 <= sig1) & (macd > sig)
    f["flag_rsi_midline_cross"] = (rsi1 <= 50) & (rsi > 50)
    f["flag_vwap_breakout"] = (c1 <= vwap1) & (c > vwap) & (v > vmean10 * 1.5)
    f["flag_vwap_reclaim"] = (c1 < vwap1) & (c2 < sh(vwap, 2)) & (c > vwap)
    f["flag_opening_range_break"] = after_or & (c > or_hi)
    f["flag_opening_range_break_volume"] = f["flag_opening_range_break"] & (v > vmean10 * 1.5)
    f["flag_buy_over_sell_cross"] = (sh(sb, 1) <= sh(ss, 1)) & (sb > ss)
    f["flag_fakeout_reclaim"] = (c2 > sh(ma25, 2)) & (c1 < ma25_1) & (c > ma25)
    f["flag_vwap_fakeout_reclaim"] = (c2 > sh(vwap, 2)) & (c1 < vwap1) & (c > vwap)
    f["flag_break_low_reclaim"] = (c1 < sh(cmin10, 1)) & (c > sh(cmin10, 1))
    pull_bounce = (c1 < ma5_1) & (c > ma5)
    f["flag_open_pullback_0910_0930"] = _window(mod, 9, 10, 9, 30) & pull_bounce
    f["flag_pre_lunch_pullback"] = _window(mod, 11, 0, 11, 30) & pull_bounce
    f["flag_afternoon_open_reclaim"] = _window(mod, 12, 30, 13, 0) & (c1 <= vwap1) & (c > vwap)
    f["flag_reentry_1400_1500"] = _window(mod, 14, 0, 15, 0) & (macd > sig) & (c > ma5) & pull_bounce
    f["flag_close_retry_1500_1530"] = _window(mod, 15, 0, 15, 30) & pull_bounce & (c > vwap)
    first_pb = pull_bounce & _window(mod, 9, 5, 9, 30)
    f["flag_first_pullback_after_open"] = first_pb & (first_pb.groupby(key, sort=False).cumsum() == 1)
    # ---------------- buy_bonus ----------------
    f["flag_ma25_above_ma75"] = ma25 > ma75
    f["flag_above_ma75"] = c > ma75
    f["flag_macd_hist_expand"] = (hist > hist1) & (hist1 > 0)
    f["flag_rci_rising"] = rci > rci1
    f["flag_rsi_oversold_30"] = rsi < 30
    f["flag_above_vwap"] = c > vwap
    f["flag_vwap_support"] = (c1 <= vwap1 * 1.001) & (c1 >= vwap1 * 0.997) & (c > vwap)
    f["flag_vwap_trend_up"] = vwap_slope > 0
    f["flag_volume_spike"] = v > vmean10 * 2.0
    f["flag_volume_expansion"] = (v > v1) & (v1 > v2)
    f["flag_bull_candle_volume"] = (c > o) & (v > vmean10 * 1.5)
    f["flag_range_expansion"] = body > body_mean10 * 1.5
    f["flag_volatility_expansion"] = bb_w > bbw1 * 1.2
    f["flag_structure_higher_high"] = max5 > max5_5
    f["flag_structure_higher_low"] = min5 > min5_5
    f["flag_structure_break_up"] = c > cmax20
    f["flag_structure_range_expansion"] = rng20 > rng20_20 * 1.3
    f["flag_bb_lower_touch"] = c <= bb_lo * 1.001
    f["flag_ma_fakeout_reclaim"] = f["flag_fakeout_reclaim"]
    f["flag_support_reclaim"] = (c1 < ma25_1) & (c > ma25)
    f["flag_open_0900_0905"] = _window(mod, 9, 0, 9, 5)
    f["flag_open_0900_0910"] = _window(mod, 9, 0, 9, 10)
    f["flag_market_open_window"] = _window(mod, 9, 0, 9, 30)
    f["flag_morning_0930_1030"] = _window(mod, 9, 30, 10, 30)
    f["flag_pre_lunch_1100_1130"] = _window(mod, 11, 0, 11, 30)
    f["flag_afternoon_open_1230_1300"] = _window(mod, 12, 30, 13, 0)
    f["flag_afternoon_1300_1400"] = _window(mod, 13, 0, 14, 0)
    f["flag_market_close_window"] = _window(mod, 15, 0, 15, 30)
    f["flag_morning_high_hold_to_1100"] = _window(mod, 10, 55, 11, 6) & (c >= morning_max * 0.997)
    # ---------------- sell_entry ----------------
    f["flag_slope_negative"] = slope < 0
    f["flag_ma_dead_cross"] = (ma5_1 >= ma25_1) & (ma5 < ma25)
    f["flag_macd_dead_cross"] = (macd1 >= sig1) & (macd < sig)
    f["flag_vwap_reject"] = (c1 >= vwap1) & (c < vwap)
    f["flag_vwap_resistance"] = (c1 >= vwap1 * 0.999) & (c1 <= vwap1 * 1.003) & (c < vwap)
    f["flag_opening_range_fail"] = after_or & (c < or_lo)
    # ---------------- sell_bonus ----------------
    f["flag_macd_hist_contract"] = (hist < hist1) & (hist1 < 0)
    f["flag_vwap_trend_down"] = vwap_slope < 0
    f["flag_volume_exhaustion"] = (v < vmean10 * 0.5) & (c < ma5)
    f["flag_structure_lower_high"] = max5 < max5_5
    f["flag_structure_lower_low"] = min5 < min5_5
    f["flag_structure_break_down"] = c < cmin20
    f["flag_structure_range_compression"] = rng20 < rng20_20 * 0.7

    added = 0
    for name, ser in f.items():
        # ここで生やす列は毎回計算し直す (前回の tick の値が summary 履歴に残って
        # 次の入力に含まれるため、「既存列はそのまま」だと古いフラグが残り続ける)。
        out[name] = ser.fillna(False).astype(bool).astype(int)
        added += 1
    out = out.loc[df.index]  # 元の順序へ戻す
    try:
        nonzero = int(sum(1 for n in f if n in out.columns and int(out[n].sum()) > 0))
        logger.info("[EXTRA FLAGS] built rows=%s symbols=%s added=%s nonzero=%s version=%s",
                    len(out), out["symbol"].nunique(), added, nonzero, VERSION)
    except Exception:
        pass
    return out
