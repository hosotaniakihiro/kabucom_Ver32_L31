# ============================================================
# File   : core/startup/exit_limit_pending_close_runtime_patch.py
# Version: V1-PENDING-CLOSE-FOR-LIMIT-EXIT
# ------------------------------------------------------------
# EXITを板タッチ指値にした場合、注文受付だけでpositions.dbをCLOSEDに
# すると、未約定のまま建玉が閉じた扱いになる。
#
# このpatchは、返済注文受付後のDB更新をCLOSEDではなくCLOSINGに変える。
# 実約定確認・未約定取消・再EXITは別patch/既存同期側に任せる。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("EXIT_LIMIT_PENDING_CLOSE_ENABLED", True):
        logger.warning("[EXIT LIMIT PENDING CLOSE] disabled")
        return False
    try:
        import trading.exit.executor as ex
        from database import Session_position
        from database.models import Position
    except Exception:
        logger.exception("[EXIT LIMIT PENDING CLOSE] import failed")
        return False

    old_close_db = getattr(ex, "_close_db_position", None)
    if not callable(old_close_db):
        logger.warning("[EXIT LIMIT PENDING CLOSE] _close_db_position missing")
        return False
    if getattr(old_close_db, "_exit_limit_pending_close_wrapped", False):
        _INSTALLED = True
        return True

    def _mark_db_closing(*, symbol: str, exit_price: float, pnl: float, reason: str, order_id: str) -> bool:
        # 成行運用へ戻したい場合は従来挙動へ戻せる。
        if _env_bool("EXIT_MARK_CLOSED_ON_ORDER_ACCEPT", False):
            return old_close_db(symbol=symbol, exit_price=exit_price, pnl=pnl, reason=reason, order_id=order_id)

        session = Session_position()
        try:
            db_pos = (
                session.query(Position)
                .filter(Position.symbol == str(symbol))
                .filter(Position.status.in_(["OPEN", "CLOSING"]))
                .first()
            )
            if not db_pos:
                logger.warning("[EXIT LIMIT PENDING CLOSE] DB open/closing position not found symbol=%s", symbol)
                session.rollback()
                return False
            now = dt.datetime.now()
            db_pos.status = "CLOSING"
            for attr, value in [
                ("exit_price", _f(exit_price)),
                ("updated_at", now),
                ("pnl", _f(pnl)),
                ("exit_reason", reason),
                ("close_reason", reason),
                ("exit_order_id", order_id),
                ("order_id", order_id),
            ]:
                try:
                    if hasattr(db_pos, attr):
                        setattr(db_pos, attr, value)
                except Exception:
                    pass
            session.commit()
            logger.warning(
                "[EXIT LIMIT PENDING CLOSE] DB marked CLOSING symbol=%s exit_price=%.4f pnl=%.4f order_id=%s reason=%s",
                symbol, _f(exit_price), _f(pnl), order_id, reason,
            )
            return True
        except Exception:
            session.rollback()
            logger.exception("[EXIT LIMIT PENDING CLOSE] DB mark closing failed symbol=%s", symbol)
            return False
        finally:
            try:
                session.close()
            except Exception:
                pass

    _mark_db_closing._exit_limit_pending_close_wrapped = True  # type: ignore[attr-defined]
    _mark_db_closing._original = old_close_db  # type: ignore[attr-defined]
    ex._close_db_position = _mark_db_closing
    _INSTALLED = True
    logger.warning("[EXIT LIMIT PENDING CLOSE] installed mark CLOSING instead of CLOSED on close-order accept")
    return True


try:
    install()
except Exception:
    logger.exception("[EXIT LIMIT PENDING CLOSE] auto install failed")


__all__ = ["install"]
