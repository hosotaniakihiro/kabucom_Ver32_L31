# ============================================================
# File   : core/startup/final_entry_liquidity_movement_hard_guard_patch.py
# Version: V1-HARD-LIQUIDITY-MOVEMENT-GUARD
# ------------------------------------------------------------
# 発注直前の最終ハードガード。
#
# 目的:
#   - RANKING/TONOSAMA/SUMMARY の救済パッチで候補が通っても、
#     出来高が少ない銘柄・売買代金が薄い銘柄・値動きが小さい銘柄は
#     注文APIへ到達させない。
#   - 既存の entry_controller._execute_best_candidate をラップするだけなので、
#     候補生成側の救済ロジックは壊さない。
#
# デフォルト:
#   ENTRY_HARD_MIN_VOLUME       = 100000
#   ENTRY_HARD_MIN_TURNOVER     = 50000000
#   ENTRY_HARD_MIN_RANGE_PCT    = 0.006     # 0.6%
#   ENTRY_HARD_MIN_ATR_RATIO    = 0.003     # 0.3%
#   ENTRY_HARD_MIN_ABS_SLOPE    = 0.001
#   ENTRY_HARD_REQUIRE_MOVEMENT = 1
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

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
        return float(v)
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

        raw = d.get("_raw")
        if hasattr(raw, "to_dict"):
            try:
                raw = raw.to_dict()
            except Exception:
                raw = None
        if isinstance(raw, dict):
            for k, val in raw.items():
                if k not in d or d.get(k) in (None, ""):
                    d[k] = val
        return d
    except Exception:
        return {}


def _merge_item_row(item: Any) -> dict:
    row = {}
    try:
        if isinstance(item, dict):
            row.update(_row_to_dict(item.get("entry_row")))
            for src_name in ("entry", "row", "_raw"):
                src = item.get(src_name)
                if isinstance(src, dict):
                    for k, v in src.items():
                        if k not in row or row.get(k) in (None, ""):
                            row[k] = v
            for k, v in item.items():
                if k not in row or row.get(k) in (None, ""):
                    row[k] = v
    except Exception:
        pass
    return row


def _range_pct(row: dict, close: float) -> tuple[float, str]:
    high = _safe_float(_first(row, ("high", "high_price", "HighPrice"), 0.0), 0.0)
    low = _safe_float(_first(row, ("low", "low_price", "LowPrice"), 0.0), 0.0)
    if close > 0 and high > 0 and low > 0 and high >= low:
        return (high - low) / close, "high_low"

    raw = _safe_float(
        _first(
            row,
            (
                "_intrabar_range_pct",
                "intrabar_range_pct",
                "range_pct",
                "price_range_pct",
                "range_1m_pct",
                "range_3m_pct",
                "range_5m_pct",
                "disp_range_pct",
            ),
            0.0,
        ),
        0.0,
    )
    if raw > 1.0:
        raw = raw / 100.0
    return max(0.0, raw), "row_range_pct"


def _hard_guard(item: Any) -> bool:
    if not _env_bool("ENTRY_HARD_LIQUIDITY_MOVEMENT_GUARD_ENABLED", True):
        return True

    row = _merge_item_row(item)
    symbol = _norm_symbol(_first(row, ("symbol", "Symbol", "code", "銘柄コード"), ""))
    side = _norm_side(_first(row, ("side", "entry_decision", "ai_side"), ""))

    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    volume = _safe_float(
        _first(
            row,
            (
                "volume",
                "Volume",
                "出来高",
                "day_volume",
                "acc_volume",
                "trading_volume",
            ),
            0.0,
        ),
        0.0,
    )
    turnover = _safe_float(
        _first(
            row,
            (
                "turnover",
                "trading_value",
                "売買代金",
                "day_turnover",
                "acc_turnover",
                "turnover_value",
            ),
            0.0,
        ),
        0.0,
    )
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume

    min_volume = _env_float("ENTRY_HARD_MIN_VOLUME", 100000.0)
    min_turnover = _env_float("ENTRY_HARD_MIN_TURNOVER", 50000000.0)

    if volume <= 0:
        logger.warning("[ENTRY HARD GUARD] NG symbol=%s side=%s reason=volume_missing close=%.2f turnover=%.0f keys=%s", symbol, side, close, turnover, sorted(list(row.keys()))[:80])
        return False
    if volume < min_volume:
        logger.warning("[ENTRY HARD GUARD] NG symbol=%s side=%s reason=low_volume volume=%.0f min_volume=%.0f close=%.2f turnover=%.0f", symbol, side, volume, min_volume, close, turnover)
        return False
    if turnover < min_turnover:
        logger.warning("[ENTRY HARD GUARD] NG symbol=%s side=%s reason=low_turnover turnover=%.0f min_turnover=%.0f volume=%.0f close=%.2f", symbol, side, turnover, min_turnover, volume, close)
        return False

    if not _env_bool("ENTRY_HARD_REQUIRE_MOVEMENT", True):
        logger.info("[ENTRY HARD GUARD] OK symbol=%s side=%s volume=%.0f turnover=%.0f movement_check=disabled", symbol, side, volume, turnover)
        return True

    range_value, range_source = _range_pct(row, close)
    atr = _safe_float(_first(row, ("atr", "atr_1m", "atr_3m", "atr_5m"), 0.0), 0.0)
    atr_ratio = atr / close if close > 0 and atr > 0 else 0.0
    slope = _safe_float(_first(row, ("slope_atr_scaled", "slope", "score_slope", "disp_slope", "_slope"), 0.0), 0.0)

    min_range = _env_float("ENTRY_HARD_MIN_RANGE_PCT", 0.006)
    min_atr_ratio = _env_float("ENTRY_HARD_MIN_ATR_RATIO", 0.003)
    min_abs_slope = _env_float("ENTRY_HARD_MIN_ABS_SLOPE", 0.001)

    movement_ok = (
        range_value >= min_range
        or atr_ratio >= min_atr_ratio
        or abs(slope) >= min_abs_slope
    )
    if not movement_ok:
        logger.warning(
            "[ENTRY HARD GUARD] NG symbol=%s side=%s reason=low_movement close=%.2f range_pct=%.5f min_range=%.5f range_source=%s atr_ratio=%.5f min_atr=%.5f slope=%.6f min_abs_slope=%.6f volume=%.0f turnover=%.0f",
            symbol,
            side,
            close,
            range_value,
            min_range,
            range_source,
            atr_ratio,
            min_atr_ratio,
            slope,
            min_abs_slope,
            volume,
            turnover,
        )
        return False

    logger.info(
        "[ENTRY HARD GUARD] OK symbol=%s side=%s close=%.2f volume=%.0f turnover=%.0f range_pct=%.5f atr_ratio=%.5f slope=%.6f",
        symbol,
        side,
        close,
        volume,
        turnover,
        range_value,
        atr_ratio,
        slope,
    )
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
        if getattr(cur, "_entry_hard_liq_move_guard_v1", False):
            _INSTALLED = True
            return True
        _ORIG_EXECUTE = cur
        _patched_execute_best_candidate._entry_hard_liq_move_guard_v1 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._original = cur  # type: ignore[attr-defined]
        ec._execute_best_candidate = _patched_execute_best_candidate
        _INSTALLED = True
        logger.warning(
            "[ENTRY HARD GUARD] installed v1 min_volume=%.0f min_turnover=%.0f min_range=%.5f min_atr=%.5f min_abs_slope=%.6f require_movement=%s",
            _env_float("ENTRY_HARD_MIN_VOLUME", 100000.0),
            _env_float("ENTRY_HARD_MIN_TURNOVER", 50000000.0),
            _env_float("ENTRY_HARD_MIN_RANGE_PCT", 0.006),
            _env_float("ENTRY_HARD_MIN_ATR_RATIO", 0.003),
            _env_float("ENTRY_HARD_MIN_ABS_SLOPE", 0.001),
            _env_bool("ENTRY_HARD_REQUIRE_MOVEMENT", True),
        )
        return True
    except Exception:
        logger.exception("[ENTRY HARD GUARD] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[ENTRY HARD GUARD] auto install failed")

__all__ = ["install"]
