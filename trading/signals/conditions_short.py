# ============================================================
# File   : trading/signals/conditions_short.py
# Version: Ver2.0-PRODUCTION-FULL-COMPAT-HARDENED
# ------------------------------------------------------------
# ✔ SELL（SHORT）条件完全保持（削除ゼロ）
# ✔ 1 condition = 1 reason 維持
# ✔ BUY 側 hardening と対称化
# ✔ close/open と close_price/open_price 両対応
# ✔ dict / Series / DataFrame 全対応
# ✔ KeyError完全防止
# ✔ 指標欠損時も安全に False
# ✔ check_short_conditions 互換維持
# ✔ 本番用完全版
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# COMMON SAFE HELPERS
# ============================================================

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


def _has_value(v) -> bool:
    try:
        return v is not None and not pd.isna(v)
    except Exception:
        return v is not None


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


def _row_get(row, key: str, default=None):
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


def _all_row_values(row, keys) -> bool:
    for k in keys:
        if not _has_value(_row_get(row, k)):
            return False
    return True


def _normalize_row(row):
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


def _normalize_price_columns(df):
    if df is None or not isinstance(df, pd.DataFrame):
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


def _normalize_inputs(curr, prev, recent):
    curr = _normalize_row(curr)
    prev = _normalize_row(prev)
    recent = _normalize_price_columns(recent)
    return curr, prev, recent


def _df_get_series(df: Optional[pd.DataFrame], col: str) -> pd.Series:
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


def _series_notnull_all(s: pd.Series) -> bool:
    try:
        return len(s) > 0 and s.notna().all()
    except Exception:
        return False


def _is_bull(r):
    return _num(_row_get(r, "close_price")) > _num(_row_get(r, "open_price"))


def _is_bear(r):
    return _num(_row_get(r, "close_price")) < _num(_row_get(r, "open_price"))


def _body(r):
    return abs(_num(_row_get(r, "close_price")) - _num(_row_get(r, "open_price")))


def _range(r):
    return _num(_row_get(r, "high_price")) - _num(_row_get(r, "low_price"))


# ============================================================
# direction / MA
# ============================================================

def cond_dir_down(curr, prev, recent, *_):
    try:
        if bool(_row_get(curr, "dir_down", False)):
            return True, "dir_down"
    except Exception:
        logger.exception("cond_dir_down")
    return False, None


def cond_ma_alignment_down(curr, prev, recent, *_):
    try:
        if bool(_row_get(curr, "ma_alignment_down", False)):
            return True, "ma_alignment_down"
    except Exception:
        logger.exception("cond_ma_alignment_down")
    return False, None


def cond_ma5_downtrend(curr, prev, recent, *_):
    try:
        if _all_row_values(prev, ["ma5"]) and _all_row_values(curr, ["ma5"]):
            if _num(_row_get(curr, "ma5")) < _num(_row_get(prev, "ma5")):
                return True, "ma5_downtrend"
    except Exception:
        logger.exception("cond_ma5_downtrend")
    return False, None


def cond_ma5_below_ma25(curr, prev, recent, *_):
    try:
        if _all_row_values(curr, ["ma5", "ma25"]):
            if _num(_row_get(curr, "ma5")) < _num(_row_get(curr, "ma25")):
                return True, "ma5_below_ma25"
    except Exception:
        logger.exception("cond_ma5_below_ma25")
    return False, None


def cond_perfect_order_down(curr, prev, recent, *_):
    try:
        if bool(_row_get(prev, "ma_alignment_down", False)) and bool(_row_get(curr, "ma_alignment_down", False)):
            return True, "perfect_order_down"
    except Exception:
        logger.exception("cond_perfect_order_down")
    return False, None


# ============================================================
# momentum failure
# ============================================================

def cond_macd_dc(curr, prev, recent, *_):
    try:
        if bool(_row_get(curr, "macd_dc", False)):
            return True, "macd_dc"
    except Exception:
        logger.exception("cond_macd_dc")
    return False, None


def cond_rsi_falling(curr, prev, recent, *_):
    try:
        if _all_row_values(prev, ["rsi"]) and _all_row_values(curr, ["rsi"]):
            if _num(_row_get(curr, "rsi")) < _num(_row_get(prev, "rsi")):
                return True, "rsi_falling"
    except Exception:
        logger.exception("cond_rsi_falling")
    return False, None


# ============================================================
# price / volume
# ============================================================

def cond_below_ma75(curr, prev, recent, *_):
    try:
        if _all_row_values(curr, ["ma75", "close_price"]):
            if _num(_row_get(curr, "close_price")) < _num(_row_get(curr, "ma75")):
                return True, "below_ma75"
    except Exception:
        logger.exception("cond_below_ma75")
    return False, None


def cond_vwap_fail(curr, prev, recent, *_):
    try:
        if _all_row_values(curr, ["close_price", "vwap"]):
            if _num(_row_get(curr, "close_price")) < _num(_row_get(curr, "vwap")):
                return True, "vwap_fail"
    except Exception:
        logger.exception("cond_vwap_fail")
    return False, None


def cond_volume_drop(curr, prev, recent, *_):
    try:
        vol_s = pd.to_numeric(_df_get_series(recent, "volume"), errors="coerce")
        if len(vol_s) >= 5 and _all_row_values(curr, ["volume"]):
            avg = vol_s.iloc[-6:-1].mean()
            if pd.notna(avg) and avg > 0 and _num(_row_get(curr, "volume")) < avg * 0.5:
                return True, "volume_drop"
    except Exception:
        logger.exception("cond_volume_drop")
    return False, None


def cond_volume_peak_out(curr, prev, recent, *_):
    try:
        if _all_row_values(prev, ["volume"]) and _all_row_values(curr, ["volume"]):
            if _num(_row_get(curr, "volume")) < _num(_row_get(prev, "volume")) * 0.5:
                return True, "volume_peak_out"
    except Exception:
        logger.exception("cond_volume_peak_out")
    return False, None


def cond_volume_price_breakdown(curr, prev, recent, *_):
    try:
        vol_s = pd.to_numeric(_df_get_series(recent, "volume"), errors="coerce")
        if _all_row_values(prev, ["close_price"]) and _all_row_values(curr, ["close_price", "volume"]):
            avg = vol_s.iloc[-6:-1].mean() if len(vol_s) >= 2 else 0
            if avg > 0 and \
               _num(_row_get(curr, "close_price")) < _num(_row_get(prev, "close_price")) and \
               _num(_row_get(curr, "volume")) > avg * 1.5:
                return True, "volume_price_breakdown"
    except Exception:
        logger.exception("cond_volume_price_breakdown")
    return False, None


def cond_volume_zone_breakdown(curr, prev, recent, *_):
    try:
        low_s = pd.to_numeric(_df_get_series(recent, "low_price"), errors="coerce")
        if len(low_s) >= 30 and _all_row_values(curr, ["close_price"]):
            zone = low_s.tail(30).quantile(0.3)
            if pd.notna(zone) and _num(_row_get(curr, "close_price")) < zone:
                return True, "volume_zone_breakdown"
    except Exception:
        logger.exception("cond_volume_zone_breakdown")
    return False, None


# ============================================================
# reversal / pullback failure
# ============================================================

def cond_reversal_penalty(curr, prev, recent, *_):
    try:
        if prev is not None and curr is not None and _is_bull(prev) and _is_bear(curr):
            return True, "reversal_penalty"
    except Exception:
        logger.exception("cond_reversal_penalty")
    return False, None


def cond_fib_reversal(curr, prev, recent, *_):
    try:
        high_s = pd.to_numeric(_df_get_series(recent, "high_price"), errors="coerce")
        low_s = pd.to_numeric(_df_get_series(recent, "low_price"), errors="coerce")

        if len(high_s) >= 20 and len(low_s) >= 20 and _all_row_values(prev, ["close_price"]) and _all_row_values(curr, ["close_price"]):
            hi = high_s.tail(20).max()
            lo = low_s.tail(20).min()
            diff = hi - lo
            if pd.notna(hi) and pd.notna(lo) and diff > 0:
                fib50 = hi - diff * 0.5
                if _num(_row_get(prev, "close_price")) > fib50 > _num(_row_get(curr, "close_price")):
                    return True, "fib_reversal"
    except Exception:
        logger.exception("cond_fib_reversal")
    return False, None


def cond_pullback_entry_down(curr, prev, recent, *_):
    try:
        if _all_row_values(curr, ["ma5", "ma25", "close_price"]):
            if _num(_row_get(curr, "ma5")) < _num(_row_get(curr, "ma25")):
                ma25 = _num(_row_get(curr, "ma25"))
                touched = abs(_num(_row_get(curr, "close_price")) - ma25) / ma25 < 0.01 if ma25 > 0 else False
                if touched:
                    return True, "pullback_entry_down"
    except Exception:
        logger.exception("cond_pullback_entry_down")
    return False, None


def cond_ma_reversal_after_touch_down(curr, prev, recent, *_):
    try:
        if _all_row_values(prev, ["close_price", "ma25"]) and _all_row_values(curr, ["close_price"]):
            prev_close = _num(_row_get(prev, "close_price"))
            prev_ma25 = _num(_row_get(prev, "ma25"))
            touched = abs(prev_close - prev_ma25) / prev_ma25 < 0.01 if prev_ma25 > 0 else False
            if touched and _num(_row_get(curr, "close_price")) < prev_close:
                return True, "ma_reversal_after_touch_down"
    except Exception:
        logger.exception("cond_ma_reversal_after_touch_down")
    return False, None


# ============================================================
# breakdown
# ============================================================

def cond_breakdown_3(curr, prev, recent, *_):
    try:
        last3 = _safe_tail(recent, 3)
        low_s = pd.to_numeric(_df_get_series(last3, "low_price"), errors="coerce")
        if len(low_s) >= 3 and _all_row_values(curr, ["close_price"]):
            if _num(_row_get(curr, "close_price")) < low_s.min():
                return True, "breakdown_3"
    except Exception:
        logger.exception("cond_breakdown_3")
    return False, None


def cond_gap_down_breakdown(curr, prev, *_):
    try:
        if _all_row_values(prev, ["low_price"]) and _all_row_values(curr, ["open_price"]):
            if _num(_row_get(curr, "open_price")) < _num(_row_get(prev, "low_price")):
                return True, "gap_down_breakdown"
    except Exception:
        logger.exception("cond_gap_down_breakdown")
    return False, None


def cond_bollinger_breakdown(curr, prev, recent, *_):
    try:
        if _all_row_values(curr, ["close_price", "bb_lower"]):
            if _num(_row_get(curr, "close_price")) < _num(_row_get(curr, "bb_lower")):
                return True, "bollinger_breakdown"
    except Exception:
        logger.exception("cond_bollinger_breakdown")
    return False, None


def cond_bb_3sigma_breakdown(curr, prev, recent, *_):
    try:
        if _all_row_values(curr, ["close_price", "bb_lower_3sigma"]):
            if _num(_row_get(curr, "close_price")) < _num(_row_get(curr, "bb_lower_3sigma")):
                return True, "bb_3sigma_breakdown"
    except Exception:
        logger.exception("cond_bb_3sigma_breakdown")
    return False, None


# ============================================================
# candle patterns（SELL）
# ============================================================

def cond_bearish_engulfing(curr, prev, *_):
    try:
        if prev is not None and curr is not None and _is_bull(prev) and _is_bear(curr):
            if _num(_row_get(curr, "open_price")) > _num(_row_get(prev, "close_price")) and \
               _num(_row_get(curr, "close_price")) < _num(_row_get(prev, "open_price")):
                return True, "bearish_engulfing"
    except Exception:
        logger.exception("cond_bearish_engulfing")
    return False, None


def cond_bearish_engulfing2(curr, prev, *_):
    try:
        if prev is not None and curr is not None and _is_bull(prev) and _is_bear(curr):
            if _body(curr) > _body(prev) * 1.2:
                return True, "bearish_engulfing2"
    except Exception:
        logger.exception("cond_bearish_engulfing2")
    return False, None


def cond_dark_cloud_cover(curr, prev, *_):
    try:
        if prev is not None and curr is not None and _is_bull(prev) and _is_bear(curr):
            mid = (_num(_row_get(prev, "open_price")) + _num(_row_get(prev, "close_price"))) / 2
            if _num(_row_get(curr, "close_price")) < mid:
                return True, "dark_cloud_cover"
    except Exception:
        logger.exception("cond_dark_cloud_cover")
    return False, None


def cond_evening_star(curr, prev, recent, *_):
    try:
        last3 = _safe_tail(recent, 3)
        if len(last3) >= 3:
            a = _normalize_row(last3.iloc[0])
            b = _normalize_row(last3.iloc[1])
            c = _normalize_row(last3.iloc[2])
            if _is_bull(a) and _body(b) < _body(a) * 0.5 and _is_bear(c):
                return True, "evening_star"
    except Exception:
        logger.exception("cond_evening_star")
    return False, None


def cond_shooting_star(curr, *_):
    try:
        if _all_row_values(curr, ["open_price", "close_price", "high_price"]):
            upper = _num(_row_get(curr, "high_price")) - max(
                _num(_row_get(curr, "open_price")),
                _num(_row_get(curr, "close_price")),
            )
            body = _body(curr)
            if upper > body * 2:
                return True, "shooting_star"
    except Exception:
        logger.exception("cond_shooting_star")
    return False, None


def cond_three_black_crows(curr, prev, recent, *_):
    try:
        last3 = _safe_tail(recent, 3)
        if len(last3) >= 3:
            rows = [_normalize_row(last3.iloc[i]) for i in range(3)]
            if all(_is_bear(r) for r in rows):
                return True, "three_black_crows"
    except Exception:
        logger.exception("cond_three_black_crows")
    return False, None


def cond_hanging_man(curr, *_):
    try:
        if _all_row_values(curr, ["open_price", "close_price", "low_price"]):
            lower = min(
                _num(_row_get(curr, "open_price")),
                _num(_row_get(curr, "close_price")),
            ) - _num(_row_get(curr, "low_price"))
            if lower > _body(curr) * 2:
                return True, "hanging_man"
    except Exception:
        logger.exception("cond_hanging_man")
    return False, None


def cond_bearish_harami(curr, prev, *_):
    try:
        if prev is not None and curr is not None and _is_bull(prev) and _is_bear(curr):
            if _num(_row_get(curr, "high_price")) < _num(_row_get(prev, "high_price")) and \
               _num(_row_get(curr, "low_price")) > _num(_row_get(prev, "low_price")):
                return True, "bearish_harami"
    except Exception:
        logger.exception("cond_bearish_harami")
    return False, None


def cond_bearish_doji_star(curr, *_):
    try:
        if _all_row_values(curr, ["open_price", "close_price"]):
            close_p = _num(_row_get(curr, "close_price"))
            if close_p != 0:
                if abs(_num(_row_get(curr, "open_price")) - close_p) / abs(close_p) < 0.001:
                    return True, "bearish_doji_star"
    except Exception:
        logger.exception("cond_bearish_doji_star")
    return False, None


def cond_bearish_breakaway(curr, prev, recent, *_):
    try:
        last5 = _safe_tail(recent, 5)
        if len(last5) >= 5:
            first = _normalize_row(last5.iloc[0])
            if _is_bull(first) and _is_bear(curr):
                return True, "bearish_breakaway"
    except Exception:
        logger.exception("cond_bearish_breakaway")
    return False, None


def cond_window_down(curr, prev, *_):
    try:
        if _all_row_values(prev, ["low_price"]) and _all_row_values(curr, ["high_price"]):
            if _num(_row_get(curr, "high_price")) < _num(_row_get(prev, "low_price")):
                return True, "window_down"
    except Exception:
        logger.exception("cond_window_down")
    return False, None


def cond_gapdown_red(curr, prev, *_):
    try:
        if _all_row_values(prev, ["low_price"]) and _all_row_values(curr, ["open_price"]):
            if _num(_row_get(curr, "open_price")) < _num(_row_get(prev, "low_price")) and _is_bear(curr):
                return True, "gapdown_red"
    except Exception:
        logger.exception("cond_gapdown_red")
    return False, None


# ============================================================
# absolute
# ============================================================

def cond_rsi_overbought_70(curr, *_):
    try:
        if _all_row_values(curr, ["rsi"]):
            if _num(_row_get(curr, "rsi")) >= 70:
                return True, "rsi_overbought_70"
    except Exception:
        logger.exception("cond_rsi_overbought_70")
    return False, None


def cond_bb_upper_touch(curr, *_):
    try:
        if _all_row_values(curr, ["high_price", "bb_upper"]):
            if _num(_row_get(curr, "high_price")) >= _num(_row_get(curr, "bb_upper")):
                return True, "bb_upper_touch"
    except Exception:
        logger.exception("cond_bb_upper_touch")
    return False, None


# ============================================================
# SHORT 条件ラッパー
# ============================================================

def check_short_conditions(curr, prev, recent=None, prev_state=None):
    curr, prev, recent = _normalize_inputs(curr, prev, recent)

    hits = []

    for fn in conditions_short:
        try:
            ok, reason = fn(curr, prev, recent, prev_state)
            if ok and reason:
                hits.append(reason)
        except Exception as e:
            logger.error(f"❌ SHORT condition error: {fn.__name__} {e}", exc_info=True)

    return hits


# ============================================================
# SELL 条件一覧
# ============================================================

conditions_short = [
    # direction
    cond_dir_down,
    cond_ma_alignment_down,

    # MA failure
    cond_ma5_downtrend,
    cond_ma5_below_ma25,
    cond_perfect_order_down,

    # momentum failure
    cond_macd_dc,
    cond_rsi_falling,

    # price / volume
    cond_below_ma75,
    cond_vwap_fail,
    cond_volume_drop,
    cond_volume_peak_out,
    cond_volume_price_breakdown,
    cond_volume_zone_breakdown,

    # reversal
    cond_reversal_penalty,
    cond_fib_reversal,
    cond_pullback_entry_down,
    cond_ma_reversal_after_touch_down,

    # breakdown
    cond_breakdown_3,
    cond_gap_down_breakdown,
    cond_bollinger_breakdown,
    cond_bb_3sigma_breakdown,

    # candle patterns
    cond_bearish_engulfing,
    cond_bearish_engulfing2,
    cond_dark_cloud_cover,
    cond_evening_star,
    cond_shooting_star,
    cond_three_black_crows,
    cond_hanging_man,
    cond_bearish_harami,
    cond_bearish_doji_star,
    cond_bearish_breakaway,
    cond_window_down,
    cond_gapdown_red,

    # absolute
    cond_rsi_overbought_70,
    cond_bb_upper_touch,
]