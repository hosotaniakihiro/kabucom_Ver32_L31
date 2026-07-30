# ============================================================
# File   : core/startup/summary_ai_async_entry_patch.py
# Version: Ver13-EXECUTOR-QUEUE-INLINED
# ------------------------------------------------------------
# SUMMARY AI の実発注を非同期化し、entry_controller の lock 競合時も
# retryable として扱う runtime patch。
#
# Ver13:
#   - execute_ai_ok_entries_bulk の sync/queue+worker ディスパッチ部分は
#     trading/entry/summary_ai/executor.py 本体 (REV11) へインライン化したため、
#     ここでの差し替えと専用queue/workerスレッドは撤去した
#     (execute_entry_pipeline の direct snapshot 差し替えは維持)。
#
# Ver12:
#   - reason_chain に entry_controller_lock_timeout が含まれる場合は、
#     上位に entry_pipeline_no_order / summary_entry_executor_no_order があっても
#     retryable=True と判定する。
#   - execute_entry_pipeline を direct snapshot 実行へ差し替え、
#     lock busy 時も reentrant 実行で候補評価・発注まで進める。
#   - queued_async は「未実行」だが worker で必ず実行する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_SUMMARY_ENTRY_PIPELINE = None

os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY", "1")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC", "0")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", "1")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", "20")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", "1")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", "3")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", "1.0")
os.environ.setdefault("SUMMARY_AI_DIRECT_ENTRY_SNAPSHOT", "1")
os.environ.setdefault("SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK", "1")

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return max(1, int(float(v)))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _symbols(rows: Any, limit: int = 20) -> list[str]:
    out: list[str] = []
    try:
        for r in list(rows or [])[:limit]:
            if isinstance(r, dict):
                sym = _norm_symbol(r.get("symbol"))
            else:
                sym = _norm_symbol(getattr(r, "symbol", ""))
            if sym:
                out.append(sym)
    except Exception:
        pass
    return out


def _entries_to_list(entries: Any) -> list[dict[str, Any]]:
    try:
        from trading.summary import summary_entry as se
        fn = getattr(se, "normalize_approved_rows", None)
        rows = fn(entries) if callable(fn) else list(entries or [])
        out: list[dict[str, Any]] = []
        for r in rows or []:
            if isinstance(r, dict):
                d = dict(r)
                sym = _norm_symbol(d.get("symbol"))
                if sym:
                    d["symbol"] = sym
                    out.append(d)
        return out
    except Exception:
        logger.exception("[SUMMARY AI DIRECT SNAPSHOT] entries normalize failed")
        return []


def _build_boost_active(ec: Any) -> bool:
    try:
        gd = getattr(ec, "global_data")
        return bool(ec.boost_engine.update(
            win_rate=getattr(gd, "recent_win_rate", 0.5),
            regime=getattr(gd, "current_regime", 0),
            drawdown=getattr(gd, "current_drawdown", 0.0),
            collapse_prob=getattr(gd, "collapse_prob", 0.0),
            consecutive_losses=getattr(gd, "consecutive_losses", 0),
            regime_changed=getattr(gd, "regime_changed", False),
        ))
    except Exception:
        return False


def _summary_ai_direct_snapshot_execute(entries: Any, *, pipeline_source: str | None = None, interval: int | None = None) -> dict[str, Any]:
    rows = _entries_to_list(entries)
    if not rows:
        return {"executed": False, "entries": 0, "skip_reason": "no_entries", "result": None}
    try:
        import trading.handlers.entry_controller as ec
        from trading.entry.pending_manager import pop_entry, snapshot_root
    except Exception:
        logger.exception("[SUMMARY AI DIRECT SNAPSHOT] import entry_controller failed")
        return {"executed": False, "entries": len(rows), "skip_reason": "entry_controller_import_failed", "retryable": True, "result": None}

    if not _env_bool("SUMMARY_AI_DIRECT_ENTRY_SNAPSHOT", True):
        if callable(_ORIG_SUMMARY_ENTRY_PIPELINE):
            return _ORIG_SUMMARY_ENTRY_PIPELINE(entries, pipeline_source=pipeline_source, interval=interval)
        return {"executed": False, "entries": len(rows), "skip_reason": "direct_snapshot_disabled_no_original", "result": None}

    lock = getattr(ec, "_pipeline_lock", None)
    acquired = False
    reentrant_lock = False
    try:
        if lock is not None:
            acquired = bool(lock.acquire(blocking=False))
            if not acquired:
                if _env_bool("SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK", True):
                    reentrant_lock = True
                    logger.warning("[SUMMARY AI DIRECT SNAPSHOT] entry_controller lock busy -> reentrant execution entries=%s symbols=%s", len(rows), _symbols(rows))
                else:
                    return {"executed": False, "entries": len(rows), "skip_reason": "entry_controller_lock_timeout", "retryable": True, "result": None, "pending_root": snapshot_root()}

        try:
            market_open = bool(ec.is_market_open()) and bool(ec._is_trading_hours())
        except Exception:
            market_open = True
        if not market_open:
            return {"executed": False, "entries": len(rows), "skip_reason": "market_closed", "result": None}

        if pipeline_source:
            pipeline_source = ec._normalize_source(pipeline_source)
        if interval is not None:
            interval = ec._normalize_interval(interval)

        for guard_name, guard_fn, retryable in (
            ("api_rate_limit", ec._api_rate_limited, True),
            ("ai_health_ng", lambda: not ec.ai_health_ok(), False),
            ("risk_guard_ng", lambda: not ec.risk_ok(), False),
            ("index_shock", lambda: ec.detect_index_shock() != 0, False),
        ):
            try:
                if guard_fn():
                    return {"executed": False, "entries": len(rows), "skip_reason": guard_name, "retryable": retryable, "result": None}
            except Exception:
                logger.debug("[SUMMARY AI DIRECT SNAPSHOT] guard failed name=%s", guard_name, exc_info=True)

        if not ec.allow_entry_by_market(now=dt.datetime.now(), nikkei_velocity=getattr(ec.global_data, "nikkei_velocity", None), api_429_count=getattr(ec.global_data, "api_429_count", 0), board_update_delay_sec=getattr(ec.global_data, "board_delay_sec", None)):
            return {"executed": False, "entries": len(rows), "skip_reason": "market_guard_ng", "result": None}

        ec.reset_entry_lock()
        boost_active = _build_boost_active(ec)
        open_position_symbols = ec._normalize_open_positions(getattr(ec.global_data, "open_positions", None))
        by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            sym = _norm_symbol(row.get("symbol"))
            if sym:
                row["symbol"] = sym
                by_symbol[sym].append(row)

        global_scored_candidates: list[dict[str, Any]] = []
        for symbol, bucket in by_symbol.items():
            try:
                scored = ec._build_scored_candidates(
                    symbol=symbol,
                    entries=sorted(bucket, key=lambda e: (ec.ENTRY_TYPE_PRIORITY.get(e.get("entry_type"), 0), ec._safe_float(e.get("score"), 0.0), e.get("created_at") or dt.datetime.min), reverse=True),
                    open_position_symbols=open_position_symbols,
                    boost_active=boost_active,
                    pipeline_source=pipeline_source,
                    interval=interval,
                )
                if scored:
                    global_scored_candidates.extend(scored)
            except Exception:
                logger.exception("[SUMMARY AI DIRECT SNAPSHOT] candidate build failed symbol=%s", symbol)

        if not global_scored_candidates:
            return {"executed": False, "entries": len(rows), "approved_count": 0, "skip_reason": "snapshot_no_ai_approved_candidates", "retryable": False, "result": None, "pending_root": snapshot_root()}

        global_scored_candidates.sort(key=lambda x: (x.get("priority_score", 0.0), x.get("confidence", 0.0)), reverse=True)
        max_per_run = int(getattr(ec, "MAX_APPROVED_PER_RUN", 3) or 3)
        approved_count = 0
        attempted_count = 0
        executed_symbols: set[str] = set()
        order_results: list[dict[str, Any]] = []
        logger.warning("[SUMMARY AI DIRECT SNAPSHOT] ranked total=%s top=%s pipeline_source=%s interval=%s reentrant_lock=%s", len(global_scored_candidates), [{"symbol": x.get("symbol"), "side": x.get("side"), "priority": round(ec._safe_float(x.get("priority_score"), 0.0), 4), "conf": round(ec._safe_float(x.get("confidence"), 0.0), 4)} for x in global_scored_candidates[:10]], pipeline_source, interval, reentrant_lock)

        for item in global_scored_candidates:
            if approved_count >= max_per_run:
                break
            symbol = item.get("symbol")
            side = item.get("side")
            entry = item.get("entry")
            if not symbol or symbol in executed_symbols or symbol in open_position_symbols:
                continue
            if ec._is_symbol_trade_restricted(symbol):
                continue
            if not ec.lock_symbol(symbol):
                continue
            attempted_count += 1
            ok = bool(ec._execute_best_candidate(item, boost_active=boost_active))
            order_results.append({"symbol": symbol, "side": side, "ok": ok})
            if not ok:
                continue
            approved_count += 1
            executed_symbols.add(symbol)
            try:
                pop_entry(symbol, entry)
            except Exception:
                logger.debug("[SUMMARY AI DIRECT SNAPSHOT] pop_entry failed symbol=%s", symbol, exc_info=True)

        executed = approved_count > 0
        out = {"executed": executed, "entries": len(rows), "approved_count": approved_count, "attempted_count": attempted_count, "result": order_results, "pipeline_source": pipeline_source, "interval": interval, "skip_reason": None if executed else "snapshot_no_order", "retryable": attempted_count == 0 and bool(snapshot_root()), "pending_root": snapshot_root(), "reentrant_lock": reentrant_lock}
        logger.warning("[SUMMARY AI DIRECT SNAPSHOT] done %s", out)
        return out
    except Exception as e:
        logger.exception("[SUMMARY AI DIRECT SNAPSHOT] failed err=%s", e)
        return {"executed": False, "entries": len(rows), "skip_reason": "snapshot_entry_exception", "error": str(e), "retryable": False}
    finally:
        try:
            if acquired and lock is not None:
                lock.release()
        except Exception:
            logger.debug("[SUMMARY AI DIRECT SNAPSHOT] lock release failed", exc_info=True)


def _install_summary_entry_snapshot_patch() -> bool:
    global _ORIG_SUMMARY_ENTRY_PIPELINE
    try:
        from trading.summary import summary_entry as se
        current = getattr(se, "execute_entry_pipeline", None)
        if getattr(current, "_summary_ai_direct_snapshot_v12", False):
            return True
        _ORIG_SUMMARY_ENTRY_PIPELINE = getattr(current, "_original", None) or current
        if not callable(_ORIG_SUMMARY_ENTRY_PIPELINE):
            logger.warning("[SUMMARY AI DIRECT SNAPSHOT] original summary entry pipeline missing")
            return False

        def _patched_execute_entry_pipeline(entries, *, pipeline_source: str | None = None, interval: int | None = None):
            return _summary_ai_direct_snapshot_execute(entries, pipeline_source=pipeline_source, interval=interval)

        _patched_execute_entry_pipeline._summary_ai_direct_snapshot_v12 = True  # type: ignore[attr-defined]
        _patched_execute_entry_pipeline._summary_ai_direct_snapshot_v11 = True  # type: ignore[attr-defined]
        _patched_execute_entry_pipeline._original = _ORIG_SUMMARY_ENTRY_PIPELINE  # type: ignore[attr-defined]
        se.execute_entry_pipeline = _patched_execute_entry_pipeline
        logger.warning("[SUMMARY AI DIRECT SNAPSHOT] installed v12 enabled=%s reentrant_lock=%s", _env_bool("SUMMARY_AI_DIRECT_ENTRY_SNAPSHOT", True), _env_bool("SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK", True))
        return True
    except Exception:
        logger.exception("[SUMMARY AI DIRECT SNAPSHOT] install failed")
        return False


# execute_ai_ok_entries_bulk の sync/queue+worker ディスパッチ
# (旧 _patched_execute_ai_ok_entries_bulk / _run_worker_loop) は
# trading/entry/summary_ai/executor.py 本体 (REV11) へインライン化済み。


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        if os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC") is None or str(os.getenv("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC")).strip() == "1":
            os.environ["SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"] = "0"
        os.environ["SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX"] = str(max(1, min(_env_int("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", 1), 3)))
        if _env_float("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", 20.0) > 20:
            os.environ["SUMMARY_AI_ASYNC_ENTRY_STALE_SEC"] = "20"
        _install_summary_entry_snapshot_patch()
        _INSTALLED = True
        logger.warning("[SUMMARY AI ASYNC ENTRY] installed v13 (executor dispatch inlined) direct_sync=%s snapshot_direct=%s reentrant_lock=%s", _env_bool("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC", False), _env_bool("SUMMARY_AI_DIRECT_ENTRY_SNAPSHOT", True), _env_bool("SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK", True))
        return True
    except Exception:
        logger.exception("[SUMMARY AI ASYNC ENTRY] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI ASYNC ENTRY] auto install failed")

__all__ = ["install"]
