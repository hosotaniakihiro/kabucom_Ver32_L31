# ============================================================
# File   : core/startup/final_entry_liquidity_movement_hard_guard_patch.py
# Version: V5-SUMMARY-AI-LOW-MOVEMENT-RESCUE
# ------------------------------------------------------------
# 発注直前の最終ハードガード。
#
# V5:
#   - SUMMARY_AI が AI_OK / 高score / 十分な出来高・売買代金なのに、
#     1分足の high-low / ATR が小さいだけで low_movement になり、
#     発注直前で全落ちするケースを救済する。
#   - ただし低流動性は従来通りブロックする。
#   - 救済条件: SUMMARY_AI かつ score>=3.0 かつ turnover>=1,000万円 かつ volume>=3万。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V5-SUMMARY-AI-LOW-MOVEMENT-RESCUE"
_INSTALLED = False
_ORIG_EXECUTE = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        s = str(v).strip().replace(",", "")
        if s == "" or s.lower() in {"none", "nan", "nat", "<na>"}:
            return float(default)
        return float(s)
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


def _first_positive(row: dict, keys: tuple[str, ...], default: float = 0.0) -> tuple[float, str]:
    for k in keys:
        try:
            if k not in row:
                continue
            val = _safe_float(row.get(k), 0.0)
            if val > 0:
                return val, k
        except Exception:
            continue
    return float(default), ""


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _copy_nested(prefix: str, src: Any, dst: dict) -> None:
    try:
        if hasattr(src, "to_dict"):
            src = src.to_dict()
        if not isinstance(src, dict):
            return
        for k, v in src.items():
            if k not in dst or dst.get(k) in (None, ""):
                dst[k] = v
            alias = f"{k}_{prefix}"
            if alias not in dst:
                dst[alias] = v
            if isinstance(v, dict):
                _copy_nested(f"{prefix}_{k}", v, dst)
    except Exception:
        pass


def _row_to_dict(v: Any) -> dict:
    try:
        if v is None:
            return {}
        if isinstance(v, dict):
            d = dict(v)
        elif hasattr(v, "to_dict"):
            x = v.to_dict()
            d = dict(x) if isinstance(x, dict) else {}
        else:
            d = {}
        _copy_nested("raw_alias", d.get("_raw"), d)
        _copy_nested("entry_alias", d.get("entry"), d)
        _copy_nested("row_alias", d.get("row"), d)
        _copy_nested("ai_alias", d.get("ai"), d)
        return d
    except Exception:
        return {}


def _merge_item_row(item: Any) -> dict:
    row: dict = {}
    try:
        if isinstance(item, dict):
            row.update(_row_to_dict(item.get("entry_row")))
            _copy_nested("entry_alias", item.get("entry"), row)
            _copy_nested("row_alias", item.get("row"), row)
            _copy_nested("raw_alias", item.get("_raw"), row)
            _copy_nested("ai_alias", item.get("ai"), row)
            for k, v in item.items():
                if k not in row or row.get(k) in (None, ""):
                    row[k] = v
            _copy_nested("item_alias", item, row)
    except Exception:
        pass
    return row


def _range_pct(row: dict, close: float) -> tuple[float, str]:
    high = _safe_float(_first(row, ("high", "high_price", "HighPrice"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low", "low_price", "LowPrice"), 0.0), 0.0)
    if close > 0 and high > 0 and low > 0 and high >= low:
        return (high - low) / close, "high_low"
    raw = _safe_float(_first(row, ("_intrabar_range_pct", "intrabar_range_pct", "range_pct", "price_range_pct", "range_1m_pct", "range_3m_pct", "range_5m_pct", "disp_range_pct"), 0.0), 0.0)
    if raw > 1.0:
        raw = raw / 100.0
    return max(0.0, raw), "row_range_pct"


def _abs_score(row: dict, side: str) -> float:
    side_u = _norm_side(side)
    if side_u == "BUY":
        keys = ("score_buy", "buy_score", "ai_buy_score", "priority_score", "priority", "confidence")
    elif side_u == "SELL":
        keys = ("score_sell", "sell_score", "ai_sell_score", "priority_score", "priority", "confidence")
    else:
        keys = ("score", "score_total", "final_score", "display_score", "priority_score", "priority", "confidence")
    for k in keys:
        v = _safe_float(row.get(k), 0.0)
        if v:
            return abs(v)
    for k in ("score", "score_total", "final_score", "display_score", "combined_score"):
        v = _safe_float(row.get(k), 0.0)
        if v:
            return abs(v)
    return 0.0


def _is_summary_ai(row: dict) -> bool:
    text = " ".join(str(row.get(k) or "") for k in ("source", "pipeline_source", "entry_type", "strategy", "model", "model_used", "reason", "ai_reason"))
    text = text.upper()
    return "SUMMARY_AI" in text or "SUMMARY" in text or "MTF" in text


def _summary_ai_low_movement_rescue(row: dict, *, symbol: str, side: str, volume: float, turnover: float, close: float, range_value: float, atr_ratio: float, slope: float) -> bool:
    if not _env_bool("ENTRY_HARD_SUMMARY_AI_LOW_MOVEMENT_RESCUE", True):
        return False
    if not _is_summary_ai(row):
        return False
    score = _abs_score(row, side)
    min_score = _env_float("ENTRY_HARD_SUMMARY_AI_RESCUE_MIN_SCORE", 3.0)
    min_volume = _env_float("ENTRY_HARD_SUMMARY_AI_RESCUE_MIN_VOLUME", _env_float("ENTRY_HARD_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("ENTRY_HARD_SUMMARY_AI_RESCUE_MIN_TURNOVER", _env_float("ENTRY_HARD_MIN_TURNOVER", 10000000.0))
    if score >= min_score and volume >= min_volume and turnover >= min_turnover and close > 0:
        logger.warning(
            "[ENTRY HARD GUARD] SUMMARY_AI_LOW_MOVEMENT_RESCUE symbol=%s side=%s score=%.3f close=%.2f volume=%.0f turnover=%.0f range_pct=%.5f atr_ratio=%.5f slope=%.6f min_score=%.3f version=%s",
            symbol,
            side,
            score,
            close,
            volume,
            turnover,
            range_value,
            atr_ratio,
            slope,
            min_score,
            VERSION,
        )
        return True
    return False


def _hard_guard(item: Any) -> bool:
    if not _env_bool("ENTRY_HARD_LIQUIDITY_MOVEMENT_GUARD_ENABLED", True):
        return True

    row = _merge_item_row(item)
    symbol = _norm_symbol(_first(row, ("symbol", "Symbol", "code", "銘柄コード"), ""))
    side = _norm_side(_first(row, ("side", "entry_decision", "ai_side"), ""))
    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)

    volume, volume_source = _first_positive(row, ("volume", "Volume", "出来高", "day_volume", "acc_volume", "trading_volume", "_latest_volume", "latest_volume", "latest_vol", "recent_volume", "recent_volume_1m", "recent_volume_3m", "recent_volume_5m", "display_volume_1m", "display_volume_3m", "display_volume_5m", "volume_1m", "volume_3m", "volume_5m", "latest_volume_1m", "volume_raw", "raw_volume", "turnover_volume", "_volume", "_latest_volume_raw_alias", "latest_volume_raw_alias", "recent_volume_1m_raw_alias", "display_volume_1m_raw_alias", "volume_item_alias", "_latest_volume_item_alias", "volume_raw_alias", "Volume_raw_alias", "出来高_raw_alias", "day_volume_raw_alias", "acc_volume_raw_alias", "trading_volume_raw_alias"), 0.0)
    turnover, turnover_source = _first_positive(row, ("turnover", "trading_value", "売買代金", "day_turnover", "acc_turnover", "turnover_value", "turnover_raw", "raw_turnover", "trading_value_raw", "売買代金_raw", "turnover_raw_alias", "trading_value_raw_alias", "売買代金_raw_alias", "day_turnover_raw_alias", "acc_turnover_raw_alias", "turnover_value_raw_alias", "turnover_item_alias", "turnover_raw_item_alias", "trading_value_item_alias"), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
        turnover_source = "close_x_volume"
    if volume <= 0 and close > 0 and turnover > 0:
        volume = turnover / close
        volume_source = "turnover_div_close"

    min_volume = _env_float("ENTRY_HARD_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("ENTRY_HARD_MIN_TURNOVER", 10000000.0)

    if volume <= 0:
        logger.warning("[ENTRY HARD GUARD] NG symbol=%s side=%s reason=volume_missing close=%.2f turnover=%.0f turnover_source=%s keys=%s", symbol, side, close, turnover, turnover_source, sorted(list(row.keys()))[:140])
        return False
    if volume < min_volume:
        logger.warning("[ENTRY HARD GUARD] NG symbol=%s side=%s reason=low_volume volume=%.0f min_volume=%.0f close=%.2f turnover=%.0f volume_source=%s turnover_source=%s", symbol, side, volume, min_volume, close, turnover, volume_source, turnover_source)
        return False
    if turnover < min_turnover:
        logger.warning("[ENTRY HARD GUARD] NG symbol=%s side=%s reason=low_turnover turnover=%.0f min_turnover=%.0f volume=%.0f close=%.2f volume_source=%s turnover_source=%s", symbol, side, turnover, min_turnover, volume, close, volume_source, turnover_source)
        return False

    if not _env_bool("ENTRY_HARD_REQUIRE_MOVEMENT", True):
        logger.info("[ENTRY HARD GUARD] OK symbol=%s side=%s volume=%.0f turnover=%.0f movement_check=disabled volume_source=%s turnover_source=%s", symbol, side, volume, turnover, volume_source, turnover_source)
        return True

    range_value, range_source = _range_pct(row, close)
    atr = _safe_float(_first(row, ("atr", "atr_1m", "atr_3m", "atr_5m"), 0.0), 0.0)
    atr_ratio = atr / close if close > 0 and atr > 0 else 0.0
    slope = _safe_float(_first(row, ("slope_atr_scaled", "slope", "score_slope", "disp_slope", "_slope"), 0.0), 0.0)

    min_range = _env_float("ENTRY_HARD_MIN_RANGE_PCT", 0.006)
    min_atr_ratio = _env_float("ENTRY_HARD_MIN_ATR_RATIO", 0.003)
    min_abs_slope = _env_float("ENTRY_HARD_MIN_ABS_SLOPE", 0.001)

    movement_ok = range_value >= min_range or atr_ratio >= min_atr_ratio or abs(slope) >= min_abs_slope
    if not movement_ok:
        if _summary_ai_low_movement_rescue(row, symbol=symbol, side=side, volume=volume, turnover=turnover, close=close, range_value=range_value, atr_ratio=atr_ratio, slope=slope):
            return True
        logger.warning("[ENTRY HARD GUARD] NG symbol=%s side=%s reason=low_movement close=%.2f range_pct=%.5f min_range=%.5f range_source=%s atr_ratio=%.5f min_atr=%.5f slope=%.6f min_abs_slope=%.6f volume=%.0f turnover=%.0f volume_source=%s turnover_source=%s", symbol, side, close, range_value, min_range, range_source, atr_ratio, min_atr_ratio, slope, min_abs_slope, volume, turnover, volume_source, turnover_source)
        return False

    logger.info("[ENTRY HARD GUARD] OK symbol=%s side=%s close=%.2f volume=%.0f turnover=%.0f range_pct=%.5f atr_ratio=%.5f slope=%.6f volume_source=%s turnover_source=%s", symbol, side, close, volume, turnover, range_value, atr_ratio, slope, volume_source, turnover_source)
    return True


def _patched_execute_best_candidate(*args, **kwargs):
    item = args[0] if args else kwargs.get("item")
    if not _hard_guard(item):
        return False
    return _ORIG_EXECUTE(*args, **kwargs)  # type: ignore[misc]


def install() -> bool:
    global _INSTALLED, _ORIG_EXECUTE
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_execute_best_candidate", None)
        if not callable(cur):
            logger.warning("[ENTRY HARD GUARD] target missing")
            return False
        if getattr(cur, "_entry_hard_liq_move_guard_v5", False):
            _INSTALLED = True
            return True
        _ORIG_EXECUTE = getattr(cur, "_original", cur) if (getattr(cur, "_entry_hard_liq_move_guard_v1", False) or getattr(cur, "_entry_hard_liq_move_guard_v2", False) or getattr(cur, "_entry_hard_liq_move_guard_v3", False) or getattr(cur, "_entry_hard_liq_move_guard_v4", False)) else cur
        _patched_execute_best_candidate._entry_hard_liq_move_guard_v1 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._entry_hard_liq_move_guard_v2 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._entry_hard_liq_move_guard_v3 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._entry_hard_liq_move_guard_v4 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._entry_hard_liq_move_guard_v5 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._original = _ORIG_EXECUTE  # type: ignore[attr-defined]
        ec._execute_best_candidate = _patched_execute_best_candidate
        _INSTALLED = True
        logger.warning(
            "[ENTRY HARD GUARD] installed v5 min_volume=%.0f min_turnover=%.0f min_range=%.5f min_atr=%.5f min_abs_slope=%.6f require_movement=%s summary_ai_low_move_rescue=%s rescue_min_score=%.3f latest_volume_fallback=True raw_fallback=True",
            _env_float("ENTRY_HARD_MIN_VOLUME", 30000.0),
            _env_float("ENTRY_HARD_MIN_TURNOVER", 10000000.0),
            _env_float("ENTRY_HARD_MIN_RANGE_PCT", 0.006),
            _env_float("ENTRY_HARD_MIN_ATR_RATIO", 0.003),
            _env_float("ENTRY_HARD_MIN_ABS_SLOPE", 0.001),
            _env_bool("ENTRY_HARD_REQUIRE_MOVEMENT", True),
            _env_bool("ENTRY_HARD_SUMMARY_AI_LOW_MOVEMENT_RESCUE", True),
            _env_float("ENTRY_HARD_SUMMARY_AI_RESCUE_MIN_SCORE", 3.0),
        )
        return True
    except Exception:
        logger.exception("[ENTRY HARD GUARD] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[ENTRY HARD GUARD] auto install failed")

__all__ = ["install", "VERSION"]
