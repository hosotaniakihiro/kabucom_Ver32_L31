# ============================================================
# File   : trading/signals/conditions_buy.py
# Version: Ver2.0-PRODUCTION-FULL-COMPAT-HARDENED
# ------------------------------------------------------------
# ✔ 既存BUY条件完全保持（削除ゼロ）
# ✔ close/open と close_price/open_price 両対応
# ✔ dict / Series / DataFrame 全対応
# ✔ KeyError完全防止
# ✔ ma5 / ma25 / ma75 欠損時も安全
# ✔ recent_data列欠損時は False返却
# ✔ check_entry_conditions 互換維持
# ✔ 本番用完全版
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# COMMON SAFE HELPERS
# ============================================================

def _is_df(x) -> bool:
    return isinstance(x, pd.DataFrame)


def _is_series(x) -> bool:
    return isinstance(x, pd.Series)


def _is_mapping_like(x) -> bool:
    return isinstance(x, (dict, pd.Series))


def _to_series(row) -> pd.Series:
    if row is None:
        return pd.Series(dtype="object")
    if isinstance(row, pd.Series):
        return row.copy()
    if isinstance(row, dict):
        return pd.Series(row).copy()
    try:
        return pd.Series(dict(row)).copy()
    except Exception:
        return pd.Series(dtype="object")


def _num(v, default=0.0) -> float:
    try:
        if v is None:
            return default
        if pd.isna(v):
            return default
        if isinstance(v, (int, float)):
            return float(v)
        return float(str(v).replace(",", ""))
    except Exception:
        return default


def _has_value(v) -> bool:
    try:
        return v is not None and not pd.isna(v)
    except Exception:
        return v is not None


def _row_get(row, key: str, default=None):
    """
    row から key を取得。
    close/open/high/low と *_price の相互互換も吸収。
    """
    if row is None:
        return default

    s = _to_series(row)

    alias_map = {
        "open_price": ["open_price", "open"],
        "high_price": ["high_price", "high"],
        "low_price": ["low_price", "low"],
        "close_price": ["close_price", "close"],
        "open": ["open", "open_price"],
        "high": ["high", "high_price"],
        "low": ["low", "low_price"],
        "close": ["close", "close_price"],
    }

    candidates = alias_map.get(key, [key])

    for c in candidates:
        if c in s.index:
            v = s.get(c, default)
            if _has_value(v):
                return v

    return default


def _df_has_col(df: Optional[pd.DataFrame], col: str) -> bool:
    return isinstance(df, pd.DataFrame) and col in df.columns


def _df_get_series(df: Optional[pd.DataFrame], col: str) -> pd.Series:
    """
    DataFrame列取得。close/open/high/low と *_price の相互互換を吸収。
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.Series(dtype="float64")

    alias_map = {
        "open_price": ["open_price", "open"],
        "high_price": ["high_price", "high"],
        "low_price": ["low_price", "low"],
        "close_price": ["close_price", "close"],
        "open": ["open", "open_price"],
        "high": ["high", "high_price"],
        "low": ["low", "low_price"],
        "close": ["close", "close_price"],
    }

    candidates = alias_map.get(col, [col])

    for c in candidates:
        if c in df.columns:
            return df[c]

    return pd.Series(dtype="float64")


def _safe_tail(df: Optional[pd.DataFrame], n: int) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    try:
        return df.tail(n).copy()
    except Exception:
        return pd.DataFrame()


def _normalize_price_columns(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """
    open/high/low/close しかない場合は *_price を補完。
    *_price がある場合は元列も残す（削除ゼロ）。
    """
    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()

    df = df.copy()

    alias_pairs = [
        ("open", "open_price"),
        ("high", "high_price"),
        ("low", "low_price"),
        ("close", "close_price"),
    ]

    for src, dst in alias_pairs:
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]
        if dst in df.columns and src not in df.columns:
            df[src] = df[dst]

    return df


def _normalize_row(row):
    """
    dict / Series の OHLC 別名を双方向補完。
    """
    s = _to_series(row)

    if s.empty:
        return s

    alias_pairs = [
        ("open", "open_price"),
        ("high", "high_price"),
        ("low", "low_price"),
        ("close", "close_price"),
    ]

    for a, b in alias_pairs:
        if a in s.index and b not in s.index and _has_value(s.get(a)):
            s[b] = s[a]
        if b in s.index and a not in s.index and _has_value(s.get(b)):
            s[a] = s[b]

    return s


def _normalize_inputs(curr, prev, recent_data):
    curr = _normalize_row(curr)
    prev = _normalize_row(prev)
    recent_data = _normalize_price_columns(recent_data)
    return curr, prev, recent_data


def _all_row_values(row, keys) -> bool:
    for k in keys:
        if not _has_value(_row_get(row, k)):
            return False
    return True


def _series_notnull_all(s: pd.Series) -> bool:
    try:
        return len(s) > 0 and s.notna().all()
    except Exception:
        return False


# ============================================================
# TREND
# ============================================================

def cond_ma_uptrend(curr, prev, recent_data=None, prev_state=None):
    """MA5 > MA25 > MA75"""
    try:
        if _all_row_values(curr, ["ma5", "ma25", "ma75"]):
            if _num(_row_get(curr, "ma5")) > _num(_row_get(curr, "ma25")) > _num(_row_get(curr, "ma75")):
                return True, "ma_uptrend"
    except Exception as e:
        logger.error(f"❌ cond_ma_uptrend エラー: {e}", exc_info=True)
    return False, None


def cond_ma5_ma25_cross(curr, prev, recent_data=None, prev_state=None):
    """MA5とMA25のゴールデンクロス"""
    try:
        if _all_row_values(prev, ["ma5", "ma25"]) and _all_row_values(curr, ["ma5", "ma25"]):
            if _num(_row_get(prev, "ma5")) <= _num(_row_get(prev, "ma25")) and \
               _num(_row_get(curr, "ma5")) > _num(_row_get(curr, "ma25")):
                return True, "ma5_ma25_cross"
    except Exception as e:
        logger.error(f"❌ cond_ma5_ma25_cross エラー: {e}", exc_info=True)
    return False, None


def cond_perfect_order(curr, prev, recent_data=None, prev_state=None):
    """パーフェクトオーダー (MA5 > MA25 > MA75 が継続)"""
    try:
        last = _safe_tail(recent_data, 1)
        if not last.empty:
            row = last.iloc[0]
            if _all_row_values(row, ["ma5", "ma25", "ma75"]):
                if _num(_row_get(row, "ma5")) > _num(_row_get(row, "ma25")) > _num(_row_get(row, "ma75")):
                    return True, "perfect_order"
    except Exception as e:
        logger.error(f"❌ cond_perfect_order エラー: {e}", exc_info=True)
    return False, None


def cond_pullback_entry(curr, prev, recent_data=None, prev_state=None):
    """上昇トレンド中の押し目買い"""
    try:
        if _all_row_values(curr, ["close_price", "ma25"]):
            curr_close = _num(_row_get(curr, "close_price"))
            curr_ma25 = _num(_row_get(curr, "ma25"))
            touched = abs(curr_close - curr_ma25) < curr_ma25 * 0.01 if curr_ma25 > 0 else False

            rebounded = False
            if _all_row_values(prev, ["close_price", "ma25"]):
                prev_close = _num(_row_get(prev, "close_price"))
                prev_ma25 = _num(_row_get(prev, "ma25"))
                rebounded = prev_close < prev_ma25 and curr_close > curr_ma25

            if touched or rebounded:
                return True, "pullback_entry"
    except Exception as e:
        logger.error(f"❌ cond_pullback_entry エラー: {e}", exc_info=True)
    return False, None


# ============================================================
# MACD / RSI / RCI / STOCH
# ============================================================

def cond_macd_cross(curr, prev, recent_data=None, prev_state=None):
    """MACDゴールデンクロス"""
    try:
        if _all_row_values(prev, ["macd", "signal"]) and _all_row_values(curr, ["macd", "signal"]):
            if _num(_row_get(prev, "macd")) <= _num(_row_get(prev, "signal")) and \
               _num(_row_get(curr, "macd")) > _num(_row_get(curr, "signal")):
                return True, "macd_cross"
    except Exception as e:
        logger.error(f"❌ cond_macd_cross エラー: {e}", exc_info=True)
    return False, None


def cond_rsi_rebound(curr, prev, recent_data=None, prev_state=None):
    """RSI安値圏から反発"""
    try:
        if _all_row_values(prev, ["rsi"]) and _all_row_values(curr, ["rsi"]):
            if _num(_row_get(prev, "rsi")) < 30 and _num(_row_get(curr, "rsi")) > _num(_row_get(prev, "rsi")):
                return True, "rsi_rebound"
    except Exception as e:
        logger.error(f"❌ cond_rsi_rebound エラー: {e}", exc_info=True)
    return False, None


def cond_rci_trio_up(curr, prev, recent_data=None, prev_state=None):
    """RCIが3本連続上昇"""
    try:
        if _df_has_col(recent_data, "rci") and len(recent_data) >= 3:
            last_three = recent_data["rci"].tail(3).tolist()
            if all(pd.notnull(last_three)) and last_three[0] < last_three[1] < last_three[2]:
                return True, "rci_trio_up"
    except Exception as e:
        logger.error(f"❌ cond_rci_trio_up エラー: {e}", exc_info=True)
    return False, None


def cond_stoch_rebound(curr, prev, recent_data=None, prev_state=None):
    """ストキャスティクス反発"""
    try:
        if _all_row_values(prev, ["slowk", "slowd"]) and _all_row_values(curr, ["slowk", "slowd"]):
            if _num(_row_get(prev, "slowk")) < 20 and _num(_row_get(prev, "slowd")) < 20 and \
               _num(_row_get(curr, "slowk")) > _num(_row_get(curr, "slowd")):
                return True, "stoch_rebound"
    except Exception as e:
        logger.error(f"❌ cond_stoch_rebound エラー: {e}", exc_info=True)
    return False, None


# ============================================================
# BOLLINGER
# ============================================================

def cond_bollinger_rebound(curr, prev, recent_data=None, prev_state=None):
    """ボリンジャーバンド下限反発"""
    try:
        if _all_row_values(curr, ["close_price", "bb_lower"]):
            if _num(_row_get(curr, "close_price")) > _num(_row_get(curr, "bb_lower")):
                return True, "bollinger_rebound"
    except Exception as e:
        logger.error(f"❌ cond_bollinger_rebound エラー: {e}", exc_info=True)
    return False, None


def cond_bollinger_breakout(curr, prev, recent_data=None, prev_state=None):
    """ボリンジャーバンド上抜け"""
    try:
        if _all_row_values(curr, ["close_price", "bb_upper"]):
            if _num(_row_get(curr, "close_price")) > _num(_row_get(curr, "bb_upper")):
                return True, "bollinger_breakout"
    except Exception as e:
        logger.error(f"❌ cond_bollinger_breakout エラー: {e}", exc_info=True)
    return False, None


def cond_bb_3sigma_rebound(curr, prev, recent_data=None, prev_state=None):
    """ボリンジャーバンド -3σ からの反発"""
    try:
        if _all_row_values(curr, ["close_price", "bb_lower_3sigma"]):
            if _num(_row_get(curr, "close_price")) > _num(_row_get(curr, "bb_lower_3sigma")):
                return True, "bb_3sigma_rebound"
    except Exception as e:
        logger.error(f"❌ cond_bb_3sigma_rebound エラー: {e}", exc_info=True)
    return False, None


# ============================================================
# VWAP
# ============================================================

def cond_vwap_breakout(curr, prev, recent_data=None, prev_state=None):
    """VWAP超え"""
    try:
        if _all_row_values(curr, ["vwap", "close_price"]):
            if _num(_row_get(curr, "close_price")) > _num(_row_get(curr, "vwap")):
                return True, "vwap_breakout"
    except Exception as e:
        logger.error(f"❌ cond_vwap_breakout エラー: {e}", exc_info=True)
    return False, None


# ============================================================
# VOLUME
# ============================================================

def cond_volume_surge(curr, prev, recent_data=None, prev_state=None):
    """出来高急増"""
    try:
        if _all_row_values(curr, ["volume", "close_price"]) and _all_row_values(prev, ["close_price"]):
            vol_mean = 0.0
            if _df_has_col(recent_data, "volume") and len(recent_data) >= 4:
                vol_mean = pd.to_numeric(recent_data["volume"].iloc[-4:-1], errors="coerce").mean()

            if vol_mean > 0 and \
               _num(_row_get(curr, "volume")) > 2 * vol_mean and \
               _num(_row_get(curr, "close_price")) > _num(_row_get(prev, "close_price")):
                return True, "volume_surge"
    except Exception as e:
        logger.error(f"❌ cond_volume_surge エラー: {e}", exc_info=True)
    return False, None


def cond_gc_volume_boost(curr, prev, recent_data=None, prev_state=None):
    """ゴールデンクロス直後の出来高増加"""
    try:
        if _all_row_values(prev, ["ma5", "ma25"]) and _all_row_values(curr, ["ma5", "ma25", "volume"]):
            crossed = _num(_row_get(prev, "ma5")) <= _num(_row_get(prev, "ma25")) and \
                      _num(_row_get(curr, "ma5")) > _num(_row_get(curr, "ma25"))
            if crossed and _has_value(_row_get(prev, "volume")):
                if _num(_row_get(curr, "volume")) > _num(_row_get(prev, "volume")):
                    return True, "gc_volume_boost"
    except Exception as e:
        logger.error(f"❌ cond_gc_volume_boost エラー: {e}", exc_info=True)
    return False, None


# ============================================================
# CANDLE PATTERNS
# ============================================================

def cond_bullish_streak(curr, prev, recent_data=None, prev_state=None):
    """陽線3本 + MA5上昇 + 底値圏"""
    try:
        last3 = _safe_tail(recent_data, 3)
        if len(last3) < 3:
            return False, None

        close_s = pd.to_numeric(_df_get_series(last3, "close_price"), errors="coerce")
        open_s = pd.to_numeric(_df_get_series(last3, "open_price"), errors="coerce")
        ma5_s = pd.to_numeric(_df_get_series(last3, "ma5"), errors="coerce")

        if len(close_s) < 3 or len(open_s) < 3:
            return False, None

        if not _series_notnull_all(close_s) or not _series_notnull_all(open_s):
            return False, None

        bullish = bool((close_s > open_s).all())

        is_ma5_up = False
        if len(ma5_s) >= 2 and ma5_s.tail(2).notna().all():
            ma5_vals = ma5_s.tail(2)
            is_ma5_up = ma5_vals.iloc[1] > ma5_vals.iloc[0]

        low_all = pd.to_numeric(_df_get_series(recent_data, "low_price"), errors="coerce")
        high_all = pd.to_numeric(_df_get_series(recent_data, "high_price"), errors="coerce")

        if low_all.empty or high_all.empty:
            return False, None

        low_20 = low_all.tail(20).min()
        high_20 = high_all.tail(20).max()

        curr_close = _row_get(curr, "close_price")
        if not _has_value(curr_close):
            return False, None

        curr_close = _num(curr_close)
        zone_width = high_20 - low_20
        in_bottom_zone = False
        if pd.notna(low_20) and pd.notna(high_20):
            in_bottom_zone = curr_close <= low_20 + zone_width * 0.2

        if bullish and is_ma5_up and in_bottom_zone:
            return True, "bullish_streak"

    except Exception as e:
        logger.error(f"❌ cond_bullish_streak エラー: {e}", exc_info=True)

    return False, None


def cond_bullish_engulfing(curr, prev, recent_data=None, prev_state=None):
    """包み足（陽線による反転）"""
    try:
        if _all_row_values(prev, ["open_price", "close_price"]) and _all_row_values(curr, ["open_price", "close_price"]):
            if _num(_row_get(curr, "close_price")) > _num(_row_get(curr, "open_price")) and \
               _num(_row_get(curr, "open_price")) < _num(_row_get(prev, "close_price")):
                return True, "bullish_engulfing"
    except Exception as e:
        logger.error(f"❌ cond_bullish_engulfing エラー: {e}", exc_info=True)
    return False, None


def cond_engulfing_reversal(curr, prev, recent_data=None, prev_state=None):
    """包み足リバーサル"""
    try:
        if _all_row_values(prev, ["open_price", "close_price"]) and _all_row_values(curr, ["close_price"]):
            if _num(_row_get(prev, "close_price")) < _num(_row_get(prev, "open_price")) and \
               _num(_row_get(curr, "close_price")) > _num(_row_get(prev, "open_price")):
                return True, "engulfing_reversal"
    except Exception as e:
        logger.error(f"❌ cond_engulfing_reversal エラー: {e}", exc_info=True)
    return False, None


def cond_bull_candle_volume(curr, prev, recent_data=None, prev_state=None):
    """大陽線 + 出来高急増"""
    try:
        if _all_row_values(curr, ["open_price", "close_price", "volume"]):
            body = abs(_num(_row_get(curr, "close_price")) - _num(_row_get(curr, "open_price")))
            if _num(_row_get(curr, "close_price")) > _num(_row_get(curr, "open_price")) and body > 0:
                if _df_has_col(recent_data, "volume"):
                    vol_mean = pd.to_numeric(recent_data["volume"], errors="coerce").mean()
                    if vol_mean > 0 and _num(_row_get(curr, "volume")) > vol_mean * 2:
                        return True, "bull_candle_volume"
    except Exception as e:
        logger.error(f"❌ cond_bull_candle_volume エラー: {e}", exc_info=True)
    return False, None


def cond_fib_rebound(curr, prev, recent_data=None, prev_state=None):
    """フィボナッチ押し目からの反発"""
    try:
        if isinstance(recent_data, pd.DataFrame) and len(recent_data) >= 20:
            fib_data = recent_data.tail(20)
            high = pd.to_numeric(_df_get_series(fib_data, "high_price"), errors="coerce").max()
            low = pd.to_numeric(_df_get_series(fib_data, "low_price"), errors="coerce").min()
            diff = high - low

            if diff > 0 and _all_row_values(prev, ["close_price"]) and _all_row_values(curr, ["close_price"]):
                fib_50 = high - diff * 0.5
                if _num(_row_get(prev, "close_price")) < fib_50 < _num(_row_get(curr, "close_price")):
                    return True, "fib_rebound"
    except Exception as e:
        logger.error(f"❌ cond_fib_rebound エラー: {e}", exc_info=True)
    return False, None


def cond_tick_surge(curr, prev, recent_data=None, prev_state=None):
    """ティック回数急増"""
    try:
        if _all_row_values(curr, ["tick_count"]) and _df_has_col(recent_data, "tick_count") and len(recent_data) >= 5:
            tick_avg = pd.to_numeric(recent_data["tick_count"].tail(5), errors="coerce").mean()
            if tick_avg > 0 and _num(_row_get(curr, "tick_count")) > tick_avg * 2:
                return True, "tick_surge"
    except Exception as e:
        logger.error(f"❌ cond_tick_surge エラー: {e}", exc_info=True)
    return False, None


def cond_lower_wick_low_zone(curr, prev, recent_data=None, prev_state=None):
    """安値圏の下ヒゲ陽線"""
    try:
        if _all_row_values(curr, ["open_price", "close_price", "low_price"]):
            open_p = _num(_row_get(curr, "open_price"))
            close_p = _num(_row_get(curr, "close_price"))
            low_p = _num(_row_get(curr, "low_price"))

            body = abs(close_p - open_p)
            lower_wick = min(open_p, close_p) - low_p

            high_20 = pd.to_numeric(_df_get_series(recent_data, "high_price"), errors="coerce").tail(20).max()
            low_20 = pd.to_numeric(_df_get_series(recent_data, "low_price"), errors="coerce").tail(20).min()

            in_low_zone = False
            if pd.notna(high_20) and pd.notna(low_20):
                in_low_zone = close_p <= low_20 + (high_20 - low_20) * 0.2

            if close_p > open_p and lower_wick > body * 1.5 and in_low_zone:
                return True, "lower_wick_low_zone"
    except Exception as e:
        logger.error(f"❌ cond_lower_wick_low_zone エラー: {e}", exc_info=True)
    return False, None


def cond_reversal_penalty(curr, prev, recent_data=None, prev_state=None):
    """反落ペナルティ"""
    try:
        if _all_row_values(prev, ["close_price", "ma25"]) and _all_row_values(curr, ["close_price", "ma25"]):
            if _num(_row_get(prev, "close_price")) >= _num(_row_get(prev, "ma25")) and \
               _num(_row_get(curr, "close_price")) < _num(_row_get(curr, "ma25")):
                return True, "reversal_penalty"
    except Exception as e:
        logger.error(f"❌ cond_reversal_penalty エラー: {e}", exc_info=True)
    return False, None


def cond_ma5_downtrend(curr, prev, recent_data=None, prev_state=None):
    """MA5下降トレンド"""
    try:
        ma5_s = pd.to_numeric(_df_get_series(recent_data, "ma5"), errors="coerce")
        if len(ma5_s) >= 3:
            vals = ma5_s.tail(3).tolist()
            if all(pd.notnull(vals)) and vals[0] > vals[1] > vals[2]:
                return True, "ma5_downtrend"
    except Exception as e:
        logger.error(f"❌ cond_ma5_downtrend エラー: {e}", exc_info=True)
    return False, None


def cond_ma5_below_ma25(curr, prev, recent_data=None, prev_state=None):
    """MA5がMA25を下回る"""
    try:
        if _all_row_values(curr, ["ma5", "ma25"]):
            if _num(_row_get(curr, "ma5")) < _num(_row_get(curr, "ma25")):
                return True, "ma5_below_ma25"
    except Exception as e:
        logger.error(f"❌ cond_ma5_below_ma25 エラー: {e}", exc_info=True)
    return False, None


def cond_ma_reversal_after_touch(curr, prev, recent_data=None, prev_state=None):
    """MAに接触後の反落"""
    try:
        if _all_row_values(prev, ["close_price", "ma25"]) and _all_row_values(curr, ["close_price"]):
            prev_close = _num(_row_get(prev, "close_price"))
            prev_ma25 = _num(_row_get(prev, "ma25"))
            touched = abs(prev_close - prev_ma25) < prev_ma25 * 0.01 if prev_ma25 > 0 else False
            if touched and _num(_row_get(curr, "close_price")) < prev_close:
                return True, "ma_reversal_after_touch"
    except Exception as e:
        logger.error(f"❌ cond_ma_reversal_after_touch エラー: {e}", exc_info=True)
    return False, None


def cond_rci9_uptrend(curr, prev, recent_data=None, prev_state=None):
    """RCI9 上昇中"""
    try:
        if _df_has_col(recent_data, "rci") and len(recent_data) >= 3:
            last_three = recent_data["rci"].tail(3).tolist()
            if all(pd.notnull(last_three)) and last_three[0] < last_three[1] < last_three[2]:
                return True, "rci9_uptrend"
    except Exception as e:
        logger.error(f"❌ cond_rci9_uptrend エラー: {e}", exc_info=True)
    return False, None


# ============================================================
# BUY 条件ラッパー
# ============================================================

def check_entry_conditions(curr, prev, recent_data=None, prev_state=None):
    """
    buy.py / summary / ENTRY 互換用ラッパー
    """
    curr, prev, recent_data = _normalize_inputs(curr, prev, recent_data)

    hits = []

    for fn in conditions_buy:
        try:
            ok, reason = fn(curr, prev, recent_data, prev_state)
            if ok and reason:
                hits.append(reason)
        except Exception as e:
            logger.error(f"❌ BUY condition error: {fn.__name__} {e}", exc_info=True)

    return hits


# ============================================================
# BUY条件リスト
# ============================================================

conditions_buy = [
    cond_ma_uptrend,
    cond_ma5_ma25_cross,
    cond_perfect_order,
    cond_pullback_entry,
    cond_macd_cross,
    cond_rsi_rebound,
    cond_rci_trio_up,
    cond_stoch_rebound,
    cond_bollinger_rebound,
    cond_bollinger_breakout,
    cond_bb_3sigma_rebound,
    cond_vwap_breakout,
    cond_volume_surge,
    cond_gc_volume_boost,
    cond_bullish_streak,
    cond_bullish_engulfing,
    cond_engulfing_reversal,
    cond_bull_candle_volume,
    cond_fib_rebound,
    cond_tick_surge,
    cond_lower_wick_low_zone,
    cond_reversal_penalty,
    cond_ma5_downtrend,
    cond_ma5_below_ma25,
    cond_ma_reversal_after_touch,
    cond_rci9_uptrend,
]