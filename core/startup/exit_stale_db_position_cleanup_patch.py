# ============================================================
# File   : core/startup/exit_stale_db_position_cleanup_patch.py
# Version: V1-STALE-DB-POSITION-CLEANUP
# ------------------------------------------------------------
# Purpose:
#   DB上はOPENだが、証券会社側に建玉が存在しない古い建玉を自動クリーンアップする。
#
# 背景:
#   ログで 9716 が positions.db 上は OPEN のため EXIT対象になるが、
#   kabu Station /sendorder が
#     Code=1009001 Message=建玉が選択されていません。
#   を返して返済できない状態が繰り返されていた。
#
# 方針:
#   - execute_exit() が失敗した後、kabu_api.send_order の直近エラーを確認。
#   - 「建玉が選択されていません」「決済指定内容に誤りがあります」は、
#     証券会社側に返済対象建玉が無い可能性が高い。
#   - その場合だけ positions.db の OPEN/CLOSING を STALE_CLOSED に変更し、
#     メモリ上の open_positions からも除外する。
#   - 新規の実建玉を消さないよう、通常の注文拒否や通信エラーでは動かない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False

_STALE_CODES = {"1009001", "8"}
_STALE_MESSAGE_PARTS = (
    "建玉が選択されていません",
    "決済指定内容に誤りがあります",
    "返済対象建玉",
    "ClosePositions",
)


def _normalize_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _latest_order_error() -> dict[str, Any]:
    try:
        from kabu_api.send_order import get_last_send_order_error
        e = get_last_send_order_error()
        return e if isinstance(e, dict) else {}
    except Exception:
        return {}


def _is_stale_no_broker_position_error(symbol: str, err: dict[str, Any], order_message: Any = None) -> bool:
    try:
        sym = _normalize_symbol(err.get("symbol"))
        if sym and sym != _normalize_symbol(symbol):
            return False
        code = str(err.get("code") or err.get("Code") or "").strip()
        msg = str(err.get("message") or err.get("Message") or order_message or err.get("raw") or "")
        if code in _STALE_CODES:
            return True
        return any(part in msg for part in _STALE_MESSAGE_PARTS)
    except Exception:
        return False


def _mark_db_stale_closed(symbol: str, *, reason: str, order_message: Any = None) -> bool:
    symbol = _normalize_symbol(symbol)
    if not symbol:
        return False
    session = None
    try:
        from database import Session_position
        from database.models import Position

        session = Session_position()
        rows = (
            session.query(Position)
            .filter(Position.symbol == symbol)
            .all()
        )
        changed = 0
        now = dt.datetime.now()
        for p in rows or []:
            status = str(getattr(p, "status", "") or "OPEN").upper()
            if status in {"CLOSED", "STALE_CLOSED", "EXITED", "CANCELED", "CANCELLED", "REJECTED", "FAILED"}:
                continue
            try:
                p.status = "STALE_CLOSED"
            except Exception:
                pass
            for attr, value in [
                ("exit_time", now),
                ("closed_time", now),
                ("close_time", now),
                ("updated_at", now),
                ("exit_reason", reason),
                ("close_reason", reason),
                ("exit_order_message", str(order_message or "")),
            ]:
                try:
                    if hasattr(p, attr):
                        setattr(p, attr, value)
                except Exception:
                    pass
            changed += 1
        if changed:
            session.commit()
            logger.warning(
                "[EXIT STALE DB CLEANUP] marked stale closed symbol=%s changed=%d reason=%s message=%s",
                symbol,
                changed,
                reason,
                order_message,
            )
            return True
        session.rollback()
        return False
    except Exception:
        try:
            if session is not None:
                session.rollback()
        except Exception:
            pass
        logger.exception("[EXIT STALE DB CLEANUP] failed symbol=%s", symbol)
        return False
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass


def _remove_memory(symbol: str) -> None:
    symbol = _normalize_symbol(symbol)
    try:
        from global_state import global_data
        pos = getattr(global_data, "open_positions", None)
        if isinstance(pos, dict):
            pos.pop(symbol, None)
    except Exception:
        pass
    try:
        from core.global_context.context import global_context as GC
        positions_obj = getattr(GC, "positions", None)
        if positions_obj is not None:
            for name in ("remove", "close", "mark_closed", "delete"):
                fn = getattr(positions_obj, name, None)
                if callable(fn):
                    try:
                        fn(symbol)
                        return
                    except Exception:
                        pass
            raw = getattr(positions_obj, "open_positions", None)
            if isinstance(raw, dict):
                raw.pop(symbol, None)
    except Exception:
        pass


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.exit.executor as executor
    except Exception:
        logger.debug("[EXIT STALE DB CLEANUP] executor not ready", exc_info=True)
        return False

    try:
        cur = getattr(executor, "execute_exit", None)
        if not callable(cur):
            return False
        if getattr(cur, "_stale_db_cleanup_v1", False):
            _INSTALLED = True
            return True

        def _execute_exit_with_stale_cleanup(symbol: str, reason: str, exit_price: float) -> bool:
            ok = False
            try:
                ok = bool(cur(symbol, reason, exit_price))
                return ok
            finally:
                if ok:
                    return
                err = _latest_order_error()
                if _is_stale_no_broker_position_error(symbol, err):
                    cleanup_reason = f"STALE_NO_BROKER_POSITION_AFTER_EXIT_REJECT:{reason}"
                    msg = err.get("message") or err.get("raw") or ""
                    if _mark_db_stale_closed(symbol, reason=cleanup_reason, order_message=msg):
                        _remove_memory(symbol)
                        logger.warning(
                            "[EXIT STALE DB CLEANUP] stale DB position removed from runtime symbol=%s err=%s",
                            symbol,
                            err,
                        )

        _execute_exit_with_stale_cleanup._stale_db_cleanup_v1 = True  # type: ignore[attr-defined]
        _execute_exit_with_stale_cleanup._original_execute_exit = cur  # type: ignore[attr-defined]
        executor.execute_exit = _execute_exit_with_stale_cleanup
        _INSTALLED = True
        logger.warning("[EXIT STALE DB CLEANUP] installed v1")
        return True
    except Exception:
        logger.exception("[EXIT STALE DB CLEANUP] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[EXIT STALE DB CLEANUP] auto install failed")


__all__ = ["install"]
