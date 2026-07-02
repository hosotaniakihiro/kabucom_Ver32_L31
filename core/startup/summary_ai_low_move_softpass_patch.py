# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_low_move_softpass_patch.py
# Version: V3.3-STRICT-RANGE-REPAIR-EXECUTOR-ROLLING-RETRY
# ------------------------------------------------------------
# Purpose:
#   SUMMARY_AI の低ATR/低レンジ soft-pass は既定で無効のまま維持する。
#
# Important:
#   - 低出来高・低変動銘柄を緩和せず排除する運用では、soft-pass は不要。
#   - ただし main 1m の最新行だけで entry_order_builder に渡ると、
#     high == low == close になり、実際には日中レンジがある銘柄まで
#     LOW_MOVE_RANGE_TOO_SMALL で落ちることがある。
#   - また、承認済み候補が7件以上あっても、先頭Top3が最終ガードNGだと
#     executor がそこで no-order 終了して次候補へ進まない。
#   - この V3.3 はガードを緩和しない。低変動NGは維持し、Top3全滅時だけ
#     次の承認候補バッチへ繰り上げる。
#
# V3.3:
#   - entry_order_builder._low_move_hard_block をラップ。
#   - SUMMARY_AI の high/low が flat の場合だけ、day_high/day_low,
#     intraday_high/intraday_low, range_high/range_low 等で補完。
#   - 補完できない場合は従来通り LOW_MOVE_RANGE_TOO_SMALL を維持。
#   - summary_ai.executor.execute_ai_ok_entries_bulk をラップし、Top3 no-order 時に
#     次の承認済み候補へ進む。
#   - ENTRY_EXECUTE_ORIG_TIMEOUT_SEC は 8秒だと board retry + order build で
#     誤timeoutになりやすいため、既定だけ 15秒へ引き上げる。
# ============================================================
from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V3.3-STRICT-RANGE-REPAIR-EXECUTOR-ROLLING-RETRY"
_INSTALLED = False
_ORDER_BUILDER_PATCHED = False
_EXECUTOR_PATCHED = False
_ORIGINAL_LOW_MOVE_HARD_BLOCK = None
_ORIGINAL_EXECUTE_AI_OK_ENTRIES_BULK = None

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
        if getattr(cur, "_summary_ai_flat_range_repair_v32", False) or getattr(cur, "_summary_ai_flat_range_repair_v33", False):
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
        _patched_low_move_hard_block._summary_ai_flat_range_repair_v33 = True  # type: ignore[attr-defined]
        _patched_low_move_hard_block._original = cur  # type: ignore[attr-defined]
        eob._low_move_hard_block = _patched_low_move_hard_block
        _ORDER_BUILDER_PATCHED = True
        logger.warning("[LOW MOVE GUARD] SUMMARY_AI flat range repair installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[LOW MOVE GUARD] SUMMARY_AI flat range repair install failed version=%s", VERSION)
        return False


def _batch_size(default: int = 3) -> int:
    return max(1, min(_env_int("SUMMARY_AI_EXECUTOR_BATCH_SIZE", default), 3))


def _install_summary_ai_executor_rolling_retry() -> bool:
    global _EXECUTOR_PATCHED, _ORIGINAL_EXECUTE_AI_OK_ENTRIES_BULK
    if _EXECUTOR_PATCHED:
        return True
    try:
        import trading.entry.summary_ai.executor as exec_mod

        cur = getattr(exec_mod, "execute_ai_ok_entries_bulk", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI EXECUTOR ROLLING] target not callable version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_executor_rolling_retry_v33", False):
            _EXECUTOR_PATCHED = True
            return True

        _ORIGINAL_EXECUTE_AI_OK_ENTRIES_BULK = cur

        def _patched_execute_ai_ok_entries_bulk(
            ai_results,
            *,
            df_summary,
            interval=1,
            max_entries=3,
            dry_run=True,
            require_market_open=True,
            entry_pipeline=None,
        ):
            if not _env_bool("SUMMARY_AI_EXECUTOR_ROLLING_RETRY", True):
                return _ORIGINAL_EXECUTE_AI_OK_ENTRIES_BULK(
                    ai_results,
                    df_summary=df_summary,
                    interval=interval,
                    max_entries=max_entries,
                    dry_run=dry_run,
                    require_market_open=require_market_open,
                    entry_pipeline=entry_pipeline,
                )
            try:
                ok_items = [x for x in ai_results if isinstance(x, dict) and bool(x.get("allow"))]
                kept = exec_mod._filter_blocked_ai_ok_items(ok_items)
                if not kept:
                    return {"executed": False, "dry_run": dry_run, "approved_rows": [], "result": None, "skip_reason": "no_ai_ok"}
                if require_market_open and not exec_mod.is_market_open():
                    approved_preview = [exec_mod.build_approved_row(x) for x in sorted(kept, key=exec_mod._sort_key, reverse=True)[:_batch_size(max_entries)]]
                    return {"executed": False, "dry_run": dry_run, "approved_rows": approved_preview, "result": None, "skip_reason": "market_closed"}
                if dry_run:
                    approved_preview = [exec_mod.build_approved_row(x) for x in sorted(kept, key=exec_mod._sort_key, reverse=True)[:_batch_size(max_entries)]]
                    return {"executed": False, "dry_run": True, "approved_rows": approved_preview, "result": None, "skip_reason": "dry_run"}
                if entry_pipeline is None:
                    entry_pipeline = exec_mod.get_bulk_entry_pipeline()
                if entry_pipeline is None:
                    return {"executed": False, "dry_run": False, "approved_rows": [], "result": None, "skip_reason": "entry_pipeline_not_found"}

                batch_n = _batch_size(max_entries)
                scan_limit = max(batch_n, _env_int("SUMMARY_AI_EXECUTOR_CANDIDATE_SCAN_LIMIT", 12))
                ordered = sorted(kept, key=exec_mod._sort_key, reverse=True)[:scan_limit]
                all_rows = []
                attempts = []
                logger.warning(
                    "[SUMMARY AI EXECUTOR ROLLING] start ok_total=%s kept=%s scan=%s batch=%s interval=%s version=%s",
                    len(ok_items),
                    len(kept),
                    len(ordered),
                    batch_n,
                    interval,
                    VERSION,
                )

                for start in range(0, len(ordered), batch_n):
                    batch_items = ordered[start:start + batch_n]
                    approved_rows = [exec_mod.build_approved_row(x) for x in batch_items]
                    all_rows.extend(approved_rows)
                    symbols = [str(x.get("symbol")) for x in approved_rows]
                    logger.warning(
                        "[SUMMARY AI EXECUTOR ROLLING] batch start offset=%s size=%s symbols=%s",
                        start,
                        len(approved_rows),
                        symbols,
                    )
                    result = entry_pipeline(approved_rows, df_summary, interval)
                    executed = exec_mod._positive_result(result)
                    attempts.append({"offset": start, "symbols": symbols, "executed": executed, "result": exec_mod._summarize_no_order_result(result)})
                    if executed:
                        logger.warning("[SUMMARY AI EXECUTOR ROLLING] executed offset=%s symbols=%s result=%s", start, symbols, result)
                        return {
                            "executed": True,
                            "dry_run": False,
                            "approved_rows": all_rows,
                            "result": result,
                            "skip_reason": None,
                            "attempts": attempts,
                            "rolling_retry": True,
                        }
                    logger.warning(
                        "[SUMMARY AI EXECUTOR ROLLING] batch no-order offset=%s symbols=%s detail=%s",
                        start,
                        symbols,
                        exec_mod._summarize_no_order_result(result),
                    )

                removed_pending = 0
                if all_rows:
                    try:
                        removed_pending = exec_mod._cleanup_pending_after_no_order(attempts[-1].get("result") if attempts else None, all_rows, reason="entry_pipeline_no_order_all_batches")
                    except Exception:
                        logger.exception("[SUMMARY AI EXECUTOR ROLLING] final pending cleanup failed")
                return {
                    "executed": False,
                    "dry_run": False,
                    "approved_rows": all_rows,
                    "result": attempts[-1].get("result") if attempts else None,
                    "skip_reason": "entry_pipeline_no_order_all_batches",
                    "attempts": attempts,
                    "pending_removed": removed_pending,
                    "rolling_retry": True,
                }
            except Exception:
                logger.exception("[SUMMARY AI EXECUTOR ROLLING] patched executor failed; fallback original version=%s", VERSION)
                return _ORIGINAL_EXECUTE_AI_OK_ENTRIES_BULK(
                    ai_results,
                    df_summary=df_summary,
                    interval=interval,
                    max_entries=max_entries,
                    dry_run=dry_run,
                    require_market_open=require_market_open,
                    entry_pipeline=entry_pipeline,
                )

        _patched_execute_ai_ok_entries_bulk._summary_ai_executor_rolling_retry_v33 = True  # type: ignore[attr-defined]
        _patched_execute_ai_ok_entries_bulk._original = cur  # type: ignore[attr-defined]
        exec_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        _EXECUTOR_PATCHED = True
        logger.warning("[SUMMARY AI EXECUTOR ROLLING] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR ROLLING] install failed version=%s", VERSION)
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


def _set_timeout_defaults() -> None:
    # 8秒だと board retry + order build の途中で execute_orig_timeout になりやすい。
    # ユーザーの stale/低変動ガードは緩めず、発注処理の待ち時間だけ既定値を安全側にする。
    os.environ.setdefault("ENTRY_EXECUTE_ORIG_TIMEOUT_SEC", "15")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_ROLLING_RETRY", "1")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_CANDIDATE_SCAN_LIMIT", "12")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_BATCH_SIZE", "3")


def install() -> bool:
    """
    Strict mode:
      - デフォルトでは SUMMARY_AI 低変動 soft-pass を一切入れない。
      - watcher も起動しない。
      - low-move 判定そのものは維持する。
      - high/low が latest 1本で flat になった場合だけ、既存の day range 情報で補正する。
      - Top3 全滅時は、承認済み次候補へ繰り上げる。
    """
    global _INSTALLED

    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS", "0")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER", "0")
    os.environ.setdefault("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", "1")
    _set_timeout_defaults()

    blowoff_ok = _install_blowoff_prefilter()
    range_repair_ok = _install_entry_order_range_repair()
    rolling_ok = _install_summary_ai_executor_rolling_retry()

    if not _env_bool("SUMMARY_AI_LOW_MOVE_SOFTPASS", False):
        _INSTALLED = bool(blowoff_ok and range_repair_ok and rolling_ok)
        logger.warning(
            "[LOW MOVE GUARD] SUMMARY_AI low move softpass disabled strict mode version=%s "
            "SUMMARY_AI_LOW_MOVE_SOFTPASS=%s watcher=%s blowoff_prefilter=%s range_repair=%s rolling_retry=%s timeout=%s",
            VERSION,
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS"),
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER"),
            blowoff_ok,
            range_repair_ok,
            rolling_ok,
            os.getenv("ENTRY_EXECUTE_ORIG_TIMEOUT_SEC"),
        )
        return bool(blowoff_ok and range_repair_ok and rolling_ok)

    _INSTALLED = bool(blowoff_ok and range_repair_ok and rolling_ok)
    logger.warning(
        "[LOW MOVE GUARD] SUMMARY_AI low move softpass requested but implementation is disabled in strict build version=%s blowoff_prefilter=%s range_repair=%s rolling_retry=%s timeout=%s",
        VERSION,
        blowoff_ok,
        range_repair_ok,
        rolling_ok,
        os.getenv("ENTRY_EXECUTE_ORIG_TIMEOUT_SEC"),
    )
    return bool(blowoff_ok and range_repair_ok and rolling_ok)


try:
    install()
except Exception:
    logger.exception("[LOW MOVE GUARD] SUMMARY_AI low move softpass strict stub auto install failed")


__all__ = ["VERSION", "install"]
