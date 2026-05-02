# ============================================================
# File   : trading/scoring/core/score_calculator.py
# Version: Ver4.4-INSTITUTIONAL-ALGO-SCORE-PRODUCTION-DETAIL-AWARE
#          -MTF-PRESERVE-FIXED-FINAL
# ------------------------------------------------------------
# ✔ Ver4.3 機能完全保持（削除ゼロ）
# ✔ institutional trading score
# ✔ smart money detection
# ✔ breakout scoring
# ✔ ranking velocity scoring
# ✔ orderbook pressure scoring
# ✔ slope / mtf integration
# ✔ AI integration
# ✔ vectorized
# ✔ NaN / inf safe
# ✔ column missing safe
# ✔ duplicate column safe
# ✔ extreme value protection
# ✔ rolling safety
# ✔ group diff safety
# ✔ dtype stabilization
# ✔ column alias absorption
# ✔ score column guarantee
# ✔ summary compatibility
# ✔ pandas alignment crash防止
# ✔ MultiIndex防御
# ✔ tuple/list/ndarray/dict列防御
# ✔ score_slope / score_mtf 正式保持
# ✔ slope / mtf のゼロ潰れ防止
# ✔ slope / mtf の過大寄与を抑制
# ✔ score_total の張り付き軽減
# ✔ detail_score_builder 生成列を正式採用
# ✔ score_base / score_trend / score_momentum / score_velocity / direction_penalty 保持
# ✔ base / trend / mom / vel / pen 表示互換を正式保持
# ✔ mtf は slope_atr_scaled を代用しない
# ✔ mtf / score_mtf / slope / score_slope は NaN preserve
# ✔ total合成に必要な時だけ一時的に 0 補完
# ✔ mtf_alignment_bonus を mtf 正式候補に採用
# ✔ mtf_raw / mtf_alignment / mtf_bonus 系も吸収
# ✔ NEW: 0列を優先して拾わない nonzero-preferred alias 解決
# ✔ NEW: mtf系入力を preserve-first で吸収
# ✔ NEW: score_mtf を mtf で上書きしない
# ✔ NEW: summary表示用 mtf / score_mtf を確実に残す
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# DATAFRAME SANITIZE
# ============================================================

def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    if df.empty:
        return df

    try:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [
                "_".join([str(x) for x in col if x not in ("", None)])
                for col in df.columns
            ]

        df.columns = [str(c) for c in df.columns]

        if df.columns.duplicated().any():
            dup = list(df.columns[df.columns.duplicated()])
            logger.warning(
                "[SCORE CALCULATOR] duplicate columns removed: %s",
                dup,
            )
            df = df.loc[:, ~df.columns.duplicated()]
    except Exception:
        pass

    return df


# ============================================================
# SAFE NUMERIC
# ============================================================

def _safe_numeric(df, col, default=0.0):
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")

    s = df[col]

    try:
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        elif isinstance(s, (list, tuple, np.ndarray)):
            s = pd.Series(s, index=df.index)
        elif isinstance(s, dict):
            s = pd.Series([s] * len(df), index=df.index)

        s = pd.to_numeric(s, errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan)
        s = s.fillna(default)

        return s.astype("float64")
    except Exception:
        return pd.Series(default, index=df.index, dtype="float64")


def _safe_numeric_nan(df, col):
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")

    s = df[col]

    try:
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        elif isinstance(s, (list, tuple, np.ndarray)):
            s = pd.Series(s, index=df.index)
        elif isinstance(s, dict):
            s = pd.Series([s] * len(df), index=df.index)

        s = pd.to_numeric(s, errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan)

        return s.astype("float64")
    except Exception:
        return pd.Series(np.nan, index=df.index, dtype="float64")


# ============================================================
# COLUMN ALIAS
# ============================================================

def _col(df, *names):
    for n in names:
        if n in df.columns:
            return _safe_numeric(df, n)
    return pd.Series(0.0, index=df.index, dtype="float64")


def _col_nan(df, *names):
    for n in names:
        if n in df.columns:
            return _safe_numeric_nan(df, n)
    return pd.Series(np.nan, index=df.index, dtype="float64")


def _coalesce_nan_series(*series_list):
    """
    NaN を優先的に埋める combine_first。
    0 は有効値として扱う。
    """
    valid = [s for s in series_list if isinstance(s, pd.Series)]
    if not valid:
        return pd.Series(dtype="float64")

    out = valid[0].copy()
    for s in valid[1:]:
        try:
            out = out.combine_first(s)
        except Exception:
            try:
                out = out.where(out.notna(), s)
            except Exception:
                pass
    return out


def _coalesce_prefer_nonzero(*series_list):
    """
    0/NaN しかない列より、nonzero を持つ列を優先する。
    mtf のように 0列が先頭に来て値を潰すケースを防ぐ。
    """
    valid = [s for s in series_list if isinstance(s, pd.Series)]
    if not valid:
        return pd.Series(dtype="float64")

    base = valid[0].copy()
    try:
        base = pd.to_numeric(base, errors="coerce")
    except Exception:
        base = pd.Series(np.nan, index=getattr(base, "index", None), dtype="float64")

    for s in valid[1:]:
        try:
            s_num = pd.to_numeric(s, errors="coerce")
            replace_mask = (
                (base.isna() | (base == 0))
                & s_num.notna()
                & (s_num != 0)
            )
            base = base.where(~replace_mask, s_num)
            base = base.combine_first(s_num)
        except Exception:
            pass

    return base.astype("float64")


# ============================================================
# SERIES SANITIZE
# ============================================================

def _sanitize_series(s, clip_min: float = -10000.0, clip_max: float = 10000.0):
    try:
        s = pd.to_numeric(s, errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan)
        s = s.fillna(0.0)
        s = s.clip(clip_min, clip_max)
        return s.astype("float64")
    except Exception:
        return pd.Series(0.0, index=s.index, dtype="float64")


def _sanitize_series_nan(s, clip_min: float = -10000.0, clip_max: float = 10000.0):
    try:
        s = pd.to_numeric(s, errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan)
        s = s.clip(clip_min, clip_max)
        return s.astype("float64")
    except Exception:
        return pd.Series(np.nan, index=s.index, dtype="float64")


def _safe_clip_series(s, lower: float, upper: float):
    try:
        return (
            pd.to_numeric(s, errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
            .clip(lower, upper)
        )
    except Exception:
        return pd.Series(
            0.0,
            index=s.index if hasattr(s, "index") else None,
            dtype="float64",
        )


def _nonzero_count(s: pd.Series) -> int:
    try:
        return int(pd.to_numeric(s, errors="coerce").fillna(0.0).ne(0).sum())
    except Exception:
        return 0


# ============================================================
# BREAKOUT SCORE
# ============================================================

def _breakout_score(df):
    close = _col(df, "close", "close_price", "price", "last")
    high = _col(df, "high", "high_price")

    ma5 = _col(df, "ma5")
    ma25 = _col(df, "ma25")

    prev_high = high.shift(1)

    breakout = (close > prev_high).astype("float64") * 2.0
    trend = (ma5 > ma25).astype("float64") * 1.2

    return breakout + trend


# ============================================================
# SMART MONEY SCORE
# ============================================================

def _smart_money_score(df):
    volume = _col(df, "volume")
    volume_ma = volume.rolling(20, min_periods=1).mean()

    vwap = _col(df, "vwap")
    close = _col(df, "close", "close_price", "price")

    volume_expansion = (volume / (volume_ma + 1)).clip(0, 10)
    vwap_deviation = ((close - vwap) / (vwap + 1)).clip(-0.1, 0.1)

    score = volume_expansion * 0.8 + vwap_deviation * 2.0
    return score.replace([np.inf, -np.inf], 0).astype("float64")


# ============================================================
# RANKING VELOCITY
# ============================================================

def _ranking_velocity_score(df):
    ranking = _safe_numeric(df, "ranking_score")

    try:
        if "symbol" in df.columns:
            velocity = ranking.groupby(df["symbol"]).diff()
        else:
            velocity = ranking.diff()
    except Exception:
        velocity = ranking.diff()

    velocity = velocity.fillna(0).clip(-20, 20)
    return (-velocity).astype("float64")


# ============================================================
# ORDERBOOK PRESSURE
# ============================================================

def _orderbook_pressure_score(df):
    bid = _col(df, "bid_volume")
    ask = _col(df, "ask_volume")

    denom = bid + ask + 1
    pressure = ((bid - ask) / denom).clip(-1, 1)

    return (pressure * 2.0).astype("float64")


# ============================================================
# SLOPE SCORE
# ============================================================

def _resolve_base_slope(df):
    # slope は 0列より nonzero列を優先
    raw = _coalesce_prefer_nonzero(
        _col_nan(df, "slope"),
        _col_nan(df, "score_slope"),
        _col_nan(df, "slope_atr_scaled"),
    )
    return _sanitize_series_nan(raw, clip_min=-5.0, clip_max=5.0)


def _slope_score(df):
    slope = _resolve_base_slope(df)
    slope = _safe_clip_series(slope, -2.0, 2.0).fillna(0.0)
    return (slope * 10.0).astype("float64")


# ============================================================
# MTF SCORE
# ============================================================

def _resolve_base_mtf(df):
    """
    Ver4.4:
    mtf は 0列優先を避け、nonzero を持つ列を優先して吸収する。
    slope_atr_scaled は代用しない。
    """
    candidates = [
        _col_nan(df, "mtf"),
        _col_nan(df, "score_mtf"),
        _col_nan(df, "mtf_alignment_bonus"),
        _col_nan(df, "mtf_alignment"),
        _col_nan(df, "mtf_bonus"),
        _col_nan(df, "mtf_raw"),
    ]

    raw = _coalesce_prefer_nonzero(*candidates)
    raw = _sanitize_series_nan(raw, clip_min=-5.0, clip_max=5.0)
    return raw


def _mtf_score(df):
    mtf = _resolve_base_mtf(df)
    mtf = _safe_clip_series(mtf, -2.0, 2.0).fillna(0.0)
    return (mtf * 10.0).astype("float64")


# ============================================================
# FINAL SCORE CALCULATOR
# ============================================================

def calculate_final_scores(df: pd.DataFrame, interval=None):
    if df is None or df.empty:
        return df

    try:
        df_out = _sanitize_dataframe(df.copy())

        # ----------------------------------------------------
        # keep / restore base slope & mtf first
        # ----------------------------------------------------
        base_slope = _resolve_base_slope(df_out)
        base_mtf = _resolve_base_mtf(df_out)

        # ----------------------------------------------------
        # detailed scores from upstream builder
        # ----------------------------------------------------
        detail_base = _sanitize_series(
            _col(df_out, "score_base", "_score_base", "base"),
            clip_min=-300.0,
            clip_max=300.0,
        )
        detail_trend = _sanitize_series(
            _col(df_out, "score_trend", "_score_trend", "trend"),
            clip_min=-300.0,
            clip_max=300.0,
        )
        detail_momentum = _sanitize_series(
            _col(df_out, "score_momentum", "_score_momentum", "mom", "momentum"),
            clip_min=-300.0,
            clip_max=300.0,
        )
        detail_velocity = _sanitize_series(
            _col(df_out, "score_velocity", "_score_velocity", "vel", "velocity"),
            clip_min=-300.0,
            clip_max=300.0,
        )
        detail_penalty = _sanitize_series(
            _col(
                df_out,
                "direction_penalty",
                "direction_penalty_score",
                "penalty",
                "penalty_score",
                "pen",
            ).abs(),
            clip_min=0.0,
            clip_max=300.0,
        )

        # ----------------------------------------------------
        # BASE SCORES (bridge columns)
        # ----------------------------------------------------
        flag_score = _safe_numeric(df_out, "flag_score")
        absolute_score = _safe_numeric(df_out, "absolute_score")
        ai_score = _safe_numeric(df_out, "ai_score")

        momentum_score = _safe_numeric(df_out, "momentum_score")
        volume_score = _safe_numeric(df_out, "volume_score")
        liquidity_score = _safe_numeric(df_out, "liquidity_score")

        ranking_score = _safe_numeric(df_out, "ranking_score")
        theme_score = _safe_numeric(df_out, "theme_score")

        # ----------------------------------------------------
        # INSTITUTIONAL SIGNALS
        # ----------------------------------------------------
        breakout_score = _sanitize_series(_breakout_score(df_out))
        smart_money_score = _sanitize_series(_smart_money_score(df_out))
        ranking_velocity = _sanitize_series(_ranking_velocity_score(df_out))
        orderbook_pressure = _sanitize_series(_orderbook_pressure_score(df_out))

        slope_score = _sanitize_series(
            _slope_score(df_out),
            clip_min=-50.0,
            clip_max=50.0,
        )
        mtf_score = _sanitize_series(
            _mtf_score(df_out),
            clip_min=-50.0,
            clip_max=50.0,
        )

        # ----------------------------------------------------
        # score_slope / score_mtf 正式保持
        # score_mtf は base_mtf を保持し、後段で落とさない
        # ----------------------------------------------------
        df_out["score_slope"] = base_slope
        df_out["score_mtf"] = base_mtf

        # ----------------------------------------------------
        # WEIGHTS
        # ----------------------------------------------------
        w_flag = 1.0
        w_absolute = 1.2
        w_ai = 1.6
        w_momentum = 1.1
        w_volume = 1.0
        w_liquidity = 0.5
        w_ranking = 1.0
        w_theme = 0.6

        w_breakout = 2.0
        w_smart_money = 1.8
        w_velocity = 1.2
        w_orderbook = 1.5

        w_slope = 0.20
        w_mtf = 0.25

        # ----------------------------------------------------
        # BUY SCORE
        # ----------------------------------------------------
        buy_score = (
            flag_score * w_flag
            + absolute_score * w_absolute
            + ai_score * w_ai
            + momentum_score * w_momentum
            + volume_score * w_volume
            + liquidity_score * w_liquidity
            + ranking_score * w_ranking
            + theme_score * w_theme
            + breakout_score * w_breakout
            + smart_money_score * w_smart_money
            + ranking_velocity * w_velocity
            + orderbook_pressure * w_orderbook
            + slope_score * w_slope
            + mtf_score * w_mtf
        )

        # ----------------------------------------------------
        # SELL SCORE
        # ----------------------------------------------------
        sell_pressure = _safe_numeric(df_out, "sell_pressure")
        distribution = _safe_numeric(df_out, "distribution_score")
        volatility = _safe_numeric(df_out, "volatility_score")

        sell_score = (
            sell_pressure * 1.4
            + distribution * 1.2
            + volatility * 0.9
            - ai_score * 0.6
        )

        buy_score = _sanitize_series(buy_score, clip_min=-300.0, clip_max=300.0)
        sell_score = _sanitize_series(sell_score, clip_min=-300.0, clip_max=300.0)

        total_score = (buy_score - sell_score).clip(-500.0, 500.0)

        df_out["score_buy"] = buy_score
        df_out["score_sell"] = sell_score
        df_out["score_total"] = total_score
        df_out["score"] = df_out["score_total"]

        # ----------------------------------------------------
        # slope / mtf は表示互換のため既存値を保護
        # ----------------------------------------------------
        df_out["slope"] = base_slope
        df_out["mtf"] = base_mtf

        # ----------------------------------------------------
        # detail columns
        # ----------------------------------------------------
        df_out["score_base"] = detail_base
        df_out["score_trend"] = detail_trend
        df_out["score_momentum"] = detail_momentum
        df_out["score_velocity"] = detail_velocity
        df_out["direction_penalty"] = detail_penalty

        df_out["base"] = df_out["score_base"]
        df_out["trend"] = df_out["score_trend"]
        df_out["mom"] = df_out["score_momentum"]
        df_out["vel"] = df_out["score_velocity"]
        df_out["pen"] = df_out["direction_penalty"]

        df_out["score_rank"] = (
            df_out["score_total"]
            .rank(ascending=False, method="min")
            .fillna(0)
            .astype("int64")
        )

        try:
            logger.info(
                "[SCORE CALCULATOR] rows=%s interval=%s "
                "score_min=%.4f score_p50=%.4f score_p95=%.4f score_max=%.4f "
                "slope_nonnull=%s slope_nonzero=%s slope_min=%.4f slope_p50=%.4f slope_p95=%.4f slope_max=%.4f "
                "mtf_nonnull=%s mtf_nonzero=%s mtf_min=%s mtf_p50=%s mtf_p95=%s mtf_max=%s "
                "base_nonzero=%s trend_nonzero=%s mom_nonzero=%s vel_nonzero=%s pen_nonzero=%s "
                "mtf_sources_nonzero={mtf:%s score_mtf:%s mtf_alignment_bonus:%s mtf_alignment:%s mtf_bonus:%s mtf_raw:%s}",
                len(df_out),
                interval,
                float(df_out["score_total"].min()) if "score_total" in df_out.columns and not df_out.empty else 0.0,
                float(df_out["score_total"].quantile(0.50)) if "score_total" in df_out.columns and not df_out.empty else 0.0,
                float(df_out["score_total"].quantile(0.95)) if "score_total" in df_out.columns and not df_out.empty else 0.0,
                float(df_out["score_total"].max()) if "score_total" in df_out.columns and not df_out.empty else 0.0,
                int(pd.to_numeric(df_out["slope"], errors="coerce").notna().sum()),
                int(pd.to_numeric(df_out["slope"], errors="coerce").fillna(0.0).ne(0).sum()),
                float(pd.to_numeric(df_out["slope"], errors="coerce").min()) if "slope" in df_out.columns and pd.to_numeric(df_out["slope"], errors="coerce").notna().any() else 0.0,
                float(pd.to_numeric(df_out["slope"], errors="coerce").quantile(0.50)) if "slope" in df_out.columns and pd.to_numeric(df_out["slope"], errors="coerce").notna().any() else 0.0,
                float(pd.to_numeric(df_out["slope"], errors="coerce").quantile(0.95)) if "slope" in df_out.columns and pd.to_numeric(df_out["slope"], errors="coerce").notna().any() else 0.0,
                float(pd.to_numeric(df_out["slope"], errors="coerce").max()) if "slope" in df_out.columns and pd.to_numeric(df_out["slope"], errors="coerce").notna().any() else 0.0,
                int(pd.to_numeric(df_out["mtf"], errors="coerce").notna().sum()),
                int(pd.to_numeric(df_out["mtf"], errors="coerce").fillna(0.0).ne(0).sum()),
                float(pd.to_numeric(df_out["mtf"], errors="coerce").min()) if "mtf" in df_out.columns and pd.to_numeric(df_out["mtf"], errors="coerce").notna().any() else None,
                float(pd.to_numeric(df_out["mtf"], errors="coerce").quantile(0.50)) if "mtf" in df_out.columns and pd.to_numeric(df_out["mtf"], errors="coerce").notna().any() else None,
                float(pd.to_numeric(df_out["mtf"], errors="coerce").quantile(0.95)) if "mtf" in df_out.columns and pd.to_numeric(df_out["mtf"], errors="coerce").notna().any() else None,
                float(pd.to_numeric(df_out["mtf"], errors="coerce").max()) if "mtf" in df_out.columns and pd.to_numeric(df_out["mtf"], errors="coerce").notna().any() else None,
                int((pd.to_numeric(df_out["score_base"], errors="coerce").fillna(0.0) != 0).sum()),
                int((pd.to_numeric(df_out["score_trend"], errors="coerce").fillna(0.0) != 0).sum()),
                int((pd.to_numeric(df_out["score_momentum"], errors="coerce").fillna(0.0) != 0).sum()),
                int((pd.to_numeric(df_out["score_velocity"], errors="coerce").fillna(0.0) != 0).sum()),
                int((pd.to_numeric(df_out["direction_penalty"], errors="coerce").fillna(0.0) != 0).sum()),
                _nonzero_count(_col_nan(df_out, "mtf")),
                _nonzero_count(_col_nan(df_out, "score_mtf")),
                _nonzero_count(_col_nan(df_out, "mtf_alignment_bonus")),
                _nonzero_count(_col_nan(df_out, "mtf_alignment")),
                _nonzero_count(_col_nan(df_out, "mtf_bonus")),
                _nonzero_count(_col_nan(df_out, "mtf_raw")),
            )
        except Exception:
            logger.exception("[SCORE CALCULATOR] profile log failed")

        logger.debug(
            "[SCORE CALCULATOR] rows=%s interval=%s",
            len(df_out),
            interval,
        )

        return df_out

    except Exception:
        logger.exception("[SCORE CALCULATOR] error")
        return df