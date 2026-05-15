# ============================================================
# File   : core/startup/entry_liquidity_runtime_patch.py
# Version: V1.0-ENTRY-LIQUIDITY-MOVEMENT-GUARD
# ------------------------------------------------------------
# 目的:
#   出来高が少ない・売買代金が薄い・値動きが小さい銘柄への
#   新規エントリーを直前で止める。
#
# デフォルト足切り:
#   ENTRY_LIQ_MIN_VOLUME=30000
#   ENTRY_LIQ_MIN_TURNOVER_YEN=10000000
#   ENTRY_LIQ_MIN_RANGE_PCT=0.0015
#   ENTRY_LIQ_MIN_ATR_PCT=0.0010
#   ENTRY_LIQ_REQUIRE_DATA=1
#
# 判定:
#   - volume が 30,000株未満なら停止
#   - close * volume が 1,000万円未満なら停止
#   - high-low の値幅率が 0.15% 未満、かつ ATR率も 0.10% 未満なら停止
#   - 必要データが無い場合も、既定では停止
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


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
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _first_float(row: Dict[str, Any], keys: list[str], default: float = 0.0) -> float:
    for k in keys:
        v = _safe_float(row.get(k), 0.0)
        if v > 0:
            return v
    return float(default)


def _check_liquidity(entry_row: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    if not _env_bool("ENTRY_LIQ_GUARD_ENABLED", True):
        return True, "DISABLED", {}

    require_data = _env_bool("ENTRY_LIQ_REQUIRE_DATA", True)
    min_volume = _env_float("ENTRY_LIQ_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("ENTRY_LIQ_MIN_TURNOVER_YEN", 10000000.0)
    min_range_pct = _env_float("ENTRY_LIQ_MIN_RANGE_PCT", 0.0015)
    min_atr_pct = _env_float("ENTRY_LIQ_MIN_ATR_PCT", 0.0010)

    close = _first_float(entry_row, ["close_price", "close", "price", "current_price"], 0.0)
    volume = _first_float(entry_row, ["volume", "Volume", "vol", "出来高"], 0.0)
    turnover = _first_float(entry_row, ["turnover", "turnover_yen", "trading_value", "売買代金"], 0.0)
    high = _first_float(entry_row, ["high_price", "high"], 0.0)
    low = _first_float(entry_row, ["low_price", "low"], 0.0)
    atr = _first_float(entry_row, ["atr", "atr_1m", "atr_3m", "atr_5m"], 0.0)

    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume

    range_pct = 0.0
    if close > 0 and high > 0 and low > 0 and high >= low:
        range_pct = (high - low) / close

    atr_pct = 0.0
    if close > 0 and atr > 0:
        atr_pct = atr / close

    detail = {
        "symbol": entry_row.get("symbol"),
        "side": entry_row.get("entry_decision") or entry_row.get("side"),
        "source": entry_row.get("source"),
        "interval": entry_row.get("interval"),
        "close": close,
        "volume": volume,
        "turnover": turnover,
        "range_pct": range_pct,
        "atr_pct": atr_pct,
        "min_volume": min_volume,
        "min_turnover": min_turnover,
        "min_range_pct": min_range_pct,
        "min_atr_pct": min_atr_pct,
    }

    if require_data:
        if close <= 0:
            return False, "LIQUIDITY_NO_CLOSE", detail
        if volume <= 0:
            return False, "LIQUIDITY_NO_VOLUME", detail

    if min_volume > 0 and volume < min_volume:
        return False, "LIQUIDITY_VOLUME_LOW", detail

    if min_turnover > 0 and turnover < min_turnover:
        return False, "LIQUIDITY_TURNOVER_LOW", detail

    # 値動き判定は range_pct と atr_pct のどちらか一方が基準を満たせばOK。
    # どちらも小さい銘柄は「動かない銘柄」として停止。
    if (min_range_pct > 0 or min_atr_pct > 0):
        range_ok = range_pct >= min_range_pct if min_range_pct > 0 else False
        atr_ok = atr_pct >= min_atr_pct if min_atr_pct > 0 else False
        if not range_ok and not atr_ok:
            return False, "LIQUIDITY_MOVEMENT_LOW", detail

    return True, "LIQUIDITY_OK", detail


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception("[ENTRY LIQ GUARD] import entry_controller failed")
        return False

    old_execute = getattr(ec, "_execute_best_candidate", None)
    if not callable(old_execute):
        logger.warning("[ENTRY LIQ GUARD] _execute_best_candidate not callable")
        return False

    if not getattr(old_execute, "_entry_liq_guard_wrapped_v1", False):
        def _execute_best_candidate_liq_guard(item: dict, boost_active: bool) -> bool:
            try:
                entry_row = item.get("entry_row") if isinstance(item, dict) else None
                symbol = item.get("symbol") if isinstance(item, dict) else None
                side = item.get("side") if isinstance(item, dict) else None
                if isinstance(entry_row, dict):
                    if "symbol" not in entry_row or not entry_row.get("symbol"):
                        entry_row["symbol"] = symbol
                    if "side" not in entry_row or not entry_row.get("side"):
                        entry_row["side"] = side
                    ok, reason, detail = _check_liquidity(entry_row)
                    if not ok:
                        try:
                            ec._log_skip(str(symbol), reason, **detail)
                        except Exception:
                            logger.warning("[ENTRY LIQ GUARD] blocked symbol=%s reason=%s detail=%s", symbol, reason, detail)
                        return False
            except Exception:
                logger.exception("[ENTRY LIQ GUARD] precheck failed; fail-open")

            return old_execute(item, boost_active=boost_active)

        _execute_best_candidate_liq_guard._entry_liq_guard_wrapped_v1 = True  # type: ignore[attr-defined]
        _execute_best_candidate_liq_guard._original = old_execute  # type: ignore[attr-defined]
        ec._execute_best_candidate = _execute_best_candidate_liq_guard

    _INSTALLED = True
    logger.warning(
        "[ENTRY LIQ GUARD] installed min_volume=%s min_turnover=%s min_range_pct=%s min_atr_pct=%s require_data=%s",
        _env_float("ENTRY_LIQ_MIN_VOLUME", 30000.0),
        _env_float("ENTRY_LIQ_MIN_TURNOVER_YEN", 10000000.0),
        _env_float("ENTRY_LIQ_MIN_RANGE_PCT", 0.0015),
        _env_float("ENTRY_LIQ_MIN_ATR_PCT", 0.0010),
        _env_bool("ENTRY_LIQ_REQUIRE_DATA", True),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY LIQ GUARD] auto install failed")

__all__ = ["install"]
