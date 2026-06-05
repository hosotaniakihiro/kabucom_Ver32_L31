from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _normalize_symbol(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _broker_has_symbol(symbol: str) -> tuple[bool, bool, dict]:
    """return (read_ok, has_open_symbol, status)."""
    symbol = _normalize_symbol(symbol)
    try:
        import trading.position.kabu_position_reader as reader
        rows = reader.read_kabu_open_positions() or {}
        status = reader.get_last_read_status() or {}
        read_ok = bool(status.get("ok", True))
        for k, v in (rows or {}).items():
            s = _normalize_symbol(k or (v or {}).get("symbol") or (v or {}).get("Symbol"))
            if s != symbol:
                continue
            qty = _safe_float((v or {}).get("qty") or (v or {}).get("quantity") or (v or {}).get("LeavesQty") or (v or {}).get("HoldQty"), 0.0)
            if qty > 0:
                return read_ok, True, status
        return read_ok, False, status
    except Exception as e:
        logger.warning("[EXIT DB STALE GUARD] broker check failed symbol=%s err=%s", symbol, e, exc_info=True)
        return False, False, {"ok": False, "error": str(e)}


def _mark_db_stale_closed(symbol: str, reason: str, detail: str = "") -> bool:
    try:
        from database import Session_position
        from database.models import Position
        session = Session_position()
        try:
            rows = session.query(Position).filter(Position.symbol == symbol).filter(Position.status == "OPEN").all()
            if not rows:
                return False
            now = dt.datetime.now()
            for p in rows:
                try:
                    p.status = "CLOSED"
                except Exception:
                    pass
                for attr, value in [
                    ("exit_time", now),
                    ("closed_time", now),
                    ("close_time", now),
                    ("updated_at", now),
                    ("exit_reason", reason),
                    ("close_reason", reason),
                    ("memo", detail),
                ]:
                    try:
                        if hasattr(p, attr):
                            setattr(p, attr, value)
                    except Exception:
                        pass
            session.commit()
            logger.warning("[EXIT DB STALE GUARD] DB stale OPEN rows marked CLOSED symbol=%s rows=%d reason=%s detail=%s", symbol, len(rows), reason, detail)
            return True
        except Exception:
            session.rollback()
            logger.exception("[EXIT DB STALE GUARD] DB stale close failed symbol=%s", symbol)
            return False
        finally:
            session.close()
    except Exception:
        logger.exception("[EXIT DB STALE GUARD] DB stale close unavailable symbol=%s", symbol)
        return False


def _remove_memory(symbol: str) -> None:
    try:
        from global_state import global_data
        d = getattr(global_data, "open_positions", None)
        if isinstance(d, dict):
            d.pop(symbol, None)
    except Exception:
        pass
    try:
        from core.global_context.context import global_context as GC
        positions_obj = getattr(GC, "positions", None)
        if positions_obj is not None:
            raw = getattr(positions_obj, "open_positions", None)
            if isinstance(raw, dict):
                raw.pop(symbol, None)
    except Exception:
        pass


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("EXIT_DB_STALE_POSITION_GUARD_ENABLED", True):
        logger.warning("[EXIT DB STALE GUARD] disabled by env")
        return False
    try:
        import trading.exit.executor as ex
        current_get = getattr(ex, "_get_open_position", None)
        current_send = getattr(ex, "_send_close_order", None)
        if not callable(current_get) or not callable(current_send):
            logger.warning("[EXIT DB STALE GUARD] executor internals unavailable")
            return False
        if getattr(current_get, "_exit_db_stale_guard_v1", False):
            _INSTALLED = True
            return True
        original_get = current_get
        original_send = current_send

        def _patched_get_open_position(symbol: str):
            pos, source = original_get(symbol)
            try:
                symbol_n = _normalize_symbol(symbol)
                source_s = str(source or (pos or {}).get("_position_source") or "")
                # DB fallbackで拾った建玉だけ、brokerに実建玉があるか再確認する。
                if pos is not None and "DB.positions" in source_s:
                    read_ok, has_broker, status = _broker_has_symbol(symbol_n)
                    if read_ok and not has_broker:
                        reason = "STALE_DB_POSITION_NO_BROKER_OPEN"
                        detail = f"broker_status={status} source={source_s}"
                        logger.error("[EXIT DB STALE GUARD] reject DB fallback stale position symbol=%s source=%s broker_status=%s", symbol_n, source_s, status)
                        if _env_bool("EXIT_DB_STALE_POSITION_AUTO_CLOSE", True):
                            _mark_db_stale_closed(symbol_n, reason, detail)
                        _remove_memory(symbol_n)
                        return None, "stale_db_position_no_broker_open"
            except Exception:
                logger.exception("[EXIT DB STALE GUARD] get guard failed symbol=%s", symbol)
            return pos, source

        def _patched_send_close_order(*, symbol: str, side: str, close_side: str, qty: float, exit_price: float, reason: str, position: dict[str, Any]):
            try:
                src = str((position or {}).get("_position_source") or "")
                # DB由来かつHoldIDなし/古いHoldIDの可能性があるものは、送信前にbroker一致を必須にする。
                if "DB.positions" in src:
                    read_ok, has_broker, status = _broker_has_symbol(symbol)
                    if read_ok and not has_broker:
                        stale_reason = "STALE_DB_POSITION_BLOCK_SEND_NO_BROKER_OPEN"
                        logger.error("[EXIT DB STALE GUARD] block close order for stale DB position symbol=%s src=%s broker_status=%s", symbol, src, status)
                        if _env_bool("EXIT_DB_STALE_POSITION_AUTO_CLOSE", True):
                            _mark_db_stale_closed(_normalize_symbol(symbol), stale_reason, f"broker_status={status} src={src}")
                        _remove_memory(_normalize_symbol(symbol))
                        return False, "", stale_reason
            except Exception:
                logger.exception("[EXIT DB STALE GUARD] send guard failed symbol=%s", symbol)
            return original_send(symbol=symbol, side=side, close_side=close_side, qty=qty, exit_price=exit_price, reason=reason, position=position)

        _patched_get_open_position._exit_db_stale_guard_v1 = True  # type: ignore[attr-defined]
        _patched_get_open_position._original = original_get  # type: ignore[attr-defined]
        _patched_send_close_order._exit_db_stale_guard_v1 = True  # type: ignore[attr-defined]
        _patched_send_close_order._original = original_send  # type: ignore[attr-defined]
        ex._get_open_position = _patched_get_open_position
        ex._send_close_order = _patched_send_close_order
        os.environ.setdefault("EXIT_DB_STALE_POSITION_GUARD_ENABLED", "1")
        os.environ.setdefault("EXIT_DB_STALE_POSITION_AUTO_CLOSE", "1")
        _INSTALLED = True
        logger.warning("[EXIT DB STALE GUARD] installed auto_close=%s", os.environ.get("EXIT_DB_STALE_POSITION_AUTO_CLOSE"))
        return True
    except Exception:
        logger.exception("[EXIT DB STALE GUARD] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[EXIT DB STALE GUARD] auto install failed")

__all__ = ["install"]