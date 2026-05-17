# ============================================================
# File   : trading/audit_logging/entry_audit.py
# Version: Ver01-ENTRY-AUDIT-HELPERS
# ------------------------------------------------------------
# Helper functions for writing entry pipeline audit records.
# These wrappers are intentionally safe: audit failure must not stop trading.
# ============================================================

import json
from datetime import datetime
from typing import Any

from .recorder import (
    record_candidate_event,
    record_filter_event,
    record_order_event,
)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == '':
            return default
        return int(float(v))
    except Exception:
        return default


def _safe_text(v: Any, max_len: int = 2000) -> str:
    try:
        if isinstance(v, str):
            s = v
        else:
            s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = str(v)
    return s[:max_len]


def audit_entry_skip(symbol: str, reason: str, detail: dict | None = None) -> None:
    try:
        record_filter_event(
            datetime=datetime.now().isoformat(timespec='seconds'),
            symbol=str(symbol),
            filter_name=str(reason),
            passed=False,
            detail=_safe_text(detail or {}),
        )
    except Exception:
        return


def audit_candidate_ok(symbol: str, side: str, entry_row: dict, ai: dict, ai_msg: str = '') -> None:
    try:
        score = _safe_float(entry_row.get('score'), 0.0)
        score_buy = _safe_float(entry_row.get('score_buy'), score)
        score_sell = _safe_float(entry_row.get('score_sell'), 0.0)
        final_score = _safe_float(
            entry_row.get('final_score')
            or entry_row.get('score_final')
            or entry_row.get('score_total')
            or score,
            score,
        )

        record_candidate_event(
            datetime=str(
                entry_row.get('datetime')
                or entry_row.get('dt')
                or datetime.now().isoformat(timespec='seconds')
            ),
            symbol=str(symbol),
            side=str(side).upper(),
            source=str(entry_row.get('source') or ''),
            interval_min=_safe_int(entry_row.get('interval'), 0),
            score_buy=score_buy,
            score_sell=score_sell,
            score_total=_safe_float(entry_row.get('score_total'), score),
            final_score=final_score,
            ai_result='AI_OK',
            reason=_safe_text({
                'ai_reason': ai.get('reason') if isinstance(ai, dict) else '',
                'ai_msg': ai_msg,
                'confidence': ai.get('confidence') if isinstance(ai, dict) else None,
            }),
        )
    except Exception:
        return


def audit_order(symbol: str, side: str, qty: int, order_type: str, price: Any, status: str, order_id: str | None = None, reason: str | None = None) -> None:
    try:
        record_order_event(
            datetime=datetime.now().isoformat(timespec='seconds'),
            symbol=str(symbol),
            side=str(side).upper(),
            qty=_safe_int(qty, 0),
            order_type=str(order_type or ''),
            order_id=str(order_id or ''),
            status=str(status or ''),
            price=_safe_float(price, 0.0),
            filled_price=None,
            cancel_reason=reason,
        )
    except Exception:
        return
