# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_low_move_softpass_patch.py
# Version: V3.5-EXECUTOR-ROLLING-RETRY-INLINED
# ------------------------------------------------------------
# V3.5:
#   - execute_ai_ok_entries_bulk のrolling-retry部分は
#     trading/entry/summary_ai/executor.py 本体 (REV11) へインライン化済みのため、
#     ここでの差し替えは撤去した（entry_order_builder._low_move_hard_block と
#     volatility_filter._range_5m_filter_from_entry_row のラップは維持）。
#
# Purpose:
#   SUMMARY_AI の低ATR/低レンジ soft-pass は既定で無効のまま維持する。
#
# Important:
#   - 低出来高・低変動銘柄を緩和せず排除する運用では、soft-pass は不要。
#   - ただし main 1m の最新行だけで entry_order_builder / volatility_filter に渡ると、
#     high == low == close や 5m未成熟のため、実際にはランキング/当日変動がある銘柄まで
#     LOW_MOVE_RANGE_TOO_SMALL / liquidity で落ちることがある。
#   - また、承認済み候補が7件以上あっても、先頭Top3が最終ガードNGだと
#     executor がそこで no-order 終了して次候補へ進まない。
#   - この V3.4 はガードを緩和しない。低変動NGは維持し、Top3全滅時だけ
#     次の承認候補バッチへ繰り上げる。さらに、SUMMARY_AI 1分即時候補で
#     entry_row_range_ok=True かつ ranking_snapshot の実変動が十分な場合だけ、
#     5m代替レンジの二重ブロックを避ける。
#
# V3.4:
#   - trading.filters.volatility_filter._range_5m_filter_from_entry_row をラップ。
#   - SUMMARY_AI/SUMMARY/PUSH 由来で entry_row の高安幅が最低条件を満たし、
#     ranking rescue も通る場合だけ allow。
#   - その他の低変動NGは従来通り fail-close。
#
# V3.3:
#   - entry_order_builder._low_move_hard_block をラップ。
#   - SUMMARY_AI の high/low が flat の場合だけ day_high/day_low 等で補完。
#   - summary_ai.executor.execute_ai_ok_entries_bulk をラップし、Top3 no-order 時に
#     次の承認済み候補へ進む。
#   - ENTRY_EXECUTE_ORIG_TIMEOUT_SEC 既定を15秒へ引き上げ。
# ============================================================
from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V3.4-STRICT-RANGE-REPAIR-ROLLING-AND-1M-BRIDGE"
_INSTALLED = False
_ORDER_BUILDER_PATCHED = False
_EXECUTOR_PATCHED = False
_VOL_FILTER_PATCHED = False
_ORIGINAL_LOW_MOVE_HARD_BLOCK = None
_ORIGINAL_RANGE_5M_ENTRY_ROW = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(str(raw).replace(",", "")))
    except Exception:
        return int(default)


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


def _row_dict(entry_row: Any) -> dict:
    try:
        if isinstance(entry_row, dict):
            return dict(entry_row)
        if hasattr(entry_row, "to_dict"):
            d = entry_row.to_dict()
            return dict(d) if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _source_is_summary_ai(source: Any, row: dict) -> bool:
    src = str(source or row.get("source") or row.get("entry_type") or row.get("pipeline_source") or "").strip().upper()
    text = " ".join(str(row.get(k) or "") for k in ("entry_type", "source", "reason", "ai_reason", "model_used")).upper()
    return src in {"SUMMARY_AI", "SUMMARY", "PUSH", "PUSH_SUMMARY"} or "SUMMARY_AI" in src or "SRC=SUMMARY" in text or "SUMMARY_AI" in text


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

    high_keys = ("day_high", "intraday_high", "session_high", "today_high", "range_high", "high_1m_max", "recent_high")
    low_keys = ("day_low", "intraday_low", "session_low", "today_low", "range_low", "low_1m_min", "recent_low")
    h2 = _safe_float(_first(out, high_keys, 0.0), 0.0)
    l2 = _safe_float(_first(out, low_keys, 0.0), 0.0)
    if h2 > 0 and l2 > 0 and h2 >= l2 and h2 > l2:
        out["high"] = h2
        out["low"] = l2
        out["high_price"] = h2
        out["low_price"] = l2
        diag.update({"repaired": True, "method": "day_or_intraday_high_low", "new_high": h2, "new_low": l2})
        return out, diag

    range_pct = _safe_float(_first(out, ("range_pct", "day_range_pct", "intraday_range_pct", "range_pct_1m"), 0.0), 0.0)
    range_value = _safe_float(_first(out, ("range_value", "day_range_value", "intraday_range_value"), 0.0), 0.0)
    if range_value <= 0 and range_pct > 0 and close > 0:
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
        if getattr(cur, "_summary_ai_flat_range_repair_v34", False):
            _ORDER_BUILDER_PATCHED = True
            return True

        _ORIGINAL_LOW_MOVE_HARD_BLOCK = cur

        def _patched_low_move_hard_block(entry_row: dict, *, symbol: str, source: str):
            row = entry_row if isinstance(entry_row, dict) else {}
            if not _source_is_summary_ai(source, row):
                return _ORIGINAL_LOW_MOVE_HARD_BLOCK(entry_row, symbol=symbol, source=source)

            repaired, diag = _repair_flat_range(row, symbol=str(symbol or ""), source=str(source or ""))
            if diag.get("repaired"):
                logger.warning("[LOW MOVE GUARD] SUMMARY_AI flat range repaired before strict guard detail=%s version=%s", diag, VERSION)
                try:
                    if isinstance(entry_row, dict):
                        entry_row.update({k: repaired[k] for k in ("high", "low", "high_price", "low_price") if k in repaired})
                except Exception:
                    pass
                return _ORIGINAL_LOW_MOVE_HARD_BLOCK(repaired, symbol=symbol, source=source)

            return _ORIGINAL_LOW_MOVE_HARD_BLOCK(entry_row, symbol=symbol, source=source)

        _patched_low_move_hard_block._summary_ai_flat_range_repair_v32 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._summary_ai_flat_range_repair_v33 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._summary_ai_flat_range_repair_v34 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._original = cur  # type: ignore[attr-defined]
        eob._low_move_hard_block = _patched_low_move_hard_block
        _ORDER_BUILDER_PATCHED = True
        logger.warning("[LOW MOVE GUARD] SUMMARY_AI flat range repair installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[LOW MOVE GUARD] SUMMARY_AI flat range repair install failed version=%s", VERSION)
        return False


def _entry_row_min_range_ok(vf: Any, entry_row: Any) -> tuple[bool, dict]:
    try:
        row = _row_dict(entry_row)
        symbol = str(row.get("symbol") or row.get("Symbol") or "")
        close = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
        high = _safe_float(_first(row, ("high_price", "high"), 0.0), 0.0)
        low = _safe_float(_first(row, ("low_price", "low"), 0.0), 0.0)
        min_pct = _safe_float(getattr(vf, "DEFAULT_ENTRY_ROW_RANGE_MIN_PCT", 0.006), 0.006)
        ratio = ((high - low) / close) if close > 0 and high >= low and high > 0 and low > 0 else 0.0
        return bool(ratio >= min_pct), {"symbol": symbol, "close": close, "high": high, "low": low, "ratio": ratio, "min_pct": min_pct}
    except Exception:
        return False, {"error": "entry_row_min_range_check_failed"}


def _install_summary_ai_entry_row_range_filter_patch() -> bool:
    global _VOL_FILTER_PATCHED, _ORIGINAL_RANGE_5M_ENTRY_ROW
    if _VOL_FILTER_PATCHED:
        return True
    try:
        from trading.filters import volatility_filter as vf

        cur = getattr(vf, "_range_5m_filter_from_entry_row", None)
        if not callable(cur):
            logger.warning("[LOW MOVE GUARD] volatility_filter._range_5m_filter_from_entry_row not callable version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_1m_range_bridge_v34", False):
            _VOL_FILTER_PATCHED = True
            return True

        _ORIGINAL_RANGE_5M_ENTRY_ROW = cur

        def _patched_range_5m_filter_from_entry_row(entry_row: Any, min_pct: float = None):
            row = _row_dict(entry_row)
            src = str(row.get("source") or row.get("entry_type") or row.get("pipeline_source") or "")
            try:
                if _source_is_summary_ai(src, row):
                    min_ok, diag = _entry_row_min_range_ok(vf, entry_row)
                    rescue_ok = False
                    try:
                        rescue_min = _safe_float(getattr(vf, "DEFAULT_RANKING_RESCUE_MIN_PCT", 0.008), 0.008)
                        rescue_ok = bool(vf._ranking_move_rescue(entry_row, min_pct=rescue_min, label="summary_ai_1m_range_bridge"))
                    except Exception:
                        rescue_ok = False
                    if min_ok and rescue_ok:
                        logger.warning(
                            "[LOW MOVE GUARD] SUMMARY_AI 1m range bridge allow symbol=%s ratio=%.6f min_pct=%.6f rescue_ok=%s version=%s",
                            diag.get("symbol"),
                            float(diag.get("ratio") or 0.0),
                            float(diag.get("min_pct") or 0.0),
                            rescue_ok,
                            VERSION,
                        )
                        return True
            except Exception:
                logger.debug("[LOW MOVE GUARD] SUMMARY_AI range bridge precheck failed", exc_info=True)
            if min_pct is None:
                return _ORIGINAL_RANGE_5M_ENTRY_ROW(entry_row)
            return _ORIGINAL_RANGE_5M_ENTRY_ROW(entry_row, min_pct=min_pct)

        _patched_range_5m_filter_from_entry_row._summary_ai_1m_range_bridge_v34 = True  # type: ignore[attr-defined]
        _patched_range_5m_filter_from_entry_row._original = cur  # type: ignore[attr-defined]
        vf._range_5m_filter_from_entry_row = _patched_range_5m_filter_from_entry_row
        _VOL_FILTER_PATCHED = True
        logger.warning("[LOW MOVE GUARD] SUMMARY_AI 1m volatility range bridge installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[LOW MOVE GUARD] SUMMARY_AI 1m volatility range bridge install failed version=%s", VERSION)
        return False


# execute_ai_ok_entries_bulk のrolling-retry(旧_patched_execute_ai_ok_entries_bulk)は
# trading/entry/summary_ai/executor.py 本体 (REV11) へインライン化済み。
# その際、rolling-retryが blowoff_prefilter の ai_results フィルタを一切呼ばずに
# バイパスしていた不具合も修正した（blowoffフィルタを常に先に通すよう修正）。


def _install_summary_ai_executor_rolling_retry() -> bool:
    global _EXECUTOR_PATCHED
    _EXECUTOR_PATCHED = True
    return True


def _install_blowoff_prefilter() -> bool:
    try:
        from core.startup.summary_ai_blowoff_prefilter_patch import install as _install
        ok = bool(_install())
        logger.warning("[LOW MOVE GUARD] chained SUMMARY_AI blowoff prefilter installed=%s version=%s", ok, VERSION)
        return ok
    except Exception:
        logger.exception("[LOW MOVE GUARD] chained SUMMARY_AI blowoff prefilter install failed")
        return False


def _set_timeout_defaults() -> None:
    os.environ.setdefault("ENTRY_EXECUTE_ORIG_TIMEOUT_SEC", "15")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_ROLLING_RETRY", "1")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_CANDIDATE_SCAN_LIMIT", "12")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_BATCH_SIZE", "3")


def install() -> bool:
    global _INSTALLED

    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS", "0")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER", "0")
    os.environ.setdefault("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", "1")
    _set_timeout_defaults()

    blowoff_ok = _install_blowoff_prefilter()
    range_repair_ok = _install_entry_order_range_repair()
    vf_bridge_ok = _install_summary_ai_entry_row_range_filter_patch()
    rolling_ok = _install_summary_ai_executor_rolling_retry()

    if not _env_bool("SUMMARY_AI_LOW_MOVE_SOFTPASS", False):
        _INSTALLED = bool(blowoff_ok and range_repair_ok and vf_bridge_ok and rolling_ok)
        logger.warning(
            "[LOW MOVE GUARD] SUMMARY_AI low move softpass disabled strict mode version=%s SUMMARY_AI_LOW_MOVE_SOFTPASS=%s watcher=%s blowoff_prefilter=%s range_repair=%s vf_bridge=%s rolling_retry=%s timeout=%s",
            VERSION,
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS"),
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER"),
            blowoff_ok,
            range_repair_ok,
            vf_bridge_ok,
            rolling_ok,
            os.getenv("ENTRY_EXECUTE_ORIG_TIMEOUT_SEC"),
        )
        return bool(blowoff_ok and range_repair_ok and vf_bridge_ok and rolling_ok)

    _INSTALLED = bool(blowoff_ok and range_repair_ok and vf_bridge_ok and rolling_ok)
    logger.warning(
        "[LOW MOVE GUARD] SUMMARY_AI low move softpass requested but implementation is disabled in strict build version=%s blowoff_prefilter=%s range_repair=%s vf_bridge=%s rolling_retry=%s timeout=%s",
        VERSION,
        blowoff_ok,
        range_repair_ok,
        vf_bridge_ok,
        rolling_ok,
        os.getenv("ENTRY_EXECUTE_ORIG_TIMEOUT_SEC"),
    )
    return bool(blowoff_ok and range_repair_ok and vf_bridge_ok and rolling_ok)


try:
    install()
except Exception:
    logger.exception("[LOW MOVE GUARD] SUMMARY_AI low move softpass strict stub auto install failed")


__all__ = ["VERSION", "install"]
