# ============================================================
# File   : trading/scoring/core/detail_score_builder.py
# Version: Ver2.0-PRODUCTION-DETAIL-SCORE-BUILDER-FULL-LATEST
# ------------------------------------------------------------
# ✔ score_config.ini の flag 群を実スコア化
# ✔ base / trend / momentum / velocity / penalty を明示列で保持
# ✔ score_calculator.py 互換
# ✔ base / trend / mom / vel / pen 表示互換
# ✔ BUY / SELL の主要 flag を広く吸収
# ✔ MACD cross / dead cross を momentum に反映
# ✔ RSI / MA / VWAP / breakout / volume / orderflow / AI 対応
# ✔ tick_count / trade_count 系対応
# ✔ sell 側 penalty を明示化
# ✔ flag_score / momentum_score / volume_score / sell_pressure へ橋渡し
# ✔ absolute_score / liquidity_score / distribution_score / volatility_score 補完
# ✔ NaN / inf safe
# ✔ duplicate column safe
# ✔ config missing safe
# ✔ scoring_core.py + utils.market_filter.py 前提
# ✔ ETF/ETN/REIT の最終除外は market_filter 側に委譲
# ✔ production hardened
# ============================================================

from __future__ import annotations

import configparser
import logging
import os
from typing import Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "config",
    "score_config.ini",
)


# ============================================================
# basic helpers
# ============================================================

def _ensure_dataframe(df) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()

    if isinstance(df, pd.DataFrame):
        out = df.copy()
    elif isinstance(df, pd.Series):
        out = pd.DataFrame([df.to_dict()])
    elif isinstance(df, dict):
        out = pd.DataFrame([df])
    else:
        try:
            out = pd.DataFrame(df).copy()
        except Exception:
            logger.exception("[DETAIL SCORE] dataframe conversion failed")
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in col if x not in ("", None)])
                for col in out.columns.to_flat_index()
            ]
    except Exception:
        logger.debug("[DETAIL SCORE] multiindex flatten failed", exc_info=True)

    try:
        out.columns = [str(c) for c in out.columns]
    except Exception:
        logger.debug("[DETAIL SCORE] stringify columns failed", exc_info=True)

    try:
        if out.columns.duplicated().any():
            dup = list(out.columns[out.columns.duplicated()])
            logger.warning("[DETAIL SCORE] duplicate columns removed: %s", dup)
            out = out.loc[:, ~out.columns.duplicated(keep="last")].copy()
    except Exception:
        logger.debug("[DETAIL SCORE] duplicate column cleanup failed", exc_info=True)

    try:
        out = out.replace([np.inf, -np.inf], np.nan)
    except Exception:
        logger.debug("[DETAIL SCORE] inf replace failed", exc_info=True)

    try:
        out = out.reset_index(drop=True)
    except Exception:
        logger.debug("[DETAIL SCORE] reset_index failed", exc_info=True)

    return out


def _safe_numeric(df: pd.DataFrame, col: str, default=0.0) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(
            default,
            index=df.index if isinstance(df, pd.DataFrame) else None,
            dtype="float64",
        )

    try:
        s = df[col]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        s = pd.to_numeric(s, errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan).fillna(default)
        return s.astype("float64")
    except Exception:
        logger.debug("[DETAIL SCORE] safe_numeric failed col=%s", col, exc_info=True)
        return pd.Series(default, index=df.index, dtype="float64")


def _pick_numeric(df: pd.DataFrame, cols: Iterable[str], default=0.0) -> pd.Series:
    for c in cols:
        if c in df.columns:
            return _safe_numeric(df, c, default=default)
    return pd.Series(default, index=df.index, dtype="float64")


def _pick_text(df: pd.DataFrame, cols: Iterable[str], default="") -> pd.Series:
    for c in cols:
        if c in df.columns:
            try:
                s = df[c]
                if isinstance(s, pd.DataFrame):
                    s = s.iloc[:, 0]
                s = s.astype(str).fillna(default)
                s = s.replace({"nan": default, "None": default, "<NA>": default})
                return s.reindex(df.index, fill_value=default)
            except Exception:
                logger.debug("[DETAIL SCORE] pick_text failed col=%s", c, exc_info=True)
    return pd.Series(default, index=df.index, dtype="object")


def _safe_bool(df: pd.DataFrame, col: str) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)

    try:
        s = df[col]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        if pd.api.types.is_bool_dtype(s):
            return s.fillna(False).astype(bool)
        return pd.to_numeric(s, errors="coerce").fillna(0.0).ne(0)
    except Exception:
        logger.debug("[DETAIL SCORE] safe_bool failed col=%s", col, exc_info=True)
        return pd.Series(False, index=df.index, dtype=bool)


def _clip(s: pd.Series, lower: float, upper: float) -> pd.Series:
    try:
        return (
            pd.to_numeric(s, errors="coerce")
            .replace([np.inf, -np.inf], np.nan)
            .fillna(0.0)
            .clip(lower, upper)
            .astype("float64")
        )
    except Exception:
        return pd.Series(
            0.0,
            index=s.index if hasattr(s, "index") else None,
            dtype="float64",
        )


def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "symbol" not in out.columns:
        for c in ("code", "ticker", "stock_code", "Symbol"):
            if c in out.columns:
                try:
                    out["symbol"] = out[c]
                    break
                except Exception:
                    pass

    if "symbol" in out.columns:
        try:
            out["symbol"] = (
                out["symbol"]
                .astype(str)
                .str.strip()
                .str.replace(r"\.0$", "", regex=True)
            )
        except Exception:
            logger.debug("[DETAIL SCORE] symbol normalize failed", exc_info=True)

    if "symbolname" not in out.columns:
        out["symbolname"] = _pick_text(out, ["name", "symbol"], default="")
    if "name" not in out.columns:
        out["name"] = _pick_text(out, ["symbolname", "symbol"], default="")

    return out


# ============================================================
# config loader
# ============================================================

def _load_score_config(config_path: str | None = None) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    path = config_path or DEFAULT_CONFIG_PATH

    try:
        loaded = cp.read(path, encoding="utf-8")
        if loaded:
            logger.info("[DETAIL SCORE] config loaded path=%s", path)
        else:
            logger.warning("[DETAIL SCORE] config not loaded path=%s", path)
    except Exception:
        logger.exception("[DETAIL SCORE] config load failed path=%s", path)

    return cp


def _cfg_float(
    cp: configparser.ConfigParser,
    section: str,
    key: str,
    default: float = 0.0,
) -> float:
    try:
        if cp.has_option(section, key):
            return float(cp.get(section, key))
    except Exception:
        logger.debug(
            "[DETAIL SCORE] cfg parse failed section=%s key=%s",
            section,
            key,
            exc_info=True,
        )
    return float(default)


# ============================================================
# feature builders
# ============================================================

def _close(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(
        df,
        ["close", "close_price", "price", "last_price", "current_price"],
        default=np.nan,
    )


def _open(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["open", "open_price"], default=np.nan)


def _high(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["high", "high_price"], default=np.nan)


def _low(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["low", "low_price"], default=np.nan)


def _volume(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["volume", "trading_volume"], default=0.0)


def _vwap(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["vwap"], default=np.nan)


def _ma5(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["ma5"], default=np.nan)


def _ma25(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["ma25"], default=np.nan)


def _ma75(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["ma75"], default=np.nan)


def _rsi(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["rsi"], default=np.nan)


def _macd(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["macd"], default=np.nan)


def _signal(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["signal", "macd_signal"], default=np.nan)


def _slope(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(
        df,
        ["slope", "score_slope", "slope_atr_scaled"],
        default=0.0,
    )


def _mtf(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["mtf", "score_mtf", "mtf_score"], default=0.0)


def _bid_volume(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["bid_volume"], default=0.0)


def _ask_volume(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["ask_volume"], default=0.0)


def _tick_count(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(
        df,
        ["tick_count", "trade_count", "execution_count", "executions"],
        default=0.0,
    )


def _ai_score(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(
        df,
        ["ai_score", "ai_confidence", "ai_conf"],
        default=0.0,
    )


def _atr(df: pd.DataFrame) -> pd.Series:
    return _pick_numeric(df, ["atr"], default=np.nan)


# ============================================================
# generic derived features
# ============================================================

def _volume_ratio(df: pd.DataFrame) -> pd.Series:
    vol = _volume(df)
    vol_ma = vol.rolling(20, min_periods=3).mean().replace(0, np.nan)
    return (vol / vol_ma).replace([np.inf, -np.inf], np.nan)


def _tick_ratio(df: pd.DataFrame) -> pd.Series:
    t = _tick_count(df)
    t_ma = t.rolling(20, min_periods=3).mean().replace(0, np.nan)
    return (t / t_ma).replace([np.inf, -np.inf], np.nan)


def _range_size(df: pd.DataFrame) -> pd.Series:
    return (_high(df) - _low(df)).abs()


def _range_ratio(df: pd.DataFrame) -> pd.Series:
    rng = _range_size(df)
    rng_ma = rng.rolling(20, min_periods=3).mean().replace(0, np.nan)
    return (rng / rng_ma).replace([np.inf, -np.inf], np.nan)


# ============================================================
# signal evaluators
# ============================================================

def _sig_dir_up(df: pd.DataFrame) -> pd.Series:
    return _close(df).gt(_close(df).shift(1))


def _sig_dir_down(df: pd.DataFrame) -> pd.Series:
    return _close(df).lt(_close(df).shift(1))


def _sig_slope_positive(df: pd.DataFrame) -> pd.Series:
    return _slope(df).gt(0.0)


def _sig_slope_negative(df: pd.DataFrame) -> pd.Series:
    return _slope(df).lt(0.0)


def _sig_trend_strength(df: pd.DataFrame) -> pd.Series:
    return _ma5(df).gt(_ma25(df)) & _ma25(df).gt(_ma75(df))


def _sig_ma5_ma25_cross(df: pd.DataFrame) -> pd.Series:
    ma5 = _ma5(df)
    ma25 = _ma25(df)
    return ma5.shift(1).le(ma25.shift(1)) & ma5.gt(ma25)


def _sig_ma_up(df: pd.DataFrame) -> pd.Series:
    return _ma5(df).gt(_ma5(df).shift(1))


def _sig_ma5_above_ma25(df: pd.DataFrame) -> pd.Series:
    return _ma5(df).gt(_ma25(df))


def _sig_ma25_above_ma75(df: pd.DataFrame) -> pd.Series:
    return _ma25(df).gt(_ma75(df))


def _sig_perfect_order_event(df: pd.DataFrame) -> pd.Series:
    return _sig_trend_strength(df)


def _sig_first_pullback(df: pd.DataFrame) -> pd.Series:
    close = _close(df)
    ma5 = _ma5(df)
    ma25 = _ma25(df)
    return close.ge(ma25) & close.le(ma5) & _sig_trend_strength(df)


def _sig_perfect_order_down(df: pd.DataFrame) -> pd.Series:
    return _ma5(df).lt(_ma25(df)) & _ma25(df).lt(_ma75(df))


def _sig_ma_alignment_down(df: pd.DataFrame) -> pd.Series:
    return _sig_perfect_order_down(df)


def _sig_ma5_downtrend(df: pd.DataFrame) -> pd.Series:
    return _ma5(df).lt(_ma5(df).shift(1))


def _sig_ma5_below_ma25(df: pd.DataFrame) -> pd.Series:
    return _ma5(df).lt(_ma25(df))


def _sig_breakout_high(df: pd.DataFrame) -> pd.Series:
    return _close(df).gt(_high(df).shift(1))


def _sig_range_breakout(df: pd.DataFrame) -> pd.Series:
    high20 = _high(df).rolling(20, min_periods=3).max().shift(1)
    return _close(df).gt(high20)


def _sig_range_expansion(df: pd.DataFrame) -> pd.Series:
    return _range_size(df).gt(_range_size(df).rolling(10, min_periods=3).mean())


def _sig_fib_rebound(df: pd.DataFrame) -> pd.Series:
    close = _close(df)
    low20 = _low(df).rolling(20, min_periods=5).min()
    high20 = _high(df).rolling(20, min_periods=5).max()
    fib38 = low20 + (high20 - low20) * 0.382
    return close.ge(fib38) & close.gt(close.shift(1))


def _sig_rebound_on_ma25(df: pd.DataFrame) -> pd.Series:
    close = _close(df)
    ma25 = _ma25(df)
    touch = (close.shift(1) <= ma25.shift(1) * 1.003) & (close.shift(1) >= ma25.shift(1) * 0.997)
    bounce = close > ma25
    return touch & bounce


def _sig_bollinger_rebound(df: pd.DataFrame) -> pd.Series:
    if "bb_lower" not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)
    close = _close(df)
    bb_lower = _pick_numeric(df, ["bb_lower"], default=np.nan)
    return close.shift(1).le(bb_lower.shift(1)) & close.gt(close.shift(1))


def _sig_bb_3sigma_rebound(df: pd.DataFrame) -> pd.Series:
    if "bb_lower_3sigma" not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)
    close = _close(df)
    bb3 = _pick_numeric(df, ["bb_lower_3sigma"], default=np.nan)
    return close.shift(1).le(bb3.shift(1)) & close.gt(close.shift(1))


def _sig_macd_cross_up(df: pd.DataFrame) -> pd.Series:
    macd = _macd(df)
    signal = _signal(df)
    return macd.shift(1).le(signal.shift(1)) & macd.gt(signal)


def _sig_macd_cross_down(df: pd.DataFrame) -> pd.Series:
    macd = _macd(df)
    signal = _signal(df)
    return macd.shift(1).ge(signal.shift(1)) & macd.lt(signal)


def _sig_macd_hist_expand(df: pd.DataFrame) -> pd.Series:
    hist = _macd(df) - _signal(df)
    return hist.gt(hist.shift(1))


def _sig_macd_hist_contract(df: pd.DataFrame) -> pd.Series:
    hist = _macd(df) - _signal(df)
    return hist.lt(hist.shift(1))


def _sig_rsi_rebound(df: pd.DataFrame) -> pd.Series:
    rsi = _rsi(df)
    return rsi.shift(1).lt(rsi) & rsi.ge(30.0) & rsi.shift(1).lt(35.0)


def _sig_rsi_midline_cross(df: pd.DataFrame) -> pd.Series:
    rsi = _rsi(df)
    return rsi.shift(1).lt(50.0) & rsi.ge(50.0)


def _sig_rsi_falling(df: pd.DataFrame) -> pd.Series:
    return _rsi(df).lt(_rsi(df).shift(1))


def _sig_rsi_overbought_70(df: pd.DataFrame) -> pd.Series:
    return _rsi(df).ge(70.0)


def _sig_rsi_oversold_30(df: pd.DataFrame) -> pd.Series:
    return _rsi(df).le(30.0)


def _sig_stoch_rebound(df: pd.DataFrame) -> pd.Series:
    if "stoch_k" not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)
    k = _pick_numeric(df, ["stoch_k"], default=np.nan)
    return k.shift(1).lt(20.0) & k.gt(k.shift(1))


def _sig_rci_rising(df: pd.DataFrame) -> pd.Series:
    if "rci" not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)
    rci = _pick_numeric(df, ["rci"], default=np.nan)
    return rci.gt(rci.shift(1))


def _sig_rci_trio_up(df: pd.DataFrame) -> pd.Series:
    if not {"rci9", "rci26", "rci52"}.issubset(df.columns):
        return pd.Series(False, index=df.index, dtype=bool)
    r9 = _pick_numeric(df, ["rci9"], default=np.nan)
    r26 = _pick_numeric(df, ["rci26"], default=np.nan)
    r52 = _pick_numeric(df, ["rci52"], default=np.nan)
    return r9.gt(r26) & r26.gt(r52)


def _sig_rci9_uptrend(df: pd.DataFrame) -> pd.Series:
    if "rci9" not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)
    r9 = _pick_numeric(df, ["rci9"], default=np.nan)
    return r9.gt(r9.shift(1))


def _sig_above_vwap(df: pd.DataFrame) -> pd.Series:
    return _close(df).gt(_vwap(df))


def _sig_vwap_break(df: pd.DataFrame) -> pd.Series:
    close = _close(df)
    vwap = _vwap(df)
    return close.shift(1).le(vwap.shift(1)) & close.gt(vwap)


def _sig_vwap_breakout(df: pd.DataFrame) -> pd.Series:
    close = _close(df)
    vwap = _vwap(df)
    return close.gt(vwap * 1.003)


def _sig_vwap_reclaim(df: pd.DataFrame) -> pd.Series:
    close = _close(df)
    vwap = _vwap(df)
    return close.shift(1).lt(vwap.shift(1)) & close.ge(vwap)


def _sig_vwap_fail(df: pd.DataFrame) -> pd.Series:
    close = _close(df)
    vwap = _vwap(df)
    return close.shift(1).ge(vwap.shift(1)) & close.lt(vwap)


def _sig_volume_spike(df: pd.DataFrame) -> pd.Series:
    return _volume_ratio(df).ge(2.0)


def _sig_volume_surge(df: pd.DataFrame) -> pd.Series:
    return _volume_ratio(df).ge(3.0)


def _sig_volume_expansion(df: pd.DataFrame) -> pd.Series:
    return _volume(df).gt(_volume(df).shift(1))


def _sig_volume_drop(df: pd.DataFrame) -> pd.Series:
    return _volume(df).lt(_volume(df).shift(1))


def _sig_volume_price_breakout(df: pd.DataFrame) -> pd.Series:
    return _sig_volume_spike(df) & _sig_breakout_high(df)


def _sig_volume_zone_break(df: pd.DataFrame) -> pd.Series:
    return _sig_volume_surge(df) & _sig_range_breakout(df)


def _sig_volume_peak_out(df: pd.DataFrame) -> pd.Series:
    return _volume_ratio(df).ge(2.0) & _close(df).lt(_close(df).shift(1))


def _sig_volume_exhaustion(df: pd.DataFrame) -> pd.Series:
    return _volume_ratio(df).ge(2.5) & _range_ratio(df).lt(1.0)

def _sig_orderflow_imbalance(df: pd.DataFrame) -> pd.Series:
    bid = _bid_volume(df)
    ask = _ask_volume(df)
    return bid.gt(ask * 1.1)


def _sig_board_pressure_up(df: pd.DataFrame) -> pd.Series:
    return _bid_volume(df).gt(_ask_volume(df) * 1.2)


def _sig_board_pressure_down(df: pd.DataFrame) -> pd.Series:
    return _ask_volume(df).gt(_bid_volume(df) * 1.2)


def _sig_bid_stack(df: pd.DataFrame) -> pd.Series:
    return _bid_volume(df).gt(_ask_volume(df) * 1.3)


def _sig_bid_dominance(df: pd.DataFrame) -> pd.Series:
    return _bid_volume(df).gt(_ask_volume(df) * 1.5)


def _sig_ask_stack(df: pd.DataFrame) -> pd.Series:
    return _ask_volume(df).gt(_bid_volume(df) * 1.3)


def _sig_ask_dominance(df: pd.DataFrame) -> pd.Series:
    return _ask_volume(df).gt(_bid_volume(df) * 1.5)


def _sig_tick_surge(df: pd.DataFrame) -> pd.Series:
    return _tick_ratio(df).ge(1.5)


def _sig_trade_count_spike(df: pd.DataFrame) -> pd.Series:
    return _tick_ratio(df).ge(2.0)


def _sig_volatility_expansion(df: pd.DataFrame) -> pd.Series:
    return _range_ratio(df).gt(1.2)


def _sig_volatility_breakout(df: pd.DataFrame) -> pd.Series:
    return _sig_volatility_expansion(df) & _sig_breakout_high(df)


def _sig_bb_lower_touch(df: pd.DataFrame) -> pd.Series:
    if "bb_lower" not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)
    close = _close(df)
    bb_lower = _pick_numeric(df, ["bb_lower"], default=np.nan)
    return close.le(bb_lower)


def _sig_ai_momentum_boost(df: pd.DataFrame) -> pd.Series:
    return _ai_score(df).ge(1.0)


def _sig_ai_ranking_boost(df: pd.DataFrame) -> pd.Series:
    return _ai_score(df).ge(2.0)


def _sig_ai_confidence_high(df: pd.DataFrame) -> pd.Series:
    return _ai_score(df).ge(3.0)


def _sig_ai_exit_signal(df: pd.DataFrame) -> pd.Series:
    return _ai_score(df).le(-1.0)


def _sig_ai_reversal_warning(df: pd.DataFrame) -> pd.Series:
    return _ai_score(df).le(-2.0)


# ============================================================
# builder
# ============================================================

def build_detail_scores(
    df: pd.DataFrame,
    config_path: str | None = None,
) -> pd.DataFrame:
    out = _ensure_dataframe(df)
    if out.empty:
        return out

    out = _normalize_symbol(out)
    cp = _load_score_config(config_path)
    idx = out.index

    score_base = pd.Series(0.0, index=idx, dtype="float64")
    score_trend = pd.Series(0.0, index=idx, dtype="float64")
    score_momentum = pd.Series(0.0, index=idx, dtype="float64")
    score_velocity = pd.Series(0.0, index=idx, dtype="float64")
    direction_penalty = pd.Series(0.0, index=idx, dtype="float64")

    # --------------------------------------------------------
    # base
    # --------------------------------------------------------
    score_base += _sig_dir_up(out).astype(float) * _cfg_float(cp, "scoring", "flag_dir_up")
    score_base += _sig_above_vwap(out).astype(float) * _cfg_float(cp, "scoring", "flag_above_vwap")
    score_base += _sig_vwap_break(out).astype(float) * _cfg_float(cp, "scoring", "flag_vwap_break")
    score_base += _sig_vwap_breakout(out).astype(float) * _cfg_float(cp, "scoring", "flag_vwap_breakout")
    score_base += _sig_vwap_reclaim(out).astype(float) * _cfg_float(cp, "scoring", "flag_vwap_reclaim")
    score_base += _sig_fib_rebound(out).astype(float) * _cfg_float(cp, "scoring", "flag_fib_rebound")
    score_base += _sig_rebound_on_ma25(out).astype(float) * _cfg_float(cp, "scoring", "flag_rebound_on_ma25")
    score_base += _sig_bollinger_rebound(out).astype(float) * _cfg_float(cp, "scoring", "flag_bollinger_rebound")
    score_base += _sig_bb_3sigma_rebound(out).astype(float) * _cfg_float(cp, "scoring", "flag_bb_3sigma_rebound")
    score_base += _sig_rsi_oversold_30(out).astype(float) * _cfg_float(cp, "scoring", "flag_rsi_oversold_30")
    score_base += _sig_bb_lower_touch(out).astype(float) * _cfg_float(cp, "scoring", "flag_bb_lower_touch")

    # --------------------------------------------------------
    # trend
    # --------------------------------------------------------
    score_trend += _sig_slope_positive(out).astype(float) * _cfg_float(cp, "scoring", "flag_slope_positive")
    score_trend += _sig_trend_strength(out).astype(float) * _cfg_float(cp, "scoring", "flag_trend_strength")
    score_trend += _sig_ma5_ma25_cross(out).astype(float) * _cfg_float(cp, "scoring", "flag_ma5_ma25_cross")
    score_trend += _sig_ma_up(out).astype(float) * _cfg_float(cp, "scoring", "flag_ma_up")
    score_trend += _sig_ma5_above_ma25(out).astype(float) * _cfg_float(cp, "scoring", "flag_ma5_above_ma25")
    score_trend += _sig_ma25_above_ma75(out).astype(float) * _cfg_float(cp, "scoring", "flag_ma25_above_ma75")
    score_trend += _sig_perfect_order_event(out).astype(float) * _cfg_float(cp, "scoring", "flag_perfect_order_event")
    score_trend += _sig_first_pullback(out).astype(float) * _cfg_float(cp, "scoring", "flag_first_pullback")
    score_trend += _sig_breakout_high(out).astype(float) * _cfg_float(cp, "scoring", "flag_breakout_high")
    score_trend += _sig_range_breakout(out).astype(float) * _cfg_float(cp, "scoring", "flag_range_breakout")
    score_trend += _sig_range_expansion(out).astype(float) * _cfg_float(cp, "scoring", "flag_range_expansion")

    # --------------------------------------------------------
    # momentum
    # --------------------------------------------------------
    score_momentum += _sig_macd_cross_up(out).astype(float) * _cfg_float(cp, "scoring", "flag_macd_cross")
    score_momentum += _sig_macd_hist_expand(out).astype(float) * _cfg_float(cp, "scoring", "flag_macd_hist_expand")
    score_momentum += _sig_rsi_rebound(out).astype(float) * _cfg_float(cp, "scoring", "flag_rsi_rebound")
    score_momentum += _sig_rsi_midline_cross(out).astype(float) * _cfg_float(cp, "scoring", "flag_rsi_midline_cross")
    score_momentum += _sig_stoch_rebound(out).astype(float) * _cfg_float(cp, "scoring", "flag_stoch_rebound")
    score_momentum += _sig_rci_rising(out).astype(float) * _cfg_float(cp, "scoring", "flag_rci_rising")
    score_momentum += _sig_rci_trio_up(out).astype(float) * _cfg_float(cp, "scoring", "flag_rci_trio_up")
    score_momentum += _sig_rci9_uptrend(out).astype(float) * _cfg_float(cp, "scoring", "flag_rci9_uptrend")

    # --------------------------------------------------------
    # velocity
    # --------------------------------------------------------
    score_velocity += _sig_volume_spike(out).astype(float) * _cfg_float(cp, "scoring", "flag_volume_spike")
    score_velocity += _sig_volume_surge(out).astype(float) * _cfg_float(cp, "scoring", "flag_volume_surge")
    score_velocity += _sig_volume_expansion(out).astype(float) * _cfg_float(cp, "scoring", "flag_volume_expansion")
    score_velocity += _sig_volume_price_breakout(out).astype(float) * _cfg_float(cp, "scoring", "flag_volume_price_breakout")
    score_velocity += _sig_volume_zone_break(out).astype(float) * _cfg_float(cp, "scoring", "flag_volume_zone_break")
    score_velocity += _sig_tick_surge(out).astype(float) * _cfg_float(cp, "scoring", "flag_tick_surge")
    score_velocity += _sig_trade_count_spike(out).astype(float) * _cfg_float(cp, "scoring", "flag_trade_count_spike")
    score_velocity += _sig_bid_stack(out).astype(float) * _cfg_float(cp, "scoring", "flag_bid_stack")
    score_velocity += _sig_bid_dominance(out).astype(float) * _cfg_float(cp, "scoring", "flag_bid_dominance")
    score_velocity += _sig_orderflow_imbalance(out).astype(float) * _cfg_float(cp, "scoring", "flag_orderflow_imbalance")
    score_velocity += _sig_board_pressure_up(out).astype(float) * _cfg_float(cp, "scoring", "flag_board_pressure_up")
    score_velocity += _sig_volatility_expansion(out).astype(float) * _cfg_float(cp, "scoring", "flag_volatility_expansion")
    score_velocity += _sig_volatility_breakout(out).astype(float) * _cfg_float(cp, "scoring", "flag_volatility_breakout")
    score_velocity += _sig_ai_momentum_boost(out).astype(float) * _cfg_float(cp, "scoring", "flag_ai_momentum_boost")
    score_velocity += _sig_ai_ranking_boost(out).astype(float) * _cfg_float(cp, "scoring", "flag_ai_ranking_boost")
    score_velocity += _sig_ai_confidence_high(out).astype(float) * _cfg_float(cp, "scoring", "flag_ai_confidence_high")

    # --------------------------------------------------------
    # penalty
    # --------------------------------------------------------
    direction_penalty += _sig_dir_down(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_dir_down"))
    direction_penalty += _sig_slope_negative(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_slope_negative"))
    direction_penalty += _sig_ma_alignment_down(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_ma_alignment_down"))
    direction_penalty += _sig_ma5_downtrend(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_ma5_downtrend"))
    direction_penalty += _sig_ma5_below_ma25(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_ma5_below_ma25"))
    direction_penalty += _sig_perfect_order_down(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_perfect_order_down"))
    direction_penalty += _sig_macd_cross_down(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_macd_dc"))
    direction_penalty += _sig_macd_hist_contract(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_macd_hist_contract"))
    direction_penalty += _sig_rsi_falling(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_rsi_falling"))
    direction_penalty += _sig_rsi_overbought_70(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_rsi_overbought_70"))
    direction_penalty += _sig_vwap_fail(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_vwap_fail"))
    direction_penalty += _sig_volume_drop(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_volume_drop"))
    direction_penalty += _sig_volume_peak_out(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_volume_peak_out"))
    direction_penalty += _sig_volume_exhaustion(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_volume_exhaustion"))
    direction_penalty += _sig_ask_stack(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_ask_stack"))
    direction_penalty += _sig_ask_dominance(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_ask_dominance"))
    direction_penalty += _sig_board_pressure_down(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_board_pressure_down"))
    direction_penalty += _sig_ai_exit_signal(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_ai_exit_signal"))
    direction_penalty += _sig_ai_reversal_warning(out).astype(float) * abs(_cfg_float(cp, "short_scoring", "flag_ai_reversal_warning"))

    # sanitize
    score_base = _clip(score_base, -300.0, 300.0)
    score_trend = _clip(score_trend, -300.0, 300.0)
    score_momentum = _clip(score_momentum, -300.0, 300.0)
    score_velocity = _clip(score_velocity, -300.0, 300.0)
    direction_penalty = _clip(direction_penalty, 0.0, 300.0)

    # explicit detail columns
    out["score_base"] = score_base
    out["score_trend"] = score_trend
    out["score_momentum"] = score_momentum
    out["score_velocity"] = score_velocity
    out["direction_penalty"] = direction_penalty

    # short aliases
    out["base"] = out["score_base"]
    out["trend"] = out["score_trend"]
    out["mom"] = out["score_momentum"]
    out["vel"] = out["score_velocity"]
    out["pen"] = out["direction_penalty"]

    # bridge columns for score_calculator
    out["flag_score"] = _clip(out["score_base"] + out["score_trend"], -500.0, 500.0)
    out["momentum_score"] = _clip(out["score_momentum"], -500.0, 500.0)
    out["volume_score"] = _clip(out["score_velocity"], -500.0, 500.0)
    out["sell_pressure"] = _clip(out["direction_penalty"], 0.0, 500.0)

    # fallback helper columns for score_calculator
    if "absolute_score" not in out.columns:
        out["absolute_score"] = 0.0

    if "liquidity_score" not in out.columns:
        turnover = _volume(out) * _close(out)
        out["liquidity_score"] = _clip(turnover / 1.0e9, 0.0, 10.0)

    if "distribution_score" not in out.columns:
        out["distribution_score"] = 0.0

    if "volatility_score" not in out.columns:
        close = _close(out).replace(0, np.nan)
        vol = ((_high(out) - _low(out)) / close).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        atr = _atr(out)
        atr_bonus = (atr / close).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        out["volatility_score"] = _clip((vol * 10.0) + (atr_bonus * 5.0), 0.0, 20.0)

    logger.info(
        "[DETAIL SCORE] built rows=%s base_nonzero=%s trend_nonzero=%s momentum_nonzero=%s velocity_nonzero=%s penalty_nonzero=%s",
        len(out),
        int((pd.to_numeric(out["score_base"], errors="coerce").fillna(0.0) != 0).sum()),
        int((pd.to_numeric(out["score_trend"], errors="coerce").fillna(0.0) != 0).sum()),
        int((pd.to_numeric(out["score_momentum"], errors="coerce").fillna(0.0) != 0).sum()),
        int((pd.to_numeric(out["score_velocity"], errors="coerce").fillna(0.0) != 0).sum()),
        int((pd.to_numeric(out["direction_penalty"], errors="coerce").fillna(0.0) != 0).sum()),
    )

    return out