# ============================================================
# File   : trading/summary/entry_signals.py
# Version: PRODUCTION-STABLE-REV1.0-ALL-ENTRY-SIGNALS
# ------------------------------------------------------------
# 【概要】
#   summary DataFrame に対して複数のエントリーサインを一括付与する
#
# 【主な機能】
#   - setup別スコア算出
#       pullback / breakout / reversal / trend_continuation
#       vwap_reclaim / range_break / retest_success
#       opening_range_break / multi_tf_resonance / relative_strength
#       phase_shift / ranking_persistence / fakeout_reversal
#       gap_go / volatility_squeeze
#
#   - 最大スコアの setup を entry_setup_type として付与
#   - entry_score_v4 を算出
#   - AI gate / top_candidates 用の説明列も付与
#
# 【前提】
#   既存の summary 系 DataFrame
#   symbol, datetime, open, high, low, close, volume
#
# 【あれば使う列】
#   ma5, ma25, ma75
#   rsi, macd, signal
#   score_buy, score_sell, final_score
#   score_mtf, mtf, score_slope, slope_atr_scaled, slope
#   vwap
#   recent_breakout_level
#   ranking_position, ranking_score, ranking_in_count
#   market_return, sector_return
#   tf3_score, tf5_score など
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd


# ------------------------------------------------------------
# utility
# ------------------------------------------------------------
def _ensure_col(df: pd.DataFrame, col: str, default=0.0) -> None:
    if col not in df.columns:
        df[col] = default


def _safe_div(a, b, default=0.0):
    if isinstance(b, pd.Series):
        b = b.replace(0, np.nan)
    else:
        b = np.nan if b == 0 else b
    out = a / b
    if isinstance(out, pd.Series):
        return out.replace([np.inf, -np.inf], np.nan).fillna(default)
    return default if pd.isna(out) or np.isinf(out) else out


def _clip(s: pd.Series, low=0.0, high=100.0) -> pd.Series:
    return s.clip(lower=low, upper=high)


def _first_true_name(row: pd.Series, mapping: list[tuple[str, str]]) -> str:
    for col, name in mapping:
        if bool(row.get(col, False)):
            return name
    return ""


def _build_setup_reason(row: pd.Series) -> str:
    parts = []

    setup = row.get("entry_setup_type", "")
    if setup:
        parts.append(f"setup={setup}")

    checks = [
        ("trend_ok", "上昇トレンド"),
        ("touch_ma25", "25MA押し"),
        ("touch_ma5", "5MA押し"),
        ("vwap_reclaim", "VWAP回復"),
        ("rsi_rebound", "RSI反転"),
        ("macd_rebound", "MACD再上向き"),
        ("higher_low", "安値切り上げ"),
        ("break_prev_high", "前足高値突破"),
        ("volume_reexpand", "出来高再拡大"),
        ("vol_recovery", "反発出来高"),
        ("range_break_up", "レンジ上抜け"),
        ("break_10bar_high", "10本高値更新"),
        ("break_5bar_high", "5本高値更新"),
        ("opening_range_break", "OR上抜け"),
        ("relative_strength_positive", "相対強度プラス"),
        ("retest_success", "レジサポ転換成功"),
        ("fakeout_reclaim", "フェイク否定"),
        ("squeeze_break", "圧縮後拡大"),
    ]
    for col, label in checks:
        if bool(row.get(col, False)):
            parts.append(label)

    return " / ".join(parts)


# ------------------------------------------------------------
# main
# ------------------------------------------------------------
def add_entry_signals(
    df: pd.DataFrame,
    *,
    min_setup_score: float = 55.0,
) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df

    out = df.copy()

    # --------------------------------------------------------
    # required base columns
    # --------------------------------------------------------
    defaults = {
        "symbol": "",
        "datetime": pd.NaT,
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "close": 0.0,
        "volume": 0.0,
        "ma5": 0.0,
        "ma25": 0.0,
        "ma75": 0.0,
        "rsi": 0.0,
        "macd": 0.0,
        "signal": 0.0,
        "score_buy": 0.0,
        "score_sell": 0.0,
        "final_score": 0.0,
        "score_mtf": 0.0,
        "mtf": 0.0,
        "score_slope": 0.0,
        "slope_atr_scaled": 0.0,
        "slope": 0.0,
        "vwap": np.nan,
        "recent_breakout_level": np.nan,
        "ranking_position": np.nan,
        "ranking_score": 0.0,
        "ranking_in_count": 0.0,
        "market_return": 0.0,
        "sector_return": 0.0,
        "tf3_score": np.nan,
        "tf5_score": np.nan,
    }
    for c, d in defaults.items():
        _ensure_col(out, c, d)

    if not pd.api.types.is_datetime64_any_dtype(out["datetime"]):
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")

    out = out.sort_values(["symbol", "datetime"]).reset_index(drop=True)

    # --------------------------------------------------------
    # per symbol
    # --------------------------------------------------------
    def _per_symbol(g: pd.DataFrame) -> pd.DataFrame:
        g = g.copy()
        eps = 1e-9

        # ---- sanitize OHLC
        g["high"] = g[["open", "high", "low", "close"]].max(axis=1)
        g["low"] = g[["open", "high", "low", "close"]].min(axis=1)

        # ---- candle features
        g["candle_range"] = (g["high"] - g["low"]).clip(lower=eps)
        g["body_size"] = (g["close"] - g["open"]).abs()
        g["body_ratio"] = _safe_div(g["body_size"], g["candle_range"], 0.0)

        g["lower_hige"] = np.minimum(g["open"], g["close"]) - g["low"]
        g["upper_hige"] = g["high"] - np.maximum(g["open"], g["close"])
        g["lower_hige_ratio"] = _safe_div(g["lower_hige"], g["candle_range"], 0.0)
        g["upper_hige_ratio"] = _safe_div(g["upper_hige"], g["candle_range"], 0.0)
        g["close_position_in_range"] = _safe_div(g["close"] - g["low"], g["candle_range"], 0.0)

        # ---- rolling refs
        g["high_5"] = g["high"].rolling(5, min_periods=1).max().shift(1)
        g["high_10"] = g["high"].rolling(10, min_periods=1).max().shift(1)
        g["low_5"] = g["low"].rolling(5, min_periods=1).min().shift(1)
        g["low_10"] = g["low"].rolling(10, min_periods=1).min().shift(1)
        g["recent_high_5"] = g["high"].rolling(5, min_periods=1).max()
        g["recent_high_10"] = g["high"].rolling(10, min_periods=1).max()
        g["range_high_10"] = g["high"].rolling(10, min_periods=3).max().shift(1)
        g["range_low_10"] = g["low"].rolling(10, min_periods=3).min().shift(1)
        g["prev_high"] = g["high"].shift(1)
        g["prev_low"] = g["low"].shift(1)
        g["prev_close"] = g["close"].shift(1)
        g["prev_open"] = g["open"].shift(1)

        # ---- volume refs
        g["volume_ma3"] = g["volume"].rolling(3, min_periods=1).mean()
        g["volume_ma5"] = g["volume"].rolling(5, min_periods=1).mean()
        g["volume_ma10"] = g["volume"].rolling(10, min_periods=1).mean()

        # ---- momentum refs
        g["rsi_prev"] = g["rsi"].shift(1)
        g["macd_prev"] = g["macd"].shift(1)
        g["score_buy_prev"] = g["score_buy"].shift(1)
        g["score_sell_prev"] = g["score_sell"].shift(1)

        # ---- baseline trend
        g["trend_ok"] = (
            (g["ma5"] > g["ma25"]) &
            (g["ma25"] > g["ma75"]) &
            (g["score_buy"] > g["score_sell"]) &
            (g["final_score"] > 0) &
            ((g["score_mtf"] > 0) | (g["mtf"] > 0))
        )
        g["trend_ok_strict"] = (
            g["trend_ok"] &
            (g["close"] >= g["ma25"]) &
            ((g["score_slope"] > 0) | (g["slope_atr_scaled"] > 0) | (g["slope"] > 0))
        )
        g["price_above_all_ma"] = (
            (g["close"] > g["ma5"]) &
            (g["close"] > g["ma25"]) &
            (g["close"] > g["ma75"])
        )
        g["mtf_keep"] = (g["score_mtf"] > 0) | (g["mtf"] > 0)
        g["slope_keep"] = (g["score_slope"] > 0) | (g["slope_atr_scaled"] > 0) | (g["slope"] > 0)

        # ---- common price action
        g["touch_ma5"] = (g["low"] <= g["ma5"]) & (g["close"] >= g["ma5"])
        g["touch_ma25"] = (g["low"] <= g["ma25"]) & (g["close"] >= g["ma25"])
        g["touch_ma75"] = (g["low"] <= g["ma75"]) & (g["close"] >= g["ma75"])

        g["rsi_rebound"] = (g["rsi"] > g["rsi_prev"]) & (g["rsi"] >= 50)
        g["macd_rebound"] = (g["macd"] > g["signal"]) & (g["macd"] >= g["macd_prev"])
        g["higher_low"] = (
            (g["low"] > g["low"].shift(1)) |
            ((g["low"].shift(1) > g["low"].shift(2)) & (g["close"] >= g["close"].shift(1)))
        )
        g["break_prev_high"] = g["close"] > g["prev_high"]
        g["high_break_prev_high"] = g["high"] > g["high"].shift(1)
        g["close_gt_prev_close"] = g["close"] > g["close"].shift(1)
        g["close_near_high"] = g["close_position_in_range"] >= 0.80
        g["body_expansion"] = g["body_ratio"] >= 0.50
        g["range_expansion"] = g["candle_range"] > (g["high"].shift(1) - g["low"].shift(1)).clip(lower=eps)

        g["volume_reexpand"] = g["volume"] > g["volume_ma5"]
        g["vol_dryup"] = g["volume"] < g["volume_ma5"]
        g["vol_recovery"] = (g["volume"] > g["volume_ma5"]) & (g["close"] > g["open"])
        g["volume_spike"] = g["volume"] > (g["volume_ma10"] * 1.8)

        g["score_buy_diff"] = g["score_buy"] - g["score_buy_prev"]
        g["score_sell_diff"] = g["score_sell"] - g["score_sell_prev"]
        g["sell_exhaustion"] = g["score_sell_diff"] < 0
        g["buy_recover"] = g["score_buy_diff"] > 0

        # ---- pullback common
        g["pullback_depth_pct"] = _safe_div(
            (g["recent_high_5"] - g["close"]).clip(lower=0),
            g["recent_high_5"],
            0.0,
        )
        g["pullback_shallow_good"] = (
            (g["pullback_depth_pct"] >= 0.003) &
            (g["pullback_depth_pct"] <= 0.018)
        )
        g["pullback_too_deep"] = g["pullback_depth_pct"] > 0.035
        g["red_count_3"] = (g["close"] < g["open"]).rolling(3, min_periods=1).sum()
        g["tight_pullback"] = (
            (g["red_count_3"] <= 2) &
            (g["pullback_depth_pct"] <= 0.020)
        )

        # ---- vwap
        has_vwap = g["vwap"].notna()
        g["price_above_vwap"] = has_vwap & (g["close"] >= g["vwap"])
        g["vwap_reclaim"] = has_vwap & (g["low"] <= g["vwap"]) & (g["close"] >= g["vwap"])
        g["vwap_cross_up"] = has_vwap & (g["close"].shift(1) < g["vwap"].shift(1)) & (g["close"] >= g["vwap"])

        # ---- breakout levels / retest
        has_breakout = g["recent_breakout_level"].notna()
        g["dist_breakout_pct"] = np.where(
            has_breakout,
            _safe_div((g["close"] - g["recent_breakout_level"]).abs(), g["close"], 999.0),
            999.0,
        )
        g["pullback_to_breakout_level"] = has_breakout & (g["dist_breakout_pct"] <= 0.0045)
        g["retest_success"] = (
            has_breakout &
            (g["low"] <= g["recent_breakout_level"] * 1.003) &
            (g["close"] >= g["recent_breakout_level"])
        )

        # ---- breakout
        g["break_5bar_high"] = g["close"] > g["high_5"]
        g["break_10bar_high"] = g["close"] > g["high_10"]
        g["momentum_accel"] = (
            (g["close"] > g["close"].shift(1)) &
            (g["macd"] >= g["macd"].shift(1)) &
            (g["score_buy"] >= g["score_buy"].shift(1))
        )

        # ---- reversal
        g["bullish_engulfing"] = (
            (g["close"] > g["open"]) &
            (g["close"].shift(1) < g["open"].shift(1)) &
            (g["open"] <= g["close"].shift(1)) &
            (g["close"] >= g["open"].shift(1))
        )
        g["double_bottom_like"] = (
            (g["low"].shift(2) > 0) &
            (_safe_div((g["low"] - g["low"].shift(2)).abs(), g["close"], 999.0) <= 0.005) &
            (g["close"] > g["close"].shift(1))
        )
        g["rsi_divergence_like"] = (
            (g["low"] <= g["low"].shift(1)) &
            (g["rsi"] > g["rsi"].shift(1))
        )

        # ---- range break
        g["range_width_pct"] = _safe_div(
            (g["range_high_10"] - g["range_low_10"]),
            g["close"],
            0.0,
        )
        g["tight_range"] = g["range_width_pct"] <= 0.03
        g["range_break_up"] = g["close"] > g["range_high_10"]

        # ---- gap go
        g["gap_up_pct"] = _safe_div((g["open"] - g["prev_close"]), g["prev_close"], 0.0)
        g["gap_up"] = g["gap_up_pct"] >= 0.01
        g["gap_hold"] = g["low"] >= g["prev_close"]

        # ---- opening range
        # simple proxy: first 5 bars in each symbol/day. if time granularity differs, user can adapt
        g["date"] = g["datetime"].dt.date
        g["bar_index_in_day"] = g.groupby("date").cumcount()
        g["or5_high"] = g.groupby("date")["high"].transform(
            lambda s: s.expanding().max()
        )
        g["or5_low"] = g.groupby("date")["low"].transform(
            lambda s: s.expanding().min()
        )
        # use the first 5 rows of each day as OR proxy
        g["opening_range_high"] = g.groupby("date")["high"].transform(
            lambda s: s.iloc[:5].max() if len(s) else np.nan
        )
        g["opening_range_low"] = g.groupby("date")["low"].transform(
            lambda s: s.iloc[:5].min() if len(s) else np.nan
        )
        g["opening_range_break"] = (
            (g["bar_index_in_day"] >= 5) &
            (g["close"] > g["opening_range_high"])
        )

        # ---- squeeze
        g["range_ma5"] = g["candle_range"].rolling(5, min_periods=1).mean()
        g["range_ma10"] = g["candle_range"].rolling(10, min_periods=1).mean()
        g["body_ma5"] = g["body_size"].rolling(5, min_periods=1).mean()
        g["squeeze_condition"] = (
            (g["range_ma5"] < g["range_ma10"]) &
            (g["volume_ma5"] < g["volume_ma10"]) &
            (g["body_ma5"] < g["body_size"].rolling(10, min_periods=1).mean())
        )
        g["squeeze_break"] = g["squeeze_condition"].shift(1).fillna(False) & g["break_prev_high"]

        # ---- fakeout reclaim
        g["fakeout_reclaim"] = (
            ((g["close"].shift(1) < g["ma5"].shift(1)) & (g["close"] >= g["ma5"])) |
            ((has_vwap & (g["close"].shift(1) < g["vwap"].shift(1)) & (g["close"] >= g["vwap"]))) |
            ((g["close"].shift(1) < g["prev_low"].shift(1)) & (g["close"] > g["prev_low"]))
        )

        # ---- relative strength
        g["relative_strength_raw"] = g["final_score"] - g["market_return"]
        g["relative_strength_positive"] = (
            (g["final_score"] > g["market_return"]) |
            (g["final_score"] > g["sector_return"])
        )

        # ---- ranking persistence
        g["ranking_good"] = g["ranking_position"].notna() & (g["ranking_position"] <= 20)
        g["ranking_improving"] = g["ranking_position"] < g["ranking_position"].shift(1)
        g["ranking_persistent"] = (
            g["ranking_good"].rolling(3, min_periods=1).sum() >= 2
        )

        # ---- phase shift
        g["phase_shift"] = (
            (g["score_buy"] > g["score_sell"]) &
            (g["score_buy"].shift(1) <= g["score_sell"].shift(1)) &
            (g["rsi_rebound"] | g["macd_rebound"] | g["vwap_reclaim"])
        )

        # ---- multi tf resonance
        g["tf3_ok"] = g["tf3_score"].fillna(g["score_mtf"]) > 0
        g["tf5_ok"] = g["tf5_score"].fillna(g["score_mtf"]) > 0
        g["multi_tf_resonance"] = (
            (g["rsi_rebound"] | g["macd_rebound"] | g["break_prev_high"]) &
            g["tf3_ok"] &
            g["tf5_ok"] &
            g["mtf_keep"]
        )

        # ---- danger penalties
        g["danger_break_ma75"] = g["close"] < g["ma75"]
        g["danger_rsi_weak"] = g["rsi"] < 45
        g["danger_macd_weak"] = g["macd"] < g["signal"]
        g["danger_heavy_sell"] = g["score_sell"] > g["score_buy"]
        g["danger_failed_rebound"] = (
            (g["close"] < g["open"]) &
            (g["close_position_in_range"] < 0.4)
        )
        g["danger_deep_pullback"] = g["pullback_depth_pct"] > 0.035
        g["danger_negative_mtf"] = (g["score_mtf"] <= 0) & (g["mtf"] <= 0)

        g["danger_penalty_score"] = 0.0
        g["danger_penalty_score"] += np.where(g["danger_break_ma75"], -20, 0)
        g["danger_penalty_score"] += np.where(g["danger_rsi_weak"], -10, 0)
        g["danger_penalty_score"] += np.where(g["danger_macd_weak"], -10, 0)
        g["danger_penalty_score"] += np.where(g["danger_heavy_sell"], -12, 0)
        g["danger_penalty_score"] += np.where(g["danger_failed_rebound"], -15, 0)
        g["danger_penalty_score"] += np.where(g["danger_deep_pullback"], -20, 0)
        g["danger_penalty_score"] += np.where(g["danger_negative_mtf"], -20, 0)

        # ----------------------------------------------------
        # setup scores
        # ----------------------------------------------------
        # 1) pullback
        g["pullback_score_v2"] = 0.0
        g["pullback_score_v2"] += np.where(g["trend_ok"], 18, -18)
        g["pullback_score_v2"] += np.where(g["touch_ma25"], 15, 0)
        g["pullback_score_v2"] += np.where(g["touch_ma5"], 10, 0)
        g["pullback_score_v2"] += np.where(g["pullback_shallow_good"], 14, 0)
        g["pullback_score_v2"] += np.where(g["tight_pullback"], 8, -4)
        g["pullback_score_v2"] += np.where(g["rsi_rebound"], 8, 0)
        g["pullback_score_v2"] += np.where(g["macd_rebound"], 10, 0)
        g["pullback_score_v2"] += np.where(g["higher_low"], 8, -4)
        g["pullback_score_v2"] += np.where(g["break_prev_high"], 12, 0)
        g["pullback_score_v2"] += np.where(g["vol_recovery"], 8, 0)
        g["pullback_score_v2"] += np.where(g["vwap_reclaim"], 6, 0)
        g["pullback_score_v2"] += g["danger_penalty_score"] * 0.5
        g["pullback_score_v2"] = _clip(g["pullback_score_v2"])

        # 2) breakout
        g["breakout_score"] = 0.0
        g["breakout_score"] += np.where(g["break_5bar_high"], 18, 0)
        g["breakout_score"] += np.where(g["break_10bar_high"], 22, 0)
        g["breakout_score"] += np.where(g["close_near_high"], 10, -4)
        g["breakout_score"] += np.where(g["body_expansion"], 8, 0)
        g["breakout_score"] += np.where(g["volume_reexpand"], 8, 0)
        g["breakout_score"] += np.where(g["momentum_accel"], 10, -5)
        g["breakout_score"] += np.where(g["trend_ok"], 12, -12)
        g["breakout_score"] += np.where(g["score_mtf"] > 0, 10, -10)
        g["breakout_score"] += g["danger_penalty_score"] * 0.5
        g["breakout_score"] = _clip(g["breakout_score"])

        # 3) reversal
        g["reversal_score"] = 0.0
        g["reversal_score"] += np.where(g["bullish_engulfing"], 18, 0)
        g["reversal_score"] += np.where(g["double_bottom_like"], 14, 0)
        g["reversal_score"] += np.where(g["rsi_divergence_like"], 10, 0)
        g["reversal_score"] += np.where(g["lower_hige_ratio"] >= 0.4, 10, 0)
        g["reversal_score"] += np.where(g["rsi_rebound"], 10, 0)
        g["reversal_score"] += np.where(g["macd_rebound"], 12, 0)
        g["reversal_score"] += np.where(g["break_prev_high"], 12, 0)
        g["reversal_score"] += np.where(g["vol_recovery"], 8, 0)
        g["reversal_score"] += g["danger_penalty_score"] * 0.6
        g["reversal_score"] = _clip(g["reversal_score"])

        # 4) trend continuation
        g["trend_continuation_score"] = 0.0
        g["trend_continuation_score"] += np.where(g["price_above_all_ma"], 18, -10)
        g["trend_continuation_score"] += np.where(g["trend_ok"], 18, -18)
        g["trend_continuation_score"] += np.where(g["close_near_high"], 8, -4)
        g["trend_continuation_score"] += np.where(g["score_buy"] > g["score_sell"], 8, -8)
        g["trend_continuation_score"] += np.where(g["score_mtf"] > 0, 10, -10)
        g["trend_continuation_score"] += np.where(g["score_slope"] > 0, 8, -8)
        g["trend_continuation_score"] += np.where(g["volume_reexpand"], 8, 0)
        g["trend_continuation_score"] += np.where(g["pullback_depth_pct"] < 0.01, 6, 0)
        g["trend_continuation_score"] = _clip(g["trend_continuation_score"])

        # 5) vwap reclaim
        g["vwap_reclaim_score"] = 0.0
        g["vwap_reclaim_score"] += np.where(g["vwap_cross_up"], 20, 0)
        g["vwap_reclaim_score"] += np.where(g["vwap_reclaim"], 14, 0)
        g["vwap_reclaim_score"] += np.where(g["price_above_vwap"], 8, -8)
        g["vwap_reclaim_score"] += np.where(g["rsi_rebound"], 8, 0)
        g["vwap_reclaim_score"] += np.where(g["macd_rebound"], 8, 0)
        g["vwap_reclaim_score"] += np.where(g["vol_recovery"], 10, 0)
        g["vwap_reclaim_score"] += np.where(g["trend_ok"], 10, -10)
        g["vwap_reclaim_score"] = _clip(g["vwap_reclaim_score"])

        # 6) range break
        g["range_break_score"] = 0.0
        g["range_break_score"] += np.where(g["range_break_up"], 20, 0)
        g["range_break_score"] += np.where(g["tight_range"], 12, 0)
        g["range_break_score"] += np.where(g["close_near_high"], 10, -4)
        g["range_break_score"] += np.where(g["volume_reexpand"], 10, 0)
        g["range_break_score"] += np.where(g["trend_ok"], 10, -10)
        g["range_break_score"] += np.where(g["score_mtf"] > 0, 8, -8)
        g["range_break_score"] = _clip(g["range_break_score"])

        # 7) retest success
        g["retest_success_score"] = 0.0
        g["retest_success_score"] += np.where(g["retest_success"], 22, 0)
        g["retest_success_score"] += np.where(g["pullback_to_breakout_level"], 10, 0)
        g["retest_success_score"] += np.where(g["lower_hige_ratio"] >= 0.30, 8, 0)
        g["retest_success_score"] += np.where(g["break_prev_high"], 12, 0)
        g["retest_success_score"] += np.where(g["vol_recovery"], 8, 0)
        g["retest_success_score"] += np.where(g["trend_ok"], 10, -10)
        g["retest_success_score"] += g["danger_penalty_score"] * 0.4
        g["retest_success_score"] = _clip(g["retest_success_score"])

        # 8) opening range break
        g["opening_range_break_score"] = 0.0
        g["opening_range_break_score"] += np.where(g["opening_range_break"], 22, 0)
        g["opening_range_break_score"] += np.where(g["volume_reexpand"], 10, 0)
        g["opening_range_break_score"] += np.where(g["close_near_high"], 10, -4)
        g["opening_range_break_score"] += np.where(g["trend_ok"], 10, -10)
        g["opening_range_break_score"] += np.where(g["price_above_vwap"], 8, -4)
        g["opening_range_break_score"] = _clip(g["opening_range_break_score"])

        # 9) multi tf resonance
        g["multi_tf_resonance_score"] = 0.0
        g["multi_tf_resonance_score"] += np.where(g["multi_tf_resonance"], 24, 0)
        g["multi_tf_resonance_score"] += np.where(g["break_prev_high"], 10, 0)
        g["multi_tf_resonance_score"] += np.where(g["rsi_rebound"], 8, 0)
        g["multi_tf_resonance_score"] += np.where(g["macd_rebound"], 8, 0)
        g["multi_tf_resonance_score"] += np.where(g["vol_recovery"], 8, 0)
        g["multi_tf_resonance_score"] += np.where(g["trend_ok"], 10, -10)
        g["multi_tf_resonance_score"] = _clip(g["multi_tf_resonance_score"])

        # 10) relative strength
        g["relative_strength_score"] = 0.0
        g["relative_strength_score"] += np.where(g["relative_strength_positive"], 18, -8)
        g["relative_strength_score"] += np.where(g["close_near_high"], 8, -4)
        g["relative_strength_score"] += np.where(g["volume_reexpand"], 8, 0)
        g["relative_strength_score"] += np.where(g["trend_ok"], 10, -10)
        g["relative_strength_score"] += np.where(g["score_buy"] > g["score_sell"], 8, -8)
        g["relative_strength_score"] = _clip(g["relative_strength_score"])

        # 11) phase shift
        g["phase_shift_score"] = 0.0
        g["phase_shift_score"] += np.where(g["phase_shift"], 22, 0)
        g["phase_shift_score"] += np.where(g["vwap_reclaim"], 8, 0)
        g["phase_shift_score"] += np.where(g["rsi_rebound"], 8, 0)
        g["phase_shift_score"] += np.where(g["macd_rebound"], 8, 0)
        g["phase_shift_score"] += np.where(g["break_prev_high"], 10, 0)
        g["phase_shift_score"] += np.where(g["vol_recovery"], 8, 0)
        g["phase_shift_score"] = _clip(g["phase_shift_score"])

        # 12) ranking persistence
        g["ranking_persistence_score"] = 0.0
        g["ranking_persistence_score"] += np.where(g["ranking_good"], 12, 0)
        g["ranking_persistence_score"] += np.where(g["ranking_persistent"], 14, 0)
        g["ranking_persistence_score"] += np.where(g["ranking_improving"], 10, 0)
        g["ranking_persistence_score"] += np.where(g["trend_ok"], 8, -8)
        g["ranking_persistence_score"] += np.where(g["volume_reexpand"], 8, 0)
        g["ranking_persistence_score"] += np.where(g["close_near_high"], 6, -2)
        g["ranking_persistence_score"] = _clip(g["ranking_persistence_score"])

        # 13) fakeout reversal
        g["fakeout_reversal_score"] = 0.0
        g["fakeout_reversal_score"] += np.where(g["fakeout_reclaim"], 22, 0)
        g["fakeout_reversal_score"] += np.where(g["vwap_reclaim"], 8, 0)
        g["fakeout_reversal_score"] += np.where(g["break_prev_high"], 10, 0)
        g["fakeout_reversal_score"] += np.where(g["rsi_rebound"], 8, 0)
        g["fakeout_reversal_score"] += np.where(g["macd_rebound"], 8, 0)
        g["fakeout_reversal_score"] += np.where(g["vol_recovery"], 8, 0)
        g["fakeout_reversal_score"] += g["danger_penalty_score"] * 0.4
        g["fakeout_reversal_score"] = _clip(g["fakeout_reversal_score"])

        # 14) gap go
        g["gap_go_score"] = 0.0
        g["gap_go_score"] += np.where(g["gap_up"], 16, 0)
        g["gap_go_score"] += np.where(g["gap_hold"], 14, -6)
        g["gap_go_score"] += np.where(g["close_near_high"], 10, -4)
        g["gap_go_score"] += np.where(g["volume_reexpand"], 10, 0)
        g["gap_go_score"] += np.where(g["trend_ok"], 10, -10)
        g["gap_go_score"] += np.where(g["score_buy"] > g["score_sell"], 8, -8)
        g["gap_go_score"] = _clip(g["gap_go_score"])

        # 15) volatility squeeze
        g["volatility_squeeze_score"] = 0.0
        g["volatility_squeeze_score"] += np.where(g["squeeze_condition"], 10, 0)
        g["volatility_squeeze_score"] += np.where(g["squeeze_break"], 20, 0)
        g["volatility_squeeze_score"] += np.where(g["volume_reexpand"], 8, 0)
        g["volatility_squeeze_score"] += np.where(g["close_near_high"], 8, -4)
        g["volatility_squeeze_score"] += np.where(g["trend_ok"], 10, -10)
        g["volatility_squeeze_score"] = _clip(g["volatility_squeeze_score"])

        # ----------------------------------------------------
        # bool setup entries
        # ----------------------------------------------------
        g["is_pullback_entry"] = (
            g["trend_ok"] &
            (g["touch_ma5"] | g["touch_ma25"] | g["vwap_reclaim"]) &
            (g["pullback_score_v2"] >= 55)
        )
        g["is_breakout_entry"] = (
            g["trend_ok"] &
            (g["break_5bar_high"] | g["break_10bar_high"]) &
            g["close_near_high"] &
            (g["breakout_score"] >= 60)
        )
        g["is_reversal_entry"] = (
            (g["rsi_rebound"] | g["macd_rebound"] | g["bullish_engulfing"]) &
            (g["reversal_score"] >= 55)
        )
        g["is_trend_continuation_entry"] = (
            g["trend_ok"] &
            g["price_above_all_ma"] &
            g["close_near_high"] &
            (g["trend_continuation_score"] >= 58)
        )
        g["is_vwap_reclaim_entry"] = (
            has_vwap &
            (g["vwap_cross_up"] | g["vwap_reclaim"]) &
            (g["vwap_reclaim_score"] >= 55)
        )
        g["is_range_break_entry"] = (
            g["range_break_up"] &
            g["tight_range"] &
            (g["range_break_score"] >= 58)
        )
        g["is_retest_success_entry"] = (
            g["retest_success"] &
            (g["retest_success_score"] >= 55)
        )
        g["is_opening_range_break_entry"] = (
            g["opening_range_break"] &
            (g["opening_range_break_score"] >= 55)
        )
        g["is_multi_tf_resonance_entry"] = (
            g["multi_tf_resonance"] &
            (g["multi_tf_resonance_score"] >= 55)
        )
        g["is_relative_strength_entry"] = (
            g["relative_strength_positive"] &
            (g["relative_strength_score"] >= 55)
        )
        g["is_phase_shift_entry"] = (
            g["phase_shift"] &
            (g["phase_shift_score"] >= 55)
        )
        g["is_ranking_persistence_entry"] = (
            g["ranking_persistent"] &
            (g["ranking_persistence_score"] >= 55)
        )
        g["is_fakeout_reversal_entry"] = (
            g["fakeout_reclaim"] &
            (g["fakeout_reversal_score"] >= 55)
        )
        g["is_gap_go_entry"] = (
            g["gap_up"] &
            g["gap_hold"] &
            (g["gap_go_score"] >= 55)
        )
        g["is_volatility_squeeze_entry"] = (
            g["squeeze_break"] &
            (g["volatility_squeeze_score"] >= 55)
        )

        # ----------------------------------------------------
        # setup aggregation
        # ----------------------------------------------------
        score_cols = {
            "pullback": "pullback_score_v2",
            "breakout": "breakout_score",
            "reversal": "reversal_score",
            "trend_continuation": "trend_continuation_score",
            "vwap_reclaim": "vwap_reclaim_score",
            "range_break": "range_break_score",
            "retest_success": "retest_success_score",
            "opening_range_break": "opening_range_break_score",
            "multi_tf_resonance": "multi_tf_resonance_score",
            "relative_strength": "relative_strength_score",
            "phase_shift": "phase_shift_score",
            "ranking_persistence": "ranking_persistence_score",
            "fakeout_reversal": "fakeout_reversal_score",
            "gap_go": "gap_go_score",
            "volatility_squeeze": "volatility_squeeze_score",
        }

        g["setup_score"] = g[list(score_cols.values())].max(axis=1)

        # row-wise max label
        setup_names = list(score_cols.keys())
        setup_score_names = list(score_cols.values())
        g["entry_setup_type"] = ""
        max_idx = g[setup_score_names].values.argmax(axis=1)
        g["entry_setup_type"] = [setup_names[i] for i in max_idx]

        # gate by bool flags + minimum score
        bool_col_map = {
            "pullback": "is_pullback_entry",
            "breakout": "is_breakout_entry",
            "reversal": "is_reversal_entry",
            "trend_continuation": "is_trend_continuation_entry",
            "vwap_reclaim": "is_vwap_reclaim_entry",
            "range_break": "is_range_break_entry",
            "retest_success": "is_retest_success_entry",
            "opening_range_break": "is_opening_range_break_entry",
            "multi_tf_resonance": "is_multi_tf_resonance_entry",
            "relative_strength": "is_relative_strength_entry",
            "phase_shift": "is_phase_shift_entry",
            "ranking_persistence": "is_ranking_persistence_entry",
            "fakeout_reversal": "is_fakeout_reversal_entry",
            "gap_go": "is_gap_go_entry",
            "volatility_squeeze": "is_volatility_squeeze_entry",
        }
        g["selected_setup_valid"] = [
            bool(g.iloc[i][bool_col_map[g.iloc[i]["entry_setup_type"]]]) if g.iloc[i]["entry_setup_type"] in bool_col_map else False
            for i in range(len(g))
        ]
        g["is_setup_entry"] = g["selected_setup_valid"] & (g["setup_score"] >= min_setup_score)

        # if invalid, clear setup_type
        g.loc[~g["is_setup_entry"], "entry_setup_type"] = ""

        # ----------------------------------------------------
        # subtype
        # ----------------------------------------------------
        g["pullback_subtype"] = np.select(
            [
                g["touch_ma25"] & g["break_prev_high"],
                g["touch_ma5"] & g["rsi_rebound"],
                g["vwap_reclaim"] & g["vol_recovery"],
                g["retest_success"],
            ],
            [
                "ma25_rebound",
                "ma5_rebound",
                "vwap_reclaim",
                "support_retest",
            ],
            default="generic",
        )

        # ----------------------------------------------------
        # timing / final score
        # ----------------------------------------------------
        g["entry_timing_score"] = 0.0
        g["entry_timing_score"] += np.where(g["break_prev_high"], 15, 0)
        g["entry_timing_score"] += np.where(g["rsi_rebound"], 8, 0)
        g["entry_timing_score"] += np.where(g["macd_rebound"], 8, 0)
        g["entry_timing_score"] += np.where(g["vol_recovery"], 10, 0)
        g["entry_timing_score"] += np.where(g["close_position_in_range"] >= 0.70, 6, -4)

        g["entry_score_v4"] = (
            g["final_score"] * 0.22
            + g["score_buy"] * 0.12
            + g["setup_score"] * 0.30
            + np.maximum(g["score_mtf"], 0) * 0.10
            + np.maximum(g["score_slope"], 0) * 0.06
            + g["entry_timing_score"] * 0.10
            + np.where(g["volume_reexpand"], 4, 0)
            + np.where(g["close_near_high"], 3, 0)
            + np.where(g["relative_strength_positive"], 3, 0)
        )

        # ----------------------------------------------------
        # display / explanation
        # ----------------------------------------------------
        g["setup_reason"] = g.apply(_build_setup_reason, axis=1)

        return g

    out = out.groupby("symbol", group_keys=False).apply(_per_symbol).reset_index(drop=True)

    if "datetime" in out.columns:
        out = out.sort_values(["symbol", "datetime"]).reset_index(drop=True)

    return out