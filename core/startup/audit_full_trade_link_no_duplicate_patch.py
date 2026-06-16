# -*- coding: utf-8 -*-
"""Avoid duplicate audit rows after audit_full_trade_link_patch.

The full-link patch expands record_* functions. This patch ensures the expanded
insert path is the only path used, so one event produces one audit row.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-AUDIT-FULL-LINK-NO-DUPLICATE"
_INSTALLED = False


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.audit_logging.recorder as rec
        from core.startup.audit_full_trade_link_patch import _insert_extra, _ensure_extra_schema, build_entry_id

        def ensure_audit_db_patched():
            _ensure_extra_schema(rec)

        def record_candidate_event(**kwargs):
            source = kwargs.get("source") or ""
            symbol = kwargs.get("symbol") or ""
            side = kwargs.get("side") or ""
            when = kwargs.get("datetime") or dt.datetime.now().isoformat(timespec="seconds")
            entry_id = kwargs.get("entry_id") or build_entry_id(source=source, symbol=symbol, side=side, when=when)
            _insert_extra(rec, "candidate_history", {
                "datetime": when,
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

        def record_filter_event(**kwargs):
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

        def record_order_event(**kwargs):
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

        def record_exit_decision(**kwargs):
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

        def record_position_state(**kwargs):
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
        rec.record_candidate_event = record_candidate_event
        rec.record_filter_event = record_filter_event
        rec.record_order_event = record_order_event
        rec.record_exit_decision = record_exit_decision
        rec.record_position_state = record_position_state
        ensure_audit_db_patched()
        _INSTALLED = True
        logger.warning("[AUDIT FULL LINK NO DUP] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[AUDIT FULL LINK NO DUP] install failed")
        return False


__all__ = ["VERSION", "install"]
