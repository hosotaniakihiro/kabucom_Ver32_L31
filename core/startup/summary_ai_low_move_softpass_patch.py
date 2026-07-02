# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_low_move_softpass_patch.py
# Version: V3.2-STRICT-RANGE-REPAIR-NO-SOFTPASS
# ------------------------------------------------------------
# Purpose:
#   SUMMARY_AI の低ATR/低レンジ soft-pass は既定で無効のまま維持する。
#
# Important:
#   - 低出来高・低変動銘柄を緩和せず排除する運用では、soft-pass は不要。
#   - ただし main 1m の最新行だけで entry_order_builder に渡ると、
#     high == low == close になり、実際には日中レンジがある銘柄まで
#     LOW_MOVE_RANGE_TOO_SMALL で落ちることがある。
#   - この V3.2 はガードを緩和しない。判定前に day_high/day_low/range_pct など
#     既に存在する安全なレンジ情報で flat range を補修するだけ。
#
# V3.2:
#   - entry_order_builder._low_move_hard_block をラップ。
#   - SUMMARY_AI の high/low が flat の場合だけ、day_high/day_low,
#     intraday_high/intraday_low, range_high/range_low 等で補完。
#   - 補完できない場合は従来通り LOW_MOVE_RANGE_TOO_SMALL を維持。
#   - soft-pass watcher は起動しない。
# ============================================================
from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V3.2-STRICT-RANGE-REPAIR-NO-SOFTPASS"
_INSTALLED = False
_ORDER_BUILDER_PATCHED = False
_ORIGINAL_LOW_MOVE_HARD_BLOCK = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _first(row: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    try:
        for k in keys:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
    except Exception:
        pass
    return default


def _source_is_summary_ai(source: Any, row: dict) -> bool:
    src = str(source or row.get("source") or row.get("entry_type") or "").strip().upper()
    return src in {"SUMMARY_AI", "SUMMARY", "PUSH", "PUSH_SUMMARY"} or "SUMMARY_AI" in src


def _is_flat_range(close: float, high: float, low: float) -> bool:
    if close <= 0:
        return False
    if high <= 0 or low <= 0:
        return True
    if high < low:
        return True
    return abs(high - low) <= 1e-9


def _repair_flat_range(row: dict, *, symbol: str, source: str) -> tuple[dict, dict]:
    """Return repaired copy and diagnostics. This does not relax low-move thresholds."""
    out = dict(row or {})
    close = _safe_float(_first(out, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    high = _safe_float(_first(out, ("high_price", "high"), 0.0), 0.0)
    low = _safe_float(_first(out, ("low_price", "low"), 0.0), 0.0)

    diag = {
        "symbol": symbol,
        "source": source,
        "close": close,
        "old_high": high,
        "old_low": low,
        "repaired": False,
        "method": None,
    }

    if close <= 0 or not _is_flat_range(close, high, low):
        return out, diag

    # 1) Prefer explicit intraday/day range columns already carried by summary rows.
    high_keys = (
        "day_high",
        "intraday_high",
        "session_high",
        "today_high",
        "range_high",
        "high_1m_max",
        "recent_high",
    )
    low_keys = (
        "day_low",
        "intraday_low",
        "session_low",
        "today_low",
        "range_low",
        "low_1m_min",
        "recent_low",
    )
    h2 = _safe_float(_first(out, high_keys, 0.0), 0.0)
    l2 = _safe_float(_first(out, low_keys, 0.0), 0.0)
    if h2 > 0 and l2 > 0 and h2 >= l2 and h2 > l2:
        out["high"] = h2
        out["low"] = l2
        out["high_price"] = h2
        out["low_price"] = l2
        diag.update({"repaired": True, "method": "day_or_intraday_high_low", "new_high": h2, "new_low": l2})
        return out, diag

    # 2) Use supplied range_pct/range_value if present. This preserves the strict threshold check.
    range_pct = _safe_float(_first(out, ("range_pct", "day_range_pct", "intraday_range_pct", "range_pct_1m"), 0.0), 0.0)
    range_value = _safe_float(_first(out, ("range_value", "day_range_value", "intraday_range_value"), 0.0), 0.0)
    if range_value <= 0 and range_pct > 0 and close > 0:
        # range_pct may be stored as ratio or percent. Values over 1 are treated as percent.
        ratio = range_pct / 100.0 if range_pct > 1.0 else range_pct
        range_value = close * ratio
    if range_value > 0:
        half = range_value / 2.0
        h3 = close + half
        l3 = max(0.01, close - half)
        if h3 > l3:
            out["high"] = h3
            out["low"] = l3
            out["high_price"] = h3
            out["low_price"] = l3
            diag.update({"repaired": True, "method": "range_pct_or_value", "new_high": h3, "new_low": l3, "range_value": range_value})
            return out, diag

    return out, diag


def _install_entry_order_range_repair() -> bool:
    global _ORDER_BUILDER_PATCHED, _ORIGINAL_LOW_MOVE_HARD_BLOCK
    if _ORDER_BUILDER_PATCHED:
        return True
    try:
        from trading.handlers import entry_order_builder as eob

        cur = getattr(eob, "_low_move_hard_block", None)
        if not callable(cur):
            logger.warning("[LOW MOVE GUARD] entry_order_builder._low_move_hard_block not callable version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_flat_range_repair_v32", False):
            _ORDER_BUILDER_PATCHED = True
            return True

        _ORIGINAL_LOW_MOVE_HARD_BLOCK = cur

        def _patched_low_move_hard_block(entry_row: dict, *, symbol: str, source: str):
            row = entry_row if isinstance(entry_row, dict) else {}
            if not _source_is_summary_ai(source, row):
                return _ORIGINAL_LOW_MOVE_HARD_BLOCK(entry_row, symbol=symbol, source=source)

            repaired, diag = _repair_flat_range(row, symbol=str(symbol or ""), source=str(source or ""))
            if diag.get("repaired"):
                logger.warning(
                    "[LOW MOVE GUARD] SUMMARY_AI flat range repaired before strict guard detail=%s version=%s",
                    diag,
                    VERSION,
                )
                try:
                    if isinstance(entry_row, dict):
                        entry_row.update({k: repaired[k] for k in ("high", "low", "high_price", "low_price") if k in repaired})
                except Exception:
                    pass
                return _ORIGINAL_LOW_MOVE_HARD_BLOCK(repaired, symbol=symbol, source=source)

            return _ORIGINAL_LOW_MOVE_HARD_BLOCK(entry_row, symbol=symbol, source=source)

        _patched_low_move_hard_block._summary_ai_flat_range_repair_v32 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._original = cur  # type: ignore[attr-defined]
        eob._low_move_hard_block = _patched_low_move_hard_block
        _ORDER_BUILDER_PATCHED = True
        logger.warning("[LOW MOVE GUARD] SUMMARY_AI flat range repair installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[LOW MOVE GUARD] SUMMARY_AI flat range repair install failed version=%s", VERSION)
        return False


def _install_blowoff_prefilter() -> bool:
    try:
        from core.startup.summary_ai_blowoff_prefilter_patch import install as _install
        ok = bool(_install())
        logger.warning("[LOW MOVE GUARD] chained SUMMARY_AI blowoff prefilter installed=%s version=%s", ok, VERSION)
        return ok
    except Exception:
        logger.exception("[LOW MOVE GUARD] chained SUMMARY_AI blowoff prefilter install failed")
        return False


def install() -> bool:
    """
    Strict mode:
      - デフォルトでは SUMMARY_AI 低変動 soft-pass を一切入れない。
      - watcher も起動しない。
      - low-move 判定そのものは維持する。
      - high/low が latest 1本で flat になった場合だけ、既存の day range 情報で補正する。
    """
    global _INSTALLED

    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS", "0")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER", "0")
    os.environ.setdefault("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", "1")

    blowoff_ok = _install_blowoff_prefilter()
    range_repair_ok = _install_entry_order_range_repair()

    if not _env_bool("SUMMARY_AI_LOW_MOVE_SOFTPASS", False):
        _INSTALLED = bool(blowoff_ok and range_repair_ok)
        logger.warning(
            "[LOW MOVE GUARD] SUMMARY_AI low move softpass disabled strict mode version=%s "
            "SUMMARY_AI_LOW_MOVE_SOFTPASS=%s watcher=%s blowoff_prefilter=%s range_repair=%s",
            VERSION,
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS"),
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER"),
            blowoff_ok,
            range_repair_ok,
        )
        return bool(blowoff_ok and range_repair_ok)

    # Safety: this file intentionally no longer installs a soft-pass implementation.
    _INSTALLED = bool(blowoff_ok and range_repair_ok)
    logger.warning(
        "[LOW MOVE GUARD] SUMMARY_AI low move softpass requested but implementation is disabled in strict build version=%s blowoff_prefilter=%s range_repair=%s",
        VERSION,
        blowoff_ok,
        range_repair_ok,
    )
    return bool(blowoff_ok and range_repair_ok)


try:
    install()
except Exception:
    logger.exception("[LOW MOVE GUARD] SUMMARY_AI low move softpass strict stub auto install failed")


__all__ = ["VERSION", "install"]
