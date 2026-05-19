# ============================================================
# File   : trading/entry/climax_reversal_detector.py
# Version: Ver01-MINUTE-CLIMAX-REVERSAL-DETECTOR
# ------------------------------------------------------------
# 分足レベルのクライマックス反転検出。
#
# 対象:
#   1. selling_climax_wick
#      急落 + 出来高/売買代金急増 + 下ヒゲ + 戻り
#
#   2. selling_climax_absorption
#      長い下落トレンド + 出来高/売買代金増加 + 下落失速
#      いわゆる「売り枯れ」「吸収」型。
#
#   3. buying_climax_wick
#      急騰 + 出来高/売買代金急増 + 上ヒゲ + 失速
#
#   4. buying_climax_exhaustion
#      長い上昇トレンド + 出来高/売買代金増加 + 上昇失速
#      いわゆる「買い疲れ」型。
#
# 方針:
#   - この検出だけで無条件発注はしない。
#   - MA構造/MA乖離ガードに対する「例外候補」として使う。
#   - 出来高・売買代金・失速・乖離の複合条件が必要。
#
# entry_row にあると利用する主な列:
#   open/open_price, high/high_price, low/low_price, close/close_price
#   volume, turnover
#   volume_ma20, turnover_ma20
#   rolling_high_20, rolling_low_20
#   price_change_5, price_change_20
#   ma25, ma75, daily_ma25, daily_ma75
#   rsi
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _first(row: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _get_ohlcv(row: dict) -> dict:
    open_ = _safe_float(_first(row, ("open", "open_price"), 0.0), 0.0)
    high = _safe_float(_first(row, ("high", "high_price"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low", "low_price"), 0.0), 0.0)
    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    volume = _safe_float(_first(row, ("volume", "Volume", "出来高"), 0.0), 0.0)
    turnover = _safe_float(_first(row, ("turnover", "trading_value", "売買代金"), 0.0), 0.0)

    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume

    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "turnover": turnover,
    }


def _get_ma(row: dict) -> tuple[float, float]:
    ma25 = _safe_float(_first(row, ("ma25", "MA25", "ma_25", "daily_ma25", "MA_25"), 0.0), 0.0)
    ma75 = _safe_float(_first(row, ("ma75", "MA75", "ma_75", "daily_ma75", "MA_75"), 0.0), 0.0)
    return ma25, ma75


def _dev_pct(close: float, ma: float) -> float:
    try:
        if close <= 0 or ma <= 0:
            return 0.0
        return ((close - ma) / ma) * 100.0
    except Exception:
        return 0.0


def _bar_metrics(open_: float, high: float, low: float, close: float) -> dict:
    rng = max(high - low, 0.0)
    if high <= 0 or low <= 0 or close <= 0 or rng <= 0:
        return {
            "range_pct": 0.0,
            "close_pos": 0.5,
            "lower_wick_ratio": 0.0,
            "upper_wick_ratio": 0.0,
            "body_ratio": 0.0,
        }

    lower_body = min(open_ if open_ > 0 else close, close)
    upper_body = max(open_ if open_ > 0 else close, close)
    lower_wick = max(lower_body - low, 0.0)
    upper_wick = max(high - upper_body, 0.0)
    body = abs(close - (open_ if open_ > 0 else close))

    return {
        "range_pct": (rng / close) * 100.0,
        "close_pos": (close - low) / rng,
        "lower_wick_ratio": lower_wick / rng,
        "upper_wick_ratio": upper_wick / rng,
        "body_ratio": body / rng,
    }


def _volume_metrics(row: dict, close: float, volume: float, turnover: float) -> dict:
    volume_ma = _safe_float(_first(row, ("volume_ma20", "vol_ma20", "avg_volume20", "volume_avg20"), 0.0), 0.0)
    turnover_ma = _safe_float(_first(row, ("turnover_ma20", "trading_value_ma20", "avg_turnover20", "turnover_avg20"), 0.0), 0.0)

    # MAが無い場合は、最低流動性を満たしていれば ratio=1.0 とする。
    volume_ratio = (volume / volume_ma) if volume_ma > 0 else 1.0
    turnover_ratio = (turnover / turnover_ma) if turnover_ma > 0 else 1.0

    return {
        "volume_ma20": volume_ma,
        "turnover_ma20": turnover_ma,
        "volume_ratio": volume_ratio,
        "turnover_ratio": turnover_ratio,
    }


def _trend_metrics(row: dict, close: float, high: float, low: float) -> dict:
    # 既存列があれば使う。無ければ現在足だけで控えめに判定する。
    price_change_5 = _safe_float(_first(row, ("price_change_5", "change_5", "ret_5", "return_5"), 0.0), 0.0)
    price_change_20 = _safe_float(_first(row, ("price_change_20", "change_20", "ret_20", "return_20"), 0.0), 0.0)
    rolling_low_20 = _safe_float(_first(row, ("rolling_low_20", "low_20", "min_low_20"), 0.0), 0.0)
    rolling_high_20 = _safe_float(_first(row, ("rolling_high_20", "high_20", "max_high_20"), 0.0), 0.0)

    # pct表記/ratio表記の両対応。0.02なら2%、2.0なら2%。
    def _as_pct(v: float) -> float:
        if abs(v) <= 1.0:
            return v * 100.0
        return v

    pc5 = _as_pct(price_change_5)
    pc20 = _as_pct(price_change_20)

    low_near_20 = bool(rolling_low_20 > 0 and low <= rolling_low_20 * 1.003)
    high_near_20 = bool(rolling_high_20 > 0 and high >= rolling_high_20 * 0.997)

    return {
        "price_change_5_pct": pc5,
        "price_change_20_pct": pc20,
        "rolling_low_20": rolling_low_20,
        "rolling_high_20": rolling_high_20,
        "low_near_20": low_near_20,
        "high_near_20": high_near_20,
    }


def detect_climax_reversal(entry_row: Any, side: Any = None) -> dict:
    """
    Returns
    -------
    dict:
      {
        allow_exception: bool,
        climax_type: str,
        climax_score: float,
        reason: str,
        details: dict,
      }
    """
    if not _env_bool("ENTRY_CLIMAX_REVERSAL_ENABLED", True):
        return {"allow_exception": False, "climax_type": "", "climax_score": 0.0, "reason": "disabled", "details": {}}

    try:
        row = entry_row if isinstance(entry_row, dict) else dict(entry_row.to_dict()) if hasattr(entry_row, "to_dict") else {}
    except Exception:
        row = {}

    side_n = _norm_side(side or _first(row, ("side", "entry_decision", "ai_side"), ""))
    symbol = _norm_symbol(_first(row, ("symbol", "code", "stock_code"), ""))

    ohlcv = _get_ohlcv(row)
    open_ = ohlcv["open"]
    high = ohlcv["high"]
    low = ohlcv["low"]
    close = ohlcv["close"]
    volume = ohlcv["volume"]
    turnover = ohlcv["turnover"]

    if close <= 0 or high <= 0 or low <= 0 or volume <= 0:
        return {"allow_exception": False, "climax_type": "", "climax_score": 0.0, "reason": "missing_ohlcv", "details": ohlcv}

    ma25, ma75 = _get_ma(row)
    dev25 = _dev_pct(close, ma25)
    dev75 = _dev_pct(close, ma75)
    bar = _bar_metrics(open_, high, low, close)
    volm = _volume_metrics(row, close, volume, turnover)
    trend = _trend_metrics(row, close, high, low)
    rsi = _safe_float(_first(row, ("rsi", "RSI"), 50.0), 50.0)

    min_volume = _env_float("ENTRY_CLIMAX_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("ENTRY_CLIMAX_MIN_TURNOVER", 10000000.0)
    min_volume_ratio = _env_float("ENTRY_CLIMAX_MIN_VOLUME_RATIO", 1.8)
    min_turnover_ratio = _env_float("ENTRY_CLIMAX_MIN_TURNOVER_RATIO", 1.5)
    min_range_pct = _env_float("ENTRY_CLIMAX_MIN_RANGE_PCT", 0.6)

    if volume < min_volume or turnover < min_turnover:
        return {
            "allow_exception": False,
            "climax_type": "",
            "climax_score": 0.0,
            "reason": "liquidity_not_enough",
            "details": {**ohlcv, **volm, "min_volume": min_volume, "min_turnover": min_turnover},
        }

    volume_ok = volm["volume_ratio"] >= min_volume_ratio or volm["volume_ma20"] <= 0
    turnover_ok = volm["turnover_ratio"] >= min_turnover_ratio or volm["turnover_ma20"] <= 0
    range_ok = bar["range_pct"] >= min_range_pct

    # --------------------------------------------------------
    # BUY: selling climax / absorption
    # --------------------------------------------------------
    if side_n == "BUY":
        wick_score = 0.0
        wick_reasons = []
        if volume_ok:
            wick_score += 1.0; wick_reasons.append("volume_spike")
        if turnover_ok:
            wick_score += 1.0; wick_reasons.append("turnover_spike")
        if range_ok:
            wick_score += 0.5; wick_reasons.append("wide_range")
        if bar["lower_wick_ratio"] >= _env_float("ENTRY_SELLING_CLIMAX_MIN_LOWER_WICK", 0.35):
            wick_score += 1.0; wick_reasons.append("lower_wick")
        if bar["close_pos"] >= _env_float("ENTRY_SELLING_CLIMAX_MIN_CLOSE_POS", 0.55):
            wick_score += 1.0; wick_reasons.append("close_recovered")
        if dev25 <= _env_float("ENTRY_SELLING_CLIMAX_MA25_DEV_PCT", -1.0) or dev75 <= _env_float("ENTRY_SELLING_CLIMAX_MA75_DEV_PCT", -1.5):
            wick_score += 1.0; wick_reasons.append("negative_ma_deviation")
        if rsi <= _env_float("ENTRY_SELLING_CLIMAX_MAX_RSI", 42.0):
            wick_score += 0.5; wick_reasons.append("low_rsi")
        if trend["low_near_20"]:
            wick_score += 0.5; wick_reasons.append("near_20_low")

        absorption_score = 0.0
        absorption_reasons = []
        pc5 = trend["price_change_5_pct"]
        pc20 = trend["price_change_20_pct"]
        if pc20 <= _env_float("ENTRY_ABSORPTION_MIN_20BAR_DROP_PCT", -1.5):
            absorption_score += 1.0; absorption_reasons.append("downtrend_20")
        if pc5 > pc20 * _env_float("ENTRY_ABSORPTION_DECEL_RATIO", 0.45):
            absorption_score += 1.0; absorption_reasons.append("drop_decelerating")
        if volume_ok:
            absorption_score += 1.0; absorption_reasons.append("volume_increasing")
        if turnover_ok:
            absorption_score += 1.0; absorption_reasons.append("turnover_increasing")
        if dev25 <= _env_float("ENTRY_SELLING_CLIMAX_MA25_DEV_PCT", -1.0) or dev75 <= _env_float("ENTRY_SELLING_CLIMAX_MA75_DEV_PCT", -1.5):
            absorption_score += 1.0; absorption_reasons.append("negative_ma_deviation")
        if bar["close_pos"] >= _env_float("ENTRY_ABSORPTION_MIN_CLOSE_POS", 0.45):
            absorption_score += 0.5; absorption_reasons.append("not_close_at_low")
        if rsi <= _env_float("ENTRY_ABSORPTION_MAX_RSI", 45.0):
            absorption_score += 0.5; absorption_reasons.append("rsi_low")

        if absorption_score >= _env_float("ENTRY_ABSORPTION_MIN_SCORE", 4.0):
            ctype = "selling_climax_absorption"
            score = absorption_score
            reasons = absorption_reasons
        elif wick_score >= _env_float("ENTRY_SELLING_CLIMAX_WICK_MIN_SCORE", 4.0):
            ctype = "selling_climax_wick"
            score = wick_score
            reasons = wick_reasons
        else:
            ctype = ""
            score = max(absorption_score, wick_score)
            reasons = absorption_reasons if absorption_score >= wick_score else wick_reasons

        allow = bool(ctype)
        logger.warning(
            "[ENTRY CLIMAX REVERSAL] %s symbol=%s side=BUY type=%s score=%.2f reasons=%s volume=%.0f turnover=%.0f vol_ratio=%.2f turnover_ratio=%.2f dev25=%.3f dev75=%.3f close_pos=%.2f lower_wick=%.2f pc5=%.3f pc20=%.3f rsi=%.1f",
            "OK" if allow else "NG",
            symbol, ctype, score, reasons, volume, turnover, volm["volume_ratio"], volm["turnover_ratio"], dev25, dev75, bar["close_pos"], bar["lower_wick_ratio"], pc5, pc20, rsi,
        )
        return {
            "allow_exception": allow,
            "climax_type": ctype,
            "climax_score": float(score),
            "reason": "|".join(reasons),
            "details": {**ohlcv, **bar, **volm, **trend, "dev25": dev25, "dev75": dev75, "rsi": rsi},
        }

    # --------------------------------------------------------
    # SELL: buying climax / exhaustion
    # --------------------------------------------------------
    if side_n == "SELL":
        wick_score = 0.0
        wick_reasons = []
        if volume_ok:
            wick_score += 1.0; wick_reasons.append("volume_spike")
        if turnover_ok:
            wick_score += 1.0; wick_reasons.append("turnover_spike")
        if range_ok:
            wick_score += 0.5; wick_reasons.append("wide_range")
        if bar["upper_wick_ratio"] >= _env_float("ENTRY_BUYING_CLIMAX_MIN_UPPER_WICK", 0.35):
            wick_score += 1.0; wick_reasons.append("upper_wick")
        if bar["close_pos"] <= _env_float("ENTRY_BUYING_CLIMAX_MAX_CLOSE_POS", 0.45):
            wick_score += 1.0; wick_reasons.append("close_faded")
        if dev25 >= _env_float("ENTRY_BUYING_CLIMAX_MA25_DEV_PCT", 1.0) or dev75 >= _env_float("ENTRY_BUYING_CLIMAX_MA75_DEV_PCT", 1.5):
            wick_score += 1.0; wick_reasons.append("positive_ma_deviation")
        if rsi >= _env_float("ENTRY_BUYING_CLIMAX_MIN_RSI", 58.0):
            wick_score += 0.5; wick_reasons.append("high_rsi")
        if trend["high_near_20"]:
            wick_score += 0.5; wick_reasons.append("near_20_high")

        exhaustion_score = 0.0
        exhaustion_reasons = []
        pc5 = trend["price_change_5_pct"]
        pc20 = trend["price_change_20_pct"]
        if pc20 >= _env_float("ENTRY_EXHAUSTION_MIN_20BAR_RISE_PCT", 1.5):
            exhaustion_score += 1.0; exhaustion_reasons.append("uptrend_20")
        if pc5 < pc20 * _env_float("ENTRY_EXHAUSTION_DECEL_RATIO", 0.45):
            exhaustion_score += 1.0; exhaustion_reasons.append("rise_decelerating")
        if volume_ok:
            exhaustion_score += 1.0; exhaustion_reasons.append("volume_increasing")
        if turnover_ok:
            exhaustion_score += 1.0; exhaustion_reasons.append("turnover_increasing")
        if dev25 >= _env_float("ENTRY_BUYING_CLIMAX_MA25_DEV_PCT", 1.0) or dev75 >= _env_float("ENTRY_BUYING_CLIMAX_MA75_DEV_PCT", 1.5):
            exhaustion_score += 1.0; exhaustion_reasons.append("positive_ma_deviation")
        if bar["close_pos"] <= _env_float("ENTRY_EXHAUSTION_MAX_CLOSE_POS", 0.55):
            exhaustion_score += 0.5; exhaustion_reasons.append("not_close_at_high")
        if rsi >= _env_float("ENTRY_EXHAUSTION_MIN_RSI", 55.0):
            exhaustion_score += 0.5; exhaustion_reasons.append("rsi_high")

        if exhaustion_score >= _env_float("ENTRY_EXHAUSTION_MIN_SCORE", 4.0):
            ctype = "buying_climax_exhaustion"
            score = exhaustion_score
            reasons = exhaustion_reasons
        elif wick_score >= _env_float("ENTRY_BUYING_CLIMAX_WICK_MIN_SCORE", 4.0):
            ctype = "buying_climax_wick"
            score = wick_score
            reasons = wick_reasons
        else:
            ctype = ""
            score = max(exhaustion_score, wick_score)
            reasons = exhaustion_reasons if exhaustion_score >= wick_score else wick_reasons

        allow = bool(ctype)
        logger.warning(
            "[ENTRY CLIMAX REVERSAL] %s symbol=%s side=SELL type=%s score=%.2f reasons=%s volume=%.0f turnover=%.0f vol_ratio=%.2f turnover_ratio=%.2f dev25=%.3f dev75=%.3f close_pos=%.2f upper_wick=%.2f pc5=%.3f pc20=%.3f rsi=%.1f",
            "OK" if allow else "NG",
            symbol, ctype, score, reasons, volume, turnover, volm["volume_ratio"], volm["turnover_ratio"], dev25, dev75, bar["close_pos"], bar["upper_wick_ratio"], pc5, pc20, rsi,
        )
        return {
            "allow_exception": allow,
            "climax_type": ctype,
            "climax_score": float(score),
            "reason": "|".join(reasons),
            "details": {**ohlcv, **bar, **volm, **trend, "dev25": dev25, "dev75": dev75, "rsi": rsi},
        }

    return {"allow_exception": False, "climax_type": "", "climax_score": 0.0, "reason": "unknown_side", "details": {}}


__all__ = ["detect_climax_reversal"]
