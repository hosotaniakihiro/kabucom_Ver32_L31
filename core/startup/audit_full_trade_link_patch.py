# -*- coding: utf-8 -*-
"""Runtime patch to enrich audit DB records with entry links and snapshots.

This keeps existing trading code compatible while adding richer audit columns:
entry_id, reason, entry_source/entry_mode, ranking metadata, and technical
snapshots on candidate/order/exit/position-state rows.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-AUDIT-FULL-TRADE-LINK"
_INSTALLED = False
_ORIG_ENSURE = None
_ORIG_CANDIDATE = None
_ORIG_FILTER = None
_ORIG_ORDER = None
_ORIG_EXIT = None
_ORIG_STATE = None


def _env_on(name: str, default: bool = True) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _safe_text(v: Any, max_len: int = 20000) -> str:
    try:
        if v is None:
            return ""
        if isinstance(v, str):
            s = v
        else:
            s = json.dumps(v, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        try:
            s = str(v)
        except Exception:
            s = ""
    return s[:max_len] if max_len and max_len > 0 else s


def build_entry_id(*, source: Any = "", symbol: Any = "", side: Any = "", when: Any = "") -> str:
    src = str(source or "UNKNOWN").strip().upper() or "UNKNOWN"
    sym = _norm_symbol(symbol) or "UNKNOWN"
    sd = str(side or "").strip().upper() or "NA"
    w = str(when or dt.datetime.now().isoformat(timespec="seconds"))
    keep = "".join(ch for ch in w if ch.isdigit())[:14] or dt.datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{src}-{keep}-{sym}-{sd}"


def _audit_db_path(rec) -> str:
    try:
        return str(rec.get_audit_db_path())
    except Exception:
        base = os.environ.get("AUDIT_DB_DIR", r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit")
        return os.path.join(base, f"audit_{dt.datetime.now().strftime('%Y%m%d')}.db")


def _ensure_col(cur, table: str, col: str, ddl: str) -> None:
    try:
        cols = {str(r[1]) for r in cur.execute(f'PRAGMA table_info("{table}")').fetchall()}
        if col not in cols:
            cur.execute(f'ALTER TABLE "{table}" ADD COLUMN {ddl}')
    except Exception:
        logger.debug("[AUDIT FULL LINK] ensure col failed table=%s col=%s", table, col, exc_info=True)


def _ensure_extra_schema(rec) -> None:
    try:
        path = _audit_db_path(rec)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with sqlite3.connect(path, timeout=30) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA busy_timeout=30000")
            extras = {
                "candidate_history": {
                    "entry_id": "entry_id TEXT",
                    "reason_code": "reason_code TEXT",
                    "ranking_type": "ranking_type TEXT",
                    "rank_position": "rank_position INTEGER",
                    "ranking_snapshot_time": "ranking_snapshot_time TEXT",
                    "technical_snapshot": "technical_snapshot TEXT",
                },
                "filter_history": {
                    "entry_id": "entry_id TEXT",
                    "source": "source TEXT",
                    "side": "side TEXT",
                },
                "order_history": {
                    "entry_id": "entry_id TEXT",
                    "reason": "reason TEXT",
                    "entry_source": "entry_source TEXT",
                    "entry_mode": "entry_mode TEXT",
                    "technical_snapshot": "technical_snapshot TEXT",
                },
                "exit_history": {
                    "entry_id": "entry_id TEXT",
                    "pnl": "pnl REAL",
                    "pnl_pct": "pnl_pct REAL",
                    "holding_seconds": "holding_seconds REAL",
                    "technical_snapshot": "technical_snapshot TEXT",
                },
                "position_state_history": {
                    "entry_id": "entry_id TEXT",
                    "current_price": "current_price REAL",
                    "pnl_pct": "pnl_pct REAL",
                    "technical_snapshot": "technical_snapshot TEXT",
                },
            }
            for table, cols in extras.items():
                for col, ddl in cols.items():
                    _ensure_col(cur, table, col, ddl)
            for sql in (
                "CREATE INDEX IF NOT EXISTS idx_candidate_history_entry_id ON candidate_history(entry_id)",
                "CREATE INDEX IF NOT EXISTS idx_order_history_entry_id ON order_history(entry_id)",
                "CREATE INDEX IF NOT EXISTS idx_exit_history_entry_id ON exit_history(entry_id)",
                "CREATE INDEX IF NOT EXISTS idx_position_state_history_entry_id ON position_state_history(entry_id)",
            ):
                try:
                    cur.execute(sql)
                except Exception:
                    pass
            conn.commit()
    except Exception:
        logger.debug("[AUDIT FULL LINK] extra schema failed", exc_info=True)


def _insert_extra(rec, table: str, values: dict[str, Any]) -> None:
    try:
        _ensure_extra_schema(rec)
        path = _audit_db_path(rec)
        with sqlite3.connect(path, timeout=30) as conn:
            cols_all = {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
            vals = {k: v for k, v in values.items() if k in cols_all}
            if not vals:
                return
            cols = list(vals.keys())
            ph = ",".join(["?"] * len(cols))
            sql = f'INSERT INTO "{table}" ({",".join(cols)}) VALUES ({ph})'
            conn.execute(sql, tuple(vals[c] for c in cols))
            conn.commit()
    except Exception:
        logger.debug("[AUDIT FULL LINK] insert extra failed table=%s", table, exc_info=True)


def install() -> bool:
    global _INSTALLED, _ORIG_ENSURE, _ORIG_CANDIDATE, _ORIG_FILTER, _ORIG_ORDER, _ORIG_EXIT, _ORIG_STATE
    if _INSTALLED:
        return True
    if not _env_on("AUDIT_FULL_TRADE_LINK_PATCH_ENABLED", True):
        logger.warning("[AUDIT FULL LINK] disabled by env")
        return False
    try:
        import trading.audit_logging.recorder as rec

        _ORIG_ENSURE = getattr(rec, "ensure_audit_db", None)
        _ORIG_CANDIDATE = getattr(rec, "record_candidate_event", None)
        _ORIG_FILTER = getattr(rec, "record_filter_event", None)
        _ORIG_ORDER = getattr(rec, "record_order_event", None)
        _ORIG_EXIT = getattr(rec, "record_exit_decision", None)
        _ORIG_STATE = getattr(rec, "record_position_state", None)

        def ensure_audit_db_patched():
            if callable(_ORIG_ENSURE):
                _ORIG_ENSURE()
            _ensure_extra_schema(rec)

        def record_candidate_event_patched(**kwargs):
            if callable(_ORIG_CANDIDATE):
                _ORIG_CANDIDATE(**kwargs)
            source = kwargs.get("source") or ""
            symbol = kwargs.get("symbol") or ""
            side = kwargs.get("side") or ""
            when = kwargs.get("datetime") or ""
            entry_id = kwargs.get("entry_id") or build_entry_id(source=source, symbol=symbol, side=side, when=when)
            _insert_extra(rec, "candidate_history", {
                "datetime": when or dt.datetime.now().isoformat(timespec="seconds"),
                "symbol": _norm_symbol(symbol),
                "side": str(side or "").upper(),
                "source": source,
                "interval_min": kwargs.get("interval_min"),
                "score_buy": kwargs.get("score_buy"),
                "score_sell": kwargs.get("score_sell"),
                "score_total": kwargs.get("score_total"),
                "final_score": kwargs.get("final_score"),
                "ai_result": kwargs.get("ai_result"),
                "reason": kwargs.get("reason"),
                "technical_snapshot": kwargs.get("technical_snapshot"),
                "entry_id": entry_id,
                "reason_code": kwargs.get("reason_code"),
                "ranking_type": kwargs.get("ranking_type"),
                "rank_position": kwargs.get("rank_position"),
                "ranking_snapshot_time": kwargs.get("ranking_snapshot_time"),
                "created_at": dt.datetime.now().isoformat(),
            })

        def record_filter_event_patched(**kwargs):
            if callable(_ORIG_FILTER):
                _ORIG_FILTER(**kwargs)
            _insert_extra(rec, "filter_history", {
                "datetime": kwargs.get("datetime") or dt.datetime.now().isoformat(timespec="seconds"),
                "symbol": _norm_symbol(kwargs.get("symbol")),
                "filter_name": kwargs.get("filter_name"),
                "passed": 1 if kwargs.get("passed") else 0,
                "detail": kwargs.get("detail"),
                "entry_id": kwargs.get("entry_id"),
                "source": kwargs.get("source"),
                "side": kwargs.get("side"),
                "created_at": dt.datetime.now().isoformat(),
            })

        def record_order_event_patched(**kwargs):
            if callable(_ORIG_ORDER):
                _ORIG_ORDER(**kwargs)
            reason = kwargs.get("reason") or kwargs.get("cancel_reason")
            _insert_extra(rec, "order_history", {
                "datetime": kwargs.get("datetime") or dt.datetime.now().isoformat(timespec="seconds"),
                "symbol": _norm_symbol(kwargs.get("symbol")),
                "side": str(kwargs.get("side") or "").upper(),
                "qty": kwargs.get("qty"),
                "order_type": kwargs.get("order_type"),
                "order_id": kwargs.get("order_id"),
                "status": kwargs.get("status"),
                "price": kwargs.get("price"),
                "filled_price": kwargs.get("filled_price"),
                "cancel_reason": kwargs.get("cancel_reason") or reason,
                "entry_id": kwargs.get("entry_id"),
                "reason": reason,
                "entry_source": kwargs.get("entry_source") or kwargs.get("source"),
                "entry_mode": kwargs.get("entry_mode"),
                "technical_snapshot": kwargs.get("technical_snapshot"),
                "created_at": dt.datetime.now().isoformat(),
            })

        def record_exit_decision_patched(**kwargs):
            if callable(_ORIG_EXIT):
                _ORIG_EXIT(**kwargs)
            _insert_extra(rec, "exit_history", {
                "datetime": kwargs.get("datetime") or dt.datetime.now().isoformat(timespec="seconds"),
                "symbol": _norm_symbol(kwargs.get("symbol")),
                "side": str(kwargs.get("side") or "").upper(),
                "entry_price": kwargs.get("entry_price"),
                "current_price": kwargs.get("current_price"),
                "highest_since_entry": kwargs.get("highest_since_entry"),
                "lowest_since_entry": kwargs.get("lowest_since_entry"),
                "exit_reason": kwargs.get("exit_reason"),
                "triggered": 1 if kwargs.get("triggered") else 0,
                "entry_id": kwargs.get("entry_id"),
                "pnl": kwargs.get("pnl"),
                "pnl_pct": kwargs.get("pnl_pct"),
                "holding_seconds": kwargs.get("holding_seconds"),
                "technical_snapshot": kwargs.get("technical_snapshot"),
                "created_at": dt.datetime.now().isoformat(),
            })

        def record_position_state_patched(**kwargs):
            if callable(_ORIG_STATE):
                _ORIG_STATE(**kwargs)
            _insert_extra(rec, "position_state_history", {
                "datetime": kwargs.get("datetime") or dt.datetime.now().isoformat(timespec="seconds"),
                "symbol": _norm_symbol(kwargs.get("symbol")),
                "side": str(kwargs.get("side") or "").upper(),
                "qty": kwargs.get("qty"),
                "entry_price": kwargs.get("entry_price"),
                "highest_since_entry": kwargs.get("highest_since_entry"),
                "lowest_since_entry": kwargs.get("lowest_since_entry"),
                "holding_seconds": kwargs.get("holding_seconds"),
                "entry_id": kwargs.get("entry_id"),
                "current_price": kwargs.get("current_price"),
                "pnl_pct": kwargs.get("pnl_pct"),
                "technical_snapshot": kwargs.get("technical_snapshot"),
                "created_at": dt.datetime.now().isoformat(),
            })

        rec.ensure_audit_db = ensure_audit_db_patched
        rec.record_candidate_event = record_candidate_event_patched
        rec.record_filter_event = record_filter_event_patched
        rec.record_order_event = record_order_event_patched
        rec.record_exit_decision = record_exit_decision_patched
        rec.record_position_state = record_position_state_patched
        ensure_audit_db_patched()
        _INSTALLED = True
        logger.warning("[AUDIT FULL LINK] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[AUDIT FULL LINK] install failed")
        return False


__all__ = ["VERSION", "install", "build_entry_id"]
