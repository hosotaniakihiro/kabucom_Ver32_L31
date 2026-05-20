# ============================================================
# File   : core/startup/entry_direction_failopen_runtime_patch.py
# Version: Ver03-BIDIRECTIONAL-TREND-SAFETY
# ------------------------------------------------------------
# SUMMARY_AI の方向確認ガードで RecursionError / pure guard False が出た場合の runtime patch。
#
# Ver03 重要修正:
#   - 上昇トレンドでSELLを禁止
#   - 下降トレンドでBUYを禁止
#   - 元の方向確認がOKでも、明確に逆トレンドなら止める
#   - fail-open は「トレンドに逆らわない場合のみ」許可
#
# ENV:
#   ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_BUY=1
#   ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_SELL=1
#   ENTRY_BLOCK_SELL_IN_BULLISH_TREND=1
#   ENTRY_BLOCK_BUY_IN_BEARISH_TREND=1
#   ENTRY_TREND_BLOCK_MIN_POINTS=2
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_CHECK = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return dict(row)
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return dict(d)
        return {}
    except Exception:
        return {}


def _first(d: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = d.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _is_summary_ai(row: Any) -> bool:
    d = _row_to_dict(row)
    src = str(d.get("source") or "").upper()
    et = str(d.get("entry_type") or "").upper()
    return src == "SUMMARY" or et == "SUMMARY_AI"


def _symbol(row: Any) -> str:
    d = _row_to_dict(row)
    s = str(d.get("symbol") or d.get("code") or d.get("stock_code") or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _side(row: Any) -> str:
    d = _row_to_dict(row)
    return str(d.get("side") or d.get("entry_decision") or d.get("ai_side") or "").upper()


def _trend_diag(row: Any) -> dict[str, Any]:
    d = _row_to_dict(row)
    close = _safe_float(_first(d, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    open_ = _safe_float(_first(d, ("open", "open_price"), 0.0), 0.0)
    high = _safe_float(_first(d, ("high", "high_price"), 0.0), 0.0)
    low = _safe_float(_first(d, ("low", "low_price"), 0.0), 0.0)
    ma5 = _safe_float(_first(d, ("ma5", "MA5", "ma_5", "daily_ma5", "MA_5"), 0.0), 0.0)
    ma25 = _safe_float(_first(d, ("ma25", "MA25", "ma_25", "daily_ma25", "MA_25"), 0.0), 0.0)
    ma75 = _safe_float(_first(d, ("ma75", "MA75", "ma_75", "daily_ma75", "MA_75"), 0.0), 0.0)
    slope = _safe_float(_first(d, ("slope_atr_scaled", "score_slope", "disp_slope", "slope"), 0.0), 0.0)
    mtf = _safe_float(_first(d, ("score_mtf", "mtf_score", "disp_mtf", "mtf"), 0.0), 0.0)
    macd = _safe_float(d.get("macd"), 0.0)
    signal = _safe_float(d.get("signal"), 0.0)
    hist = _safe_float(d.get("hist"), macd - signal)
    buy_score = _safe_float(_first(d, ("score_buy", "buy_score", "disp_buy_score", "buy"), 0.0), 0.0)
    sell_score = _safe_float(_first(d, ("score_sell", "sell_score", "disp_sell_score", "sell"), 0.0), 0.0)

    macd_diff = hist if hist != 0 else macd - signal

    ma_bull = bool(close > 0 and ma5 > 0 and ma25 > 0 and ma75 > 0 and close >= ma5 >= ma25 >= ma75)
    ma_bear = bool(close > 0 and ma5 > 0 and ma25 > 0 and ma75 > 0 and close <= ma5 <= ma25 <= ma75)
    price_above_ma = bool(close > 0 and ((ma25 > 0 and close > ma25) or (ma75 > 0 and close > ma75)))
    price_below_ma = bool(close > 0 and ((ma25 > 0 and close < ma25) or (ma75 > 0 and close < ma75)))
    bar_green = bool(close > 0 and open_ > 0 and close > open_)
    bar_red = bool(close > 0 and open_ > 0 and close < open_)
    near_high = bool(high > low and close > 0 and ((close - low) / max(high - low, 1e-9)) >= 0.65)
    near_low = bool(high > low and close > 0 and ((close - low) / max(high - low, 1e-9)) <= 0.35)
    slope_up = slope > 0
    slope_down = slope < 0
    mtf_up = mtf > 0
    mtf_down = mtf < 0
    macd_up = macd_diff > 0
    macd_down = macd_diff < 0
    buy_dominant = buy_score > sell_score and buy_score > 0
    sell_dominant = sell_score > buy_score and sell_score > 0

    bullish_points = sum(bool(x) for x in (ma_bull, price_above_ma, bar_green, near_high, slope_up, mtf_up, macd_up, buy_dominant))
    bearish_points = sum(bool(x) for x in (ma_bear, price_below_ma, bar_red, near_low, slope_down, mtf_down, macd_down, sell_dominant))

    return {
        "close": close,
        "open": open_,
        "ma5": ma5,
        "ma25": ma25,
        "ma75": ma75,
        "slope": slope,
        "mtf": mtf,
        "macd": macd,
        "signal": signal,
        "hist": hist,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "ma_bull": ma_bull,
        "ma_bear": ma_bear,
        "price_above_ma": price_above_ma,
        "price_below_ma": price_below_ma,
        "bar_green": bar_green,
        "bar_red": bar_red,
        "near_high": near_high,
        "near_low": near_low,
        "slope_up": slope_up,
        "slope_down": slope_down,
        "mtf_up": mtf_up,
        "mtf_down": mtf_down,
        "macd_up": macd_up,
        "macd_down": macd_down,
        "buy_dominant": buy_dominant,
        "sell_dominant": sell_dominant,
        "bullish_points": bullish_points,
        "bearish_points": bearish_points,
    }


def _is_bullish_trend_for_sell(row: Any) -> bool:
    if not _env_bool("ENTRY_BLOCK_SELL_IN_BULLISH_TREND", True):
        return False
    diag = _trend_diag(row)
    min_points = max(1, _env_int("ENTRY_TREND_BLOCK_MIN_POINTS", 2))
    return int(diag.get("bullish_points") or 0) >= min_points


def _is_bearish_trend_for_buy(row: Any) -> bool:
    if not _env_bool("ENTRY_BLOCK_BUY_IN_BEARISH_TREND", True):
        return False
    diag = _trend_diag(row)
    min_points = max(1, _env_int("ENTRY_TREND_BLOCK_MIN_POINTS", 2))
    return int(diag.get("bearish_points") or 0) >= min_points


def _against_clear_trend(row: Any) -> tuple[bool, str, dict[str, Any]]:
    side = _side(row)
    diag = _trend_diag(row)
    if side == "SELL" and _is_bullish_trend_for_sell(row):
        return True, "sell_in_bullish_trend", diag
    if side == "BUY" and _is_bearish_trend_for_buy(row):
        return True, "buy_in_bearish_trend", diag
    return False, "", diag


def _allow_failopen_for_side(row: Any) -> bool:
    side = _side(row)
    if side == "BUY":
        return _env_bool("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_BUY", True)
    if side == "SELL":
        return _env_bool("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_SELL", True)
    return False


def _maybe_failopen(row: Any, reason: str) -> bool:
    side = _side(row)
    symbol = _symbol(row)

    if not _is_summary_ai(row):
        return False

    against, against_reason, diag = _against_clear_trend(row)
    if against:
        logger.warning(
            "[ENTRY DIRECTION SAFETY] BLOCK %s symbol=%s side=%s reason=%s diag=%s",
            against_reason,
            symbol,
            side,
            reason,
            diag,
        )
        return False

    if not _allow_failopen_for_side(row):
        logger.warning(
            "[ENTRY DIRECTION SAFETY] BLOCK fail-open disabled symbol=%s side=%s reason=%s diag=%s",
            symbol,
            side,
            reason,
            diag,
        )
        return False

    logger.warning(
        "[ENTRY DIRECTION FAILOPEN] allow SUMMARY_AI symbol=%s side=%s reason=%s diag=%s",
        symbol,
        side,
        reason,
        diag,
    )
    return True


def _patched_check_entry_direction_confirm(entry_row: Any = None, *args, **kwargs) -> bool:
    if not callable(_ORIG_CHECK):
        return True

    try:
        ok = bool(_ORIG_CHECK(entry_row, *args, **kwargs))
        if ok:
            against, against_reason, diag = _against_clear_trend(entry_row)
            if _is_summary_ai(entry_row) and against:
                logger.warning(
                    "[ENTRY DIRECTION SAFETY] BLOCK despite original OK %s symbol=%s side=%s diag=%s",
                    against_reason,
                    _symbol(entry_row),
                    _side(entry_row),
                    diag,
                )
                return False
            return True

        return _maybe_failopen(entry_row, "original_guard_ng")

    except RecursionError:
        return _maybe_failopen(entry_row, "recursion_error")

    except Exception as e:
        logger.warning("[ENTRY DIRECTION SAFETY] direction guard error: %s", e, exc_info=False)
        return _maybe_failopen(entry_row, f"guard_error:{e}")


def install() -> bool:
    global _INSTALLED, _ORIG_CHECK
    if _INSTALLED:
        return True

    try:
        from core.startup import entry_direction_confirm_guard_patch as ed

        cur = getattr(ed, "check_entry_direction_confirm", None)
        if getattr(cur, "_entry_direction_failopen_patch_v3", False):
            _INSTALLED = True
            return True

        _ORIG_CHECK = cur
        _patched_check_entry_direction_confirm._entry_direction_failopen_patch_v3 = True  # type: ignore[attr-defined]
        ed.check_entry_direction_confirm = _patched_check_entry_direction_confirm

        _INSTALLED = True
        logger.warning(
            "[ENTRY DIRECTION SAFETY] installed v3 buy_failopen=%s sell_failopen=%s block_sell_bullish=%s block_buy_bearish=%s min_points=%s",
            _env_bool("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_BUY", True),
            _env_bool("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI_SELL", True),
            _env_bool("ENTRY_BLOCK_SELL_IN_BULLISH_TREND", True),
            _env_bool("ENTRY_BLOCK_BUY_IN_BEARISH_TREND", True),
            _env_int("ENTRY_TREND_BLOCK_MIN_POINTS", 2),
        )
        return True
    except Exception as e:
        logger.warning("[ENTRY DIRECTION SAFETY] install failed: %s", e, exc_info=False)
        return False


try:
    install()
except Exception as e:
    logger.warning("[ENTRY DIRECTION SAFETY] auto install failed: %s", e, exc_info=False)

__all__ = ["install"]
