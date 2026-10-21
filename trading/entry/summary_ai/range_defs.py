# ============================================================
# File   : trading/entry/summary_ai/range_defs.py
# Version: V1.1-RANGE-DEFINITIONS-PERCENT-UNIT
# ------------------------------------------------------------
# range の定義をシステム内で 1 箇所に集約する。
#
# 【なぜ必要か】
# 2026-08-31 のログでは strict prefilter が 67/68 件を
#   reason=low_move_range_too_small range_pct=0.0 high=1705 low=1705 close=1705
# で落としていた。調査すると各モジュールが別々に range を作っており、
# 意味が混ざっていた:
#
#   executor._pick_high/_pick_low
#       ("high","high_price","HighPrice","day_high") を順に探す
#       -> 1 本足の high が無いと **当日高値** が入ってくる。
#          「その足で動いたか」を見たいのに当日値幅で判定してしまう。
#
#   current_df_context.py:121-122
#       high も low も見つからないと opening_price / open へフォールバック
#       -> high == low == open となり range=0。
#          欠損を「値動きゼロ」に化かしている。
#
#   candidates._repair_range_pct_columns
#       day_high/day_low から range_pct / intraday_range_pct / day_range_pct
#       を同じ値で埋める -> 3 つの名前が同じ意味になる。
#
# 【用途別の定義】
#   intrabar_range_pct  その 1 本の high-low。同じ足の high/low のみ使う。
#                       day_high/day_low/open/close で代用しない。
#   day_range_pct       当日高値-当日安値。day_high/day_low のみ使う。
#   lookback_range_pct  直近 N 本の max(high)-min(low)。履歴が要る。
#
# 【欠損の扱い】
#   必要な入力が無ければ 0.0 ではなく RANGE_DATA_NOT_READY を返す。
#   close で埋めて range=0 にすることは禁止 (それは「動いていない」ではなく
#   「測れていない」)。呼び出し側は DATA_NOT_READY として fail-closed する。
#
# 【単位 (2026-09-01 に統一)】
#   *_range_pct はすべて **百分率 (percent)** で返す。
#       value = (high - low) / reference_price * 100
#   1.88% は 1.88 であって 0.0188 ではない。
#
#   これ以前は range_defs が比率 (0.0188) を返す一方、
#   executor の blowoff ガードは閾値 2.5 を「2.5%」として比べており、
#   0.0188 >= 2.5 が成立しないためガードが一度も発火していなかった。
#   executor._blowoff_range_pct_value には
#       raw * 100.0 if raw <= 0.5 else raw
#   という推測変換まで入っていて、0.4 が「0.4%」なのか「40%」なのか
#   区別できなくなっていた。列名・計算・閾値・ログをすべて percent に揃える。
#
#   PERCENT_SCALE を掛ける場所はこのモジュールの計算式だけ。
#   呼び出し側で再度 100 倍しないこと。
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

VERSION = "V1.0-RANGE-DEFINITIONS"

# range の状態
RANGE_OK = "OK"
RANGE_DATA_NOT_READY = "RANGE_DATA_NOT_READY"

# すべての *_range_pct は percent (0-100)。比率で返さない。
PERCENT_SCALE = 100.0
UNIT = "percent"

# 用途
KIND_INTRABAR = "intrabar_range_pct"
KIND_DAY = "day_range_pct"
KIND_LOOKBACK = "lookback_range_pct"

# 各用途で参照してよいキー。混ぜないこと。
_INTRABAR_HIGH_KEYS = ("high", "high_price", "HighPrice", "bar_high")
_INTRABAR_LOW_KEYS = ("low", "low_price", "LowPrice", "bar_low")
_DAY_HIGH_KEYS = ("day_high", "today_high", "intraday_high", "session_high", "high_price_day", "range_high")
_DAY_LOW_KEYS = ("day_low", "today_low", "intraday_low", "session_low", "low_price_day", "range_low")
_CLOSE_KEYS = ("close_price", "close", "price", "current_price", "last_price", "CurrentPrice")

# 「その足の値幅」として既に計算済みの列 (あれば優先)
_INTRABAR_PCT_KEYS = ("intrabar_range_pct", "_intrabar_range_pct", "bar_range_pct")
_DAY_PCT_KEYS = ("day_range_pct", "intraday_range_pct")


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(str(v).strip().replace(",", "").replace("%", ""))
    except Exception:
        return None
    if f != f:  # NaN
        return None
    return f


def _dict_sources(item: Any) -> Tuple[Dict[str, Any], ...]:
    out = []
    if isinstance(item, dict):
        out.append(item)
        for k in ("ai_row", "source_row"):
            sub = item.get(k)
            if isinstance(sub, dict):
                out.append(sub)
    return tuple(out)


def _pick(item: Any, keys: Sequence[str]) -> Optional[float]:
    """positive な値だけ採用する。0/負/欠損は「無い」とみなす。"""
    for d in _dict_sources(item):
        for k in keys:
            if k not in d:
                continue
            f = _to_float(d.get(k))
            if f is not None and f > 0:
                return f
    return None


def _pick_any(item: Any, keys: Sequence[str]) -> Optional[float]:
    """0 も有効値として採用する (既計算の pct 列用)。"""
    for d in _dict_sources(item):
        for k in keys:
            if k not in d:
                continue
            f = _to_float(d.get(k))
            if f is not None:
                return f
    return None


def compute_intrabar_range_pct(item: Any) -> Dict[str, Any]:
    """その 1 本の値幅。day_high/day_low は絶対に使わない。"""
    pre = _pick_any(item, _INTRABAR_PCT_KEYS)
    if pre is not None and pre > 0:
        return {"kind": KIND_INTRABAR, "state": RANGE_OK, "value": abs(pre), "unit": UNIT, "source": "precomputed"}

    high = _pick(item, _INTRABAR_HIGH_KEYS)
    low = _pick(item, _INTRABAR_LOW_KEYS)
    close = _pick(item, _CLOSE_KEYS)
    if high is None or low is None or close is None:
        return {
            "kind": KIND_INTRABAR, "state": RANGE_DATA_NOT_READY, "value": None,
            "source": "bar_high_low", "reason": "high_low_or_close_missing",
            "high": high, "low": low, "close": close,
        }
    if high < low:
        return {
            "kind": KIND_INTRABAR, "state": RANGE_DATA_NOT_READY, "value": None,
            "source": "bar_high_low", "reason": "high_lt_low", "high": high, "low": low, "close": close,
        }
    return {
        "kind": KIND_INTRABAR, "state": RANGE_OK,
        "value": abs(high - low) / close * PERCENT_SCALE,
        "unit": UNIT,
        "source": "bar_high_low", "high": high, "low": low, "close": close,
    }


def compute_day_range_pct(item: Any) -> Dict[str, Any]:
    """当日高値-当日安値。1 本足の high/low では代用しない。"""
    pre = _pick_any(item, _DAY_PCT_KEYS)
    if pre is not None and pre > 0:
        return {"kind": KIND_DAY, "state": RANGE_OK, "value": abs(pre), "unit": UNIT, "source": "precomputed"}

    high = _pick(item, _DAY_HIGH_KEYS)
    low = _pick(item, _DAY_LOW_KEYS)
    close = _pick(item, _CLOSE_KEYS)
    if high is None or low is None or close is None:
        return {
            "kind": KIND_DAY, "state": RANGE_DATA_NOT_READY, "value": None,
            "source": "day_high_low", "reason": "day_high_low_or_close_missing",
            "day_high": high, "day_low": low, "close": close,
        }
    return {
        "kind": KIND_DAY, "state": RANGE_OK,
        "value": abs(high - low) / close * PERCENT_SCALE,
        "unit": UNIT,
        "source": "day_high_low", "day_high": high, "day_low": low, "close": close,
    }


def compute_lookback_range_pct(highs: Sequence[Any], lows: Sequence[Any], close: Any,
                               *, min_bars: int = 2) -> Dict[str, Any]:
    """直近 N 本の max(high)-min(low)。履歴が足りなければ DATA_NOT_READY。"""
    hs = [f for f in (_to_float(x) for x in (highs or [])) if f is not None and f > 0]
    ls = [f for f in (_to_float(x) for x in (lows or [])) if f is not None and f > 0]
    c = _to_float(close)
    n = min(len(hs), len(ls))
    if c is None or c <= 0 or n < max(1, int(min_bars)):
        return {
            "kind": KIND_LOOKBACK, "state": RANGE_DATA_NOT_READY, "value": None,
            "source": "lookback", "reason": "insufficient_history",
            "bars": n, "min_bars": min_bars, "close": c,
        }
    hi, lo = max(hs[:n]), min(ls[:n])
    return {
        "kind": KIND_LOOKBACK, "state": RANGE_OK,
        "value": abs(hi - lo) / c * PERCENT_SCALE,
        "unit": UNIT,
        "source": "lookback", "bars": n, "high": hi, "low": lo, "close": c,
    }


def ratio_to_percent(value: Any) -> float:
    """内部の比率計算 (0-1) を canonical な percent 列へ書き出すときに使う。"""
    f = _to_float(value)
    return 0.0 if f is None else f * PERCENT_SCALE


def percent_to_ratio(value: Any) -> float:
    """canonical な percent 列を内部の比率計算へ持ち込むときに使う。"""
    f = _to_float(value)
    return 0.0 if f is None else f / PERCENT_SCALE


def describe(result: Dict[str, Any]) -> str:
    """ログ 1 行用。"""
    if not isinstance(result, dict):
        return "range=?"
    v = result.get("value")
    return "%s=%s state=%s src=%s" % (
        result.get("kind"),
        "None" if v is None else ("%.6f" % v),
        result.get("state"),
        result.get("source"),
    )


__all__ = [
    "VERSION",
    "PERCENT_SCALE", "UNIT",
    "ratio_to_percent", "percent_to_ratio",
    "RANGE_OK", "RANGE_DATA_NOT_READY",
    "KIND_INTRABAR", "KIND_DAY", "KIND_LOOKBACK",
    "compute_intrabar_range_pct", "compute_day_range_pct", "compute_lookback_range_pct",
    "describe",
]
