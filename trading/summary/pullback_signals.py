# ============================================================
# File   : trading/summary/pullback_signals.py
# Version: PRODUCTION-STABLE-REV1.0-PULLBACK-SIGNALS
# ------------------------------------------------------------
# 【概要】
#   押し目エントリー判定用シグナル生成
#
# 【主な機能】
#   - 既存の summary DataFrame に対して押し目関連列を追加
#   - pullback_score / pullback_score_v2 を算出
#   - strict / normal / early の3段階エントリー判定
#   - AI gate に渡しやすい subtype / reason / entry_score_v2 を付与
#
# 【前提列】
#   基本:
#     symbol, datetime, open, high, low, close, volume
#
#   あれば使う:
#     ma5, ma25, ma75
#     rsi, macd, signal
#     score, score_buy, score_sell
#     score_total, final_score, display_score
#     slope, slope_atr_scaled, score_slope
#     mtf, score_mtf
#     vwap
#     recent_breakout_level
#
# 【設計方針】
#   - 不足列は自動補完
#   - symbol ごとに時系列処理
#   - 既存の final_score 系を温存し、pullback 専用スコアを追加
#   - 実戦向けに危険シグナルは減点で反映
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd


# ------------------------------------------------------------
# ユーティリティ
# ------------------------------------------------------------
def _ensure_column(df: pd.DataFrame, col: str, default=0.0) -> None:
    if col not in df.columns:
        df[col] = default


def _safe_div(a, b, default=0.0):
    b = np.where(np.abs(b) < 1e-12, np.nan, b)
    out = a / b
    if isinstance(out, pd.Series):
        return out.replace([np.inf, -np.inf], np.nan).fillna(default)
    return np.nan_to_num(out, nan=default, posinf=default, neginf=default)


def _bool_to_int(cond) -> np.ndarray:
    return np.where(cond, 1, 0)


def _clip_series(s: pd.Series, low=None, high=None) -> pd.Series:
    if low is not None:
        s = s.clip(lower=low)
    if high is not None:
        s = s.clip(upper=high)
    return s


# ------------------------------------------------------------
# 理由文字列作成
# ------------------------------------------------------------
def _build_reason_row(row: pd.Series) -> str:
    reasons = []

    if bool(row.get("touch_ma25", False)):
        reasons.append("25MA押し")
    elif bool(row.get("touch_ma5", False)):
        reasons.append("5MA押し")

    if bool(row.get("vwap_reclaim", False)):
        reasons.append("VWAP回復")

    if bool(row.get("pullback_near_support", False)):
        reasons.append("支持線近辺")

    if bool(row.get("rsi_rebound", False)):
        reasons.append("RSI反転")

    if bool(row.get("macd_rebound", False)):
        reasons.append("MACD再上向き")

    if bool(row.get("higher_low", False)):
        reasons.append("安値切り上げ")

    if bool(row.get("break_prev_high", False)):
        reasons.append("前足高値突破")

    if bool(row.get("volume_reexpand", False)):
        reasons.append("出来高再拡大")

    if bool(row.get("vol_dryup", False)):
        reasons.append("押し中の出来高減少")

    if bool(row.get("vol_recovery", False)):
        reasons.append("反発出来高")

    if bool(row.get("trend_ok", False)):
        reasons.append("上昇トレンド維持")

    if bool(row.get("mtf_keep", False)):
        reasons.append("MTF維持")

    return " / ".join(reasons)


# ------------------------------------------------------------
# メイン
# ------------------------------------------------------------
def add_pullback_signals(
    df: pd.DataFrame,
    *,
    strict_threshold: float = 72.0,
    normal_threshold: float = 60.0,
    early_threshold: float = 52.0,
) -> pd.DataFrame:
    """
    Summary DataFrame に押し目判定列を追加して返す。

    Parameters
    ----------
    df : pd.DataFrame
        summary DataFrame
    strict_threshold : float
        strict 判定閾値
    normal_threshold : float
        normal 判定閾値
    early_threshold : float
        early 判定閾値

    Returns
    -------
    pd.DataFrame
        押し目関連列追加済み DataFrame
    """
    if df is None or len(df) == 0:
        return df

    out = df.copy()

    # --------------------------------------------------------
    # 必須/任意列の補完
    # --------------------------------------------------------
    required_defaults = {
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
        "score": 0.0,
        "score_buy": 0.0,
        "score_sell": 0.0,
        "score_total": 0.0,
        "final_score": 0.0,
        "display_score": 0.0,
        "slope": 0.0,
        "slope_atr_scaled": 0.0,
        "score_slope": 0.0,
        "mtf": 0.0,
        "score_mtf": 0.0,
    }
    for c, d in required_defaults.items():
        _ensure_column(out, c, d)

    # 任意列
    if "vwap" not in out.columns:
        out["vwap"] = np.nan
    if "recent_breakout_level" not in out.columns:
        out["recent_breakout_level"] = np.nan

    # datetime 整形
    if not pd.api.types.is_datetime64_any_dtype(out["datetime"]):
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")

    # symbol / datetime sort
    sort_cols = ["symbol"]
    if "datetime" in out.columns:
        sort_cols.append("datetime")
    out = out.sort_values(sort_cols).reset_index(drop=True)

    # --------------------------------------------------------
    # per symbol 処理
    # --------------------------------------------------------
    def _per_symbol(g: pd.DataFrame) -> pd.DataFrame:
        g = g.copy()
        eps = 1e-9

        # 基本価格整形
        g["high"] = g[["open", "high", "low", "close"]].max(axis=1)
        g["low"] = g[["open", "high", "low", "close"]].min(axis=1)
        g["candle_range"] = (g["high"] - g["low"]).clip(lower=eps)
        g["body_size"] = (g["close"] - g["open"]).abs()
        g["body_ratio"] = _safe_div(g["body_size"], g["candle_range"], 0.0)

        g["lower_hige"] = np.minimum(g["open"], g["close"]) - g["low"]
        g["upper_hige"] = g["high"] - np.maximum(g["open"], g["close"])
        g["lower_hige_ratio"] = _safe_div(g["lower_hige"], g["candle_range"], 0.0)
        g["upper_hige_ratio"] = _safe_div(g["upper_hige"], g["candle_range"], 0.0)
        g["close_position_in_range"] = _safe_div(g["close"] - g["low"], g["candle_range"], 0.0)

        # ----------------------------------------------------
        # 直近高値/押し深さ
        # ----------------------------------------------------
        g["recent_high_5"] = g["high"].rolling(5, min_periods=1).max()
        g["recent_high_10"] = g["high"].rolling(10, min_periods=1).max()
        g["pullback_depth_pct"] = _safe_div(
            (g["recent_high_5"] - g["close"]).clip(lower=0),
            g["recent_high_5"].replace(0, np.nan),
            0.0,
        )

        # ----------------------------------------------------
        # トレンド前提
        # ----------------------------------------------------
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

        g["mtf_keep"] = (g["score_mtf"] > 0) | (g["mtf"] > 0)
        g["slope_keep"] = (g["score_slope"] > 0) | (g["slope_atr_scaled"] > 0) | (g["slope"] > 0)

        # ----------------------------------------------------
        # MA / VWAP / breakout level タッチ
        # ----------------------------------------------------
        g["touch_ma5"] = (g["low"] <= g["ma5"]) & (g["close"] >= g["ma5"])
        g["touch_ma25"] = (g["low"] <= g["ma25"]) & (g["close"] >= g["ma25"])
        g["touch_ma75"] = (g["low"] <= g["ma75"]) & (g["close"] >= g["ma75"])

        has_vwap = g["vwap"].notna()
        g["price_above_vwap"] = has_vwap & (g["close"] >= g["vwap"])
        g["vwap_reclaim"] = has_vwap & (g["low"] <= g["vwap"]) & (g["close"] >= g["vwap"])

        has_breakout = g["recent_breakout_level"].notna()
        g["pullback_to_breakout_level"] = (
            has_breakout &
            (_safe_div((g["close"] - g["recent_breakout_level"]).abs(), g["close"].replace(0, np.nan), 999.0) <= 0.0045)
        )

        g["pullback_at_vwap"] = has_vwap & (
            _safe_div((g["close"] - g["vwap"]).abs(), g["close"].replace(0, np.nan), 999.0) <= 0.0035
        )

        g["dist_ma5_pct"] = _safe_div((g["close"] - g["ma5"]).abs(), g["close"].replace(0, np.nan), 999.0)
        g["dist_ma25_pct"] = _safe_div((g["close"] - g["ma25"]).abs(), g["close"].replace(0, np.nan), 999.0)
        g["dist_ma75_pct"] = _safe_div((g["close"] - g["ma75"]).abs(), g["close"].replace(0, np.nan), 999.0)
        g["dist_vwap_pct"] = np.where(
            has_vwap,
            _safe_div((g["close"] - g["vwap"]).abs(), g["close"].replace(0, np.nan), 999.0),
            999.0,
        )
        g["dist_breakout_pct"] = np.where(
            has_breakout,
            _safe_div((g["close"] - g["recent_breakout_level"]).abs(), g["close"].replace(0, np.nan), 999.0),
            999.0,
        )

        g["pullback_near_support"] = (
            (g["dist_ma5_pct"] <= 0.0030) |
            (g["dist_ma25_pct"] <= 0.0040) |
            (g["dist_vwap_pct"] <= 0.0030) |
            (g["dist_breakout_pct"] <= 0.0045)
        )

        # ----------------------------------------------------
        # RSI / MACD 反転
        # ----------------------------------------------------
        g["rsi_prev"] = g["rsi"].shift(1)
        g["rsi_rebound"] = (g["rsi"] > g["rsi_prev"]) & (g["rsi"] >= 50)
        g["rsi_good_zone"] = (g["rsi"] >= 48) & (g["rsi"] <= 68)

        g["macd_prev"] = g["macd"].shift(1)
        g["macd_rebound"] = (g["macd"] > g["signal"]) & (g["macd"] >= g["macd_prev"])

        # ----------------------------------------------------
        # 安値切り上げ / 前足高値突破
        # ----------------------------------------------------
        g["low_prev1"] = g["low"].shift(1)
        g["low_prev2"] = g["low"].shift(2)
        g["higher_low"] = (
            (g["low"] > g["low_prev1"]) |
            ((g["low_prev1"] > g["low_prev2"]) & (g["close"] >= g["close"].shift(1)))
        )

        g["prev_high"] = g["high"].shift(1)
        g["break_prev_high"] = g["close"] > g["prev_high"]
        g["high_break_prev_high"] = g["high"] > g["high"].shift(1)
        g["close_gt_prev_close"] = g["close"] > g["close"].shift(1)

        # ----------------------------------------------------
        # 出来高
        # ----------------------------------------------------
        g["volume_ma3"] = g["volume"].rolling(3, min_periods=1).mean()
        g["volume_ma5"] = g["volume"].rolling(5, min_periods=1).mean()
        g["volume_ma10"] = g["volume"].rolling(10, min_periods=1).mean()

        g["volume_reexpand"] = g["volume"] > g["volume_ma5"]
        g["vol_dryup"] = g["volume"] < g["volume_ma5"]
        g["vol_recovery"] = (g["volume"] > g["volume_ma5"]) & (g["close"] > g["open"])

        # ----------------------------------------------------
        # スコア変化から需給
        # ----------------------------------------------------
        g["score_buy_diff"] = g["score_buy"] - g["score_buy"].shift(1)
        g["score_sell_diff"] = g["score_sell"] - g["score_sell"].shift(1)

        g["sell_exhaustion"] = g["score_sell_diff"] < 0
        g["buy_recover"] = g["score_buy_diff"] > 0

        # ----------------------------------------------------
        # 押しの質
        # ----------------------------------------------------
        g["pullback_shallow_good"] = (
            (g["pullback_depth_pct"] >= 0.003) &
            (g["pullback_depth_pct"] <= 0.018)
        )
        g["pullback_too_deep"] = g["pullback_depth_pct"] > 0.035
        g["pullback_too_shallow"] = g["pullback_depth_pct"] < 0.001

        g["red_count_3"] = (g["close"] < g["open"]).rolling(3, min_periods=1).sum()
        g["tight_pullback"] = (
            (g["red_count_3"] <= 2) &
            (g["pullback_depth_pct"] <= 0.020)
        )

        # ----------------------------------------------------
        # ローソク足反転
        # ----------------------------------------------------
        g["reversal_candle"] = (
            (g["close"] > g["open"]) &
            (g["lower_hige_ratio"] >= 0.30) &
            (g["close_position_in_range"] >= 0.65)
        )

        g["prev_range"] = (g["high"].shift(1) - g["low"].shift(1)).clip(lower=eps)
        g["range_expansion"] = g["candle_range"] > g["prev_range"]

        # ----------------------------------------------------
        # 個別スコア
        # ----------------------------------------------------
        # 深さ
        g["pullback_depth_score"] = np.select(
            [
                (g["pullback_depth_pct"] >= 0.003) & (g["pullback_depth_pct"] <= 0.020),
                (g["pullback_depth_pct"] > 0.020) & (g["pullback_depth_pct"] <= 0.035),
                (g["pullback_depth_pct"] >= 0.001) & (g["pullback_depth_pct"] < 0.003),
                g["pullback_too_deep"],
                g["pullback_too_shallow"],
            ],
            [20, 10, 5, -20, -8],
            default=-8,
        )

        g["pullback_quality_depth_score"] = np.select(
            [
                g["pullback_shallow_good"],
                (g["pullback_depth_pct"] > 0.018) & (g["pullback_depth_pct"] <= 0.030),
                g["pullback_too_deep"],
            ],
            [16, 8, -25],
            default=-8,
        )

        # MA系
        g["ma_pullback_score"] = np.select(
            [
                g["touch_ma25"],
                g["touch_ma5"],
                g["touch_ma75"],
            ],
            [15, 10, 4],
            default=0,
        )

        # 支持線近辺
        g["pullback_near_support_score"] = np.select(
            [
                (g["dist_breakout_pct"] <= 0.0045),
                (g["dist_ma25_pct"] <= 0.0040),
                (g["dist_vwap_pct"] <= 0.0030),
                (g["dist_ma5_pct"] <= 0.0030),
            ],
            [14, 14, 12, 8],
            default=0,
        )

        # RSI
        g["rsi_score"] = np.select(
            [
                g["rsi_rebound"] & g["rsi_good_zone"],
                g["rsi_rebound"],
                g["rsi"] < 45,
            ],
            [10, 5, -5],
            default=0,
        )

        # MACD
        g["macd_score"] = np.select(
            [
                g["macd_rebound"],
                g["macd"] < g["signal"],
            ],
            [12, -8],
            default=0,
        )

        # 高値安値構造
        g["higher_low_score"] = np.where(g["higher_low"], 10, -5)
        g["break_score"] = np.where(g["break_prev_high"], 15, 0)

        # 出来高
        g["volume_score"] = np.where(g["volume_reexpand"], 8, 0)
        g["vol_dryup_score"] = np.where(g["vol_dryup"], 6, -2)
        g["vol_recovery_score"] = np.where(g["vol_recovery"], 10, 0)

        # フロー
        g["flow_score"] = 0.0
        g["flow_score"] += np.where(g["sell_exhaustion"], 8, 0)
        g["flow_score"] += np.where(g["buy_recover"], 8, 0)
        g["flow_score"] += np.where(g["score_buy"] > g["score_sell"], 5, -5)

        g["orderflow_score"] = 0.0
        g["orderflow_score"] += np.where(g["sell_exhaustion"], 8, -3)
        g["orderflow_score"] += np.where(g["buy_recover"], 10, -3)
        g["orderflow_score"] += np.where((g["score_buy"] - g["score_sell"]) > 0, 8, -8)

        # MTF / slope
        g["mtf_score_pb"] = np.where(g["mtf_keep"], 10, -10)
        g["slope_score_pb"] = np.where(g["slope_keep"], 8, -8)

        g["trend_strength_score"] = 0.0
        g["trend_strength_score"] += np.where(g["ma5"] > g["ma25"], 5, -5)
        g["trend_strength_score"] += np.where(g["ma25"] > g["ma75"], 8, -8)
        g["trend_strength_score"] += np.where(g["close"] > g["ma25"], 6, -4)
        g["trend_strength_score"] += np.where(g["close"] > g["ma75"], 6, -8)
        g["trend_strength_score"] += np.where(g["score_buy"] > g["score_sell"], 6, -6)
        g["trend_strength_score"] += np.where(g["final_score"] > 0, 8, -8)

        g["mtf_alignment_score"] = 0.0
        g["mtf_alignment_score"] += np.where(g["score_mtf"] > 0, 12, -12)
        g["mtf_alignment_score"] += np.where(g["mtf"] > 0, 8, -8)
        g["mtf_alignment_score"] += np.where(g["score_slope"] > 0, 6, -6)
        g["mtf_alignment_score"] += np.where(g["slope_atr_scaled"] > 0, 6, -6)

        # ローソク足
        g["reversal_candle_score"] = 0.0
        g["reversal_candle_score"] += np.where(g["close"] > g["open"], 6, -2)
        g["reversal_candle_score"] += np.where(g["lower_hige_ratio"] >= 0.35, 8, 0)
        g["reversal_candle_score"] += np.where(g["close_position_in_range"] >= 0.70, 8, -4)
        g["reversal_candle_score"] += np.where(g["body_ratio"] >= 0.35, 4, 0)

        g["range_expansion_score"] = np.where(g["range_expansion"], 6, 0)

        g["rebound_strength_score"] = 0.0
        g["rebound_strength_score"] += np.where(g["close_gt_prev_close"], 6, -4)
        g["rebound_strength_score"] += np.where(g["break_prev_high"], 12, 0)
        g["rebound_strength_score"] += np.where(g["high_break_prev_high"], 5, 0)
        g["rebound_strength_score"] += np.where(g["range_expansion"], 6, 0)

        # tight pullback
        g["tight_pullback_score"] = np.where(g["tight_pullback"], 8, -4)

        # VWAP
        g["vwap_score"] = 0.0
        g["vwap_score"] += np.where(g["price_above_vwap"], 6, -8)
        g["vwap_score"] += np.where(g["vwap_reclaim"], 10, 0)

        # ----------------------------------------------------
        # 危険シグナル
        # ----------------------------------------------------
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
        # 基本版 pullback_score
        # ----------------------------------------------------
        g["pullback_score_raw"] = (
            g["pullback_depth_score"]
            + g["ma_pullback_score"]
            + g["rsi_score"]
            + g["macd_score"]
            + g["higher_low_score"]
            + g["break_score"]
            + g["volume_score"]
            + g["flow_score"]
            + g["mtf_score_pb"]
            + g["slope_score_pb"]
        )
        g["pullback_score"] = np.where(
            g["trend_ok"],
            g["pullback_score_raw"],
            g["pullback_score_raw"] - 40,
        )
        g["pullback_score"] = _clip_series(g["pullback_score"], 0, 100)

        # ----------------------------------------------------
        # v2 スコア
        # ----------------------------------------------------
        g["pullback_quality_score"] = (
            g["pullback_quality_depth_score"]
            + g["ma_pullback_score"]
            + g["pullback_near_support_score"]
            + g["tight_pullback_score"]
            + g["vol_dryup_score"]
        )

        g["rebound_strength_score_total"] = (
            g["reversal_candle_score"]
            + g["rebound_strength_score"]
            + g["rsi_score"]
            + g["macd_score"]
            + g["volume_score"]
            + g["vol_recovery_score"]
            + g["vwap_score"]
        )

        g["trend_context_score"] = (
            g["trend_strength_score"]
            + g["mtf_alignment_score"]
            + g["orderflow_score"]
        )

        g["entry_timing_score"] = 0.0
        g["entry_timing_score"] += np.where(g["break_prev_high"], 15, 0)
        g["entry_timing_score"] += np.where(g["rsi_rebound"], 8, 0)
        g["entry_timing_score"] += np.where(g["macd_rebound"], 8, 0)
        g["entry_timing_score"] += np.where(g["vol_recovery"], 10, 0)
        g["entry_timing_score"] += np.where(g["reversal_candle_score"] >= 10, 8, 0)
        g["entry_timing_score"] += np.where(g["close_position_in_range"] >= 0.70, 6, -4)

        g["pullback_score_v2_raw"] = (
            g["pullback_quality_score"]
            + g["rebound_strength_score_total"]
            + g["trend_context_score"]
            + g["entry_timing_score"]
            + g["danger_penalty_score"]
        )
        g["pullback_score_v2"] = _clip_series(g["pullback_score_v2_raw"], 0, 100)

        # ----------------------------------------------------
        # 確認条件
        # ----------------------------------------------------
        g["rebound_confirm"] = (
            g["break_prev_high"] |
            g["rsi_rebound"] |
            g["macd_rebound"] |
            g["buy_recover"]
        )

        # ----------------------------------------------------
        # 3段階エントリー判定
        # ----------------------------------------------------
        g["is_pullback_entry_strict"] = (
            g["trend_ok_strict"] &
            (g["pullback_depth_pct"] >= 0.003) &
            (g["pullback_depth_pct"] <= 0.025) &
            (g["touch_ma25"] | g["vwap_reclaim"]) &
            g["higher_low"] &
            g["break_prev_high"] &
            (g["vol_recovery"] | g["volume_reexpand"]) &
            (g["pullback_score_v2"] >= strict_threshold)
        )

        g["is_pullback_entry_normal"] = (
            g["trend_ok"] &
            (g["pullback_depth_pct"] >= 0.002) &
            (g["pullback_depth_pct"] <= 0.030) &
            (g["touch_ma5"] | g["touch_ma25"] | g["vwap_reclaim"]) &
            (g["rsi_rebound"] | g["macd_rebound"] | g["break_prev_high"]) &
            (g["pullback_score_v2"] >= normal_threshold)
        )

        g["is_pullback_entry_early"] = (
            g["trend_ok"] &
            (g["pullback_depth_pct"] >= 0.001) &
            (g["pullback_depth_pct"] <= 0.030) &
            (g["touch_ma5"] | g["touch_ma25"]) &
            (g["rsi_rebound"] | g["buy_recover"]) &
            (g["pullback_score_v2"] >= early_threshold)
        )

        g["is_pullback_entry"] = (
            g["is_pullback_entry_strict"] |
            g["is_pullback_entry_normal"] |
            g["is_pullback_entry_early"]
        )

        # ----------------------------------------------------
        # subtype
        # ----------------------------------------------------
        g["pullback_subtype"] = np.select(
            [
                g["touch_ma25"] & g["break_prev_high"],
                g["touch_ma5"] & g["rsi_rebound"],
                g["vwap_reclaim"] & g["vol_recovery"],
                g["pullback_near_support"] & (g["reversal_candle_score"] >= 10),
            ],
            [
                "ma25_rebound",
                "ma5_rebound",
                "vwap_reclaim",
                "support_bounce",
            ],
            default="generic_pullback",
        )

        g["entry_setup_type"] = np.where(g["is_pullback_entry"], "pullback", "")

        # ----------------------------------------------------
        # 最終エントリースコア
        # ----------------------------------------------------
        g["entry_score"] = (
            g["final_score"] * 0.45
            + g["score_buy"] * 0.20
            + g["pullback_score"] * 0.25
            + np.maximum(g["score_mtf"], 0) * 0.05
            + np.maximum(g["score_slope"], 0) * 0.05
        )

        g["entry_score_v2"] = (
            g["final_score"] * 0.30
            + g["score_buy"] * 0.15
            + g["pullback_score_v2"] * 0.30
            + np.maximum(g["score_mtf"], 0) * 0.10
            + np.maximum(g["score_slope"], 0) * 0.05
            + g["entry_timing_score"] * 0.10
        )

        # ----------------------------------------------------
        # シグナル強度ラベル
        # ----------------------------------------------------
        g["pullback_signal_rank"] = np.select(
            [
                g["is_pullback_entry_strict"],
                g["is_pullback_entry_normal"],
                g["is_pullback_entry_early"],
            ],
            ["strict", "normal", "early"],
            default="",
        )

        # ----------------------------------------------------
        # 理由
        # ----------------------------------------------------
        g["pullback_reason"] = g.apply(_build_reason_row, axis=1)

        return g

    out = out.groupby("symbol", group_keys=False).apply(_per_symbol).reset_index(drop=True)

    # groupby/apply 後に index が崩れることがあるので軽く整える
    if "datetime" in out.columns:
        out = out.sort_values(["symbol", "datetime"]).reset_index(drop=True)

    return out