# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_approved_row_range_repair_patch.py
# Version: V1-SUMMARY-AI-APPROVED-ROW-RANGE-REPAIR
# ------------------------------------------------------------
# Purpose:
#   Summary-AI の AI_OK -> approved_row 作成後、発注直前の row で
#   high/low が close と同値、または 0/欠損になって低変動ガードに
#   誤って落ちる問題を補正する。
#
# Important:
#   - 低変動ガードの閾値は緩めない。
#   - day_high/day_low, high_price/low_price, range_high/range_low など
#     既に候補DF/AI itemに存在する実レンジだけを high/low に反映する。
#   - 実レンジが無ければ補正しない。
# ============================================================
from __future__ import annotations

import functools
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1-SUMMARY-AI-APPROVED-ROW-RANGE-REPAIR"
_INSTALLED = False

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if x != x or x in (float("inf"), float("-inf")):
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _row_dict(row: Any) -> dict[str, Any]:
    try:
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _get_first(d: dict[str, Any], names: tuple[str, ...], default: float = 0.0) -> float:
    for name in names:
        try:
            if name in d:
                x = _safe_float(d.get(name), 0.0)
                if x > 0:
                    return x
        except Exception:
            continue
    return float(default)


def _put(row: Any, key: str, value: float) -> None:
    try:
        if isinstance(row, dict):
            row[key] = value
            return
        if hasattr(row, "__setitem__"):
            row[key] = value
            return
        setattr(row, key, value)
    except Exception:
        pass


def _merge_sources(approved_row: Any, item: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    try:
        merged.update(_row_dict(item))
    except Exception:
        pass
    try:
        merged.update(_row_dict(approved_row))
    except Exception:
        pass
    return merged


def _repair_approved_row_range(approved_row: Any, item: Any = None) -> Any:
    try:
        if approved_row is None:
            return approved_row
        d = _merge_sources(approved_row, item)
        symbol = str(d.get("symbol") or d.get("Symbol") or "").strip()

        close = _get_first(d, (
            "close", "close_price", "current_price", "price", "last_price", "ai_disp_close",
            "entry_price", "limit_price",
        ))
        if close <= 0:
            return approved_row

        high = _get_first(d, ("high", "high_price", "bar_high", "summary_high"))
        low = _get_first(d, ("low", "low_price", "bar_low", "summary_low"))
        current_range_ok = high > 0 and low > 0 and high >= low and abs(high - low) > 1e-9
        if current_range_ok:
            return approved_row

        range_high = _get_first(d, (
            "day_high", "today_high", "intraday_high", "session_high",
            "range_high", "ranking_high", "snapshot_high", "high_price_day",
            "ai_disp_day_high", "ai_disp_high",
        ))
        range_low = _get_first(d, (
            "day_low", "today_low", "intraday_low", "session_low",
            "range_low", "ranking_low", "snapshot_low", "low_price_day",
            "ai_disp_day_low", "ai_disp_low",
        ))

        if not (range_high > 0 and range_low > 0 and range_high >= range_low):
            return approved_row
        if abs(range_high - range_low) <= 1e-9:
            return approved_row

        # 発注直前価格が日中レンジ外に少し出ている場合でも、high/lowが破綻しないよう包含する。
        repaired_high = max(float(range_high), float(close))
        repaired_low = min(float(range_low), float(close))
        if repaired_high <= 0 or repaired_low <= 0 or repaired_high < repaired_low:
            return approved_row

        _put(approved_row, "high", repaired_high)
        _put(approved_row, "low", repaired_low)
        _put(approved_row, "range_high", repaired_high)
        _put(approved_row, "range_low", repaired_low)
        _put(approved_row, "day_high", repaired_high)
        _put(approved_row, "day_low", repaired_low)
        try:
            range_pct = abs(repaired_high - repaired_low) / max(float(close), 1.0)
            _put(approved_row, "range_pct", range_pct)
            _put(approved_row, "intraday_range_pct", range_pct)
            _put(approved_row, "day_range_pct", range_pct)
            _put(approved_row, "summary_ai_approved_row_range_repaired", True)
            _put(approved_row, "summary_ai_approved_row_range_repair_version", VERSION)
        except Exception:
            pass

        logger.warning(
            "[SUMMARY AI APPROVED ROW RANGE REPAIR] repaired symbol=%s close=%s old_high=%s old_low=%s new_high=%s new_low=%s version=%s",
            symbol,
            close,
            high,
            low,
            repaired_high,
            repaired_low,
            VERSION,
        )
        return approved_row
    except Exception:
        logger.exception("[SUMMARY AI APPROVED ROW RANGE REPAIR] repair failed; return original")
        return approved_row


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_AI_APPROVED_ROW_RANGE_REPAIR", True):
        logger.warning("[SUMMARY AI APPROVED ROW RANGE REPAIR] disabled by env")
        return False
    try:
        import trading.entry.summary_ai.executor as ex

        cur = getattr(ex, "build_approved_row", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI APPROVED ROW RANGE REPAIR] target build_approved_row missing")
            return False
        if getattr(cur, "_summary_ai_approved_row_range_repair_v1", False):
            _INSTALLED = True
            return True

        original = getattr(cur, "_original", cur)

        @functools.wraps(original)
        def _wrapped_build_approved_row(*args: Any, **kwargs: Any):
            row = original(*args, **kwargs)
            item = None
            try:
                if args:
                    item = args[0]
                else:
                    item = kwargs.get("item") or kwargs.get("ai_item") or kwargs.get("result")
            except Exception:
                item = None
            return _repair_approved_row_range(row, item)

        _wrapped_build_approved_row._summary_ai_approved_row_range_repair_v1 = True  # type: ignore[attr-defined]
        _wrapped_build_approved_row._original = original  # type: ignore[attr-defined]
        ex.build_approved_row = _wrapped_build_approved_row
        _INSTALLED = True
        logger.warning("[SUMMARY AI APPROVED ROW RANGE REPAIR] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI APPROVED ROW RANGE REPAIR] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI APPROVED ROW RANGE REPAIR] auto install failed")


__all__ = ["install", "VERSION"]
