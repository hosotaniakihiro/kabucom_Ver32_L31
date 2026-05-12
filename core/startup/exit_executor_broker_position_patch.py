# ============================================================
# File   : core/startup/exit_executor_broker_position_patch.py
# Version: V1.0-BROKER-CREDIT-FALLBACK
# ------------------------------------------------------------
# Purpose:
#   EXIT_EXECUTOR が global_data.open_positions / GC.positions で
#   建玉を見つけられない場合に、kabu Station /positions?product=2
#   の信用建玉から直接復元してEXIT注文へ進ませる。
#
#   broker実建玉を正としているため、positions.dbにDB行が無い場合でも
#   返済注文成功後はDB close失敗を致命扱いしない。
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_GET_OPEN_POSITION = None
_ORIGINAL_CLOSE_DB_POSITION = None


def _normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _ensure_global_open_positions() -> dict[str, Any]:
    try:
        from global_state import global_data

        d = getattr(global_data, "open_positions", None)
        if isinstance(d, dict):
            return d
        d = {}
        setattr(global_data, "open_positions", d)
        return d
    except Exception:
        return {}


def _read_broker_position(symbol: str) -> Optional[dict[str, Any]]:
    symbol = _normalize_symbol(symbol)
    if not symbol:
        return None

    try:
        from trading.position.kabu_position_reader import read_kabu_open_positions

        rows = read_kabu_open_positions() or {}
        pos = rows.get(symbol) or rows.get(str(symbol))
        if isinstance(pos, dict):
            pos.setdefault("symbol", symbol)
            pos.setdefault("status", "OPEN")
            pos.setdefault("_position_source", "KABU.positions.credit_only.direct_exit_fallback")
            return pos
    except Exception:
        logger.warning("[EXIT EXECUTOR BROKER PATCH] broker position read failed symbol=%s", symbol, exc_info=True)
    return None


def _is_broker_credit_memory_position(symbol: str) -> bool:
    symbol = _normalize_symbol(symbol)
    try:
        positions = _ensure_global_open_positions()
        pos = positions.get(symbol) or positions.get(str(symbol))
        if not isinstance(pos, dict):
            return False
        src = str(pos.get("_position_source") or "").upper()
        return src.startswith("KABU.POSITIONS") or "CREDIT_ONLY" in src
    except Exception:
        return False


def install() -> bool:
    global _INSTALLED, _ORIGINAL_GET_OPEN_POSITION, _ORIGINAL_CLOSE_DB_POSITION

    if _INSTALLED:
        return True

    try:
        import trading.exit.executor as ex
    except Exception:
        logger.exception("[EXIT EXECUTOR BROKER PATCH] import executor failed")
        return False

    original_get = getattr(ex, "_get_open_position", None)
    original_close_db = getattr(ex, "_close_db_position", None)

    if not callable(original_get):
        logger.warning("[EXIT EXECUTOR BROKER PATCH] _get_open_position not callable")
        return False
    if not callable(original_close_db):
        logger.warning("[EXIT EXECUTOR BROKER PATCH] _close_db_position not callable")
        return False

    _ORIGINAL_GET_OPEN_POSITION = original_get
    _ORIGINAL_CLOSE_DB_POSITION = original_close_db

    def patched_get_open_position(symbol: str):
        pos, source = original_get(symbol)
        if pos is not None:
            return pos, source

        s = _normalize_symbol(symbol)
        broker_pos = _read_broker_position(s)
        if isinstance(broker_pos, dict):
            positions = _ensure_global_open_positions()
            positions[s] = broker_pos
            logger.warning(
                "[EXIT EXECUTOR BROKER PATCH] position recovered from broker credit positions symbol=%s",
                s,
            )
            return broker_pos, "KABU.positions.credit_only.direct_exit_fallback"

        return None, ""

    def patched_close_db_position(*, symbol: str, exit_price: float, pnl: float, reason: str, order_id: str) -> bool:
        ok = original_close_db(
            symbol=symbol,
            exit_price=exit_price,
            pnl=pnl,
            reason=reason,
            order_id=order_id,
        )
        if ok:
            return True

        # broker実建玉から返済した場合、positions.dbに行が無いことがある。
        # 注文はすでに受け付け後なので、DB未存在だけでEXIT失敗扱いにしない。
        if _is_broker_credit_memory_position(symbol):
            logger.warning(
                "[EXIT EXECUTOR BROKER PATCH] DB close missing ignored for broker credit position symbol=%s order_id=%s",
                symbol,
                order_id,
            )
            return True

        return False

    ex._get_open_position = patched_get_open_position
    ex._close_db_position = patched_close_db_position

    _INSTALLED = True
    logger.warning("[EXIT EXECUTOR BROKER PATCH] installed broker_credit_fallback=True db_missing_ok=True")
    return True


__all__ = ["install"]
