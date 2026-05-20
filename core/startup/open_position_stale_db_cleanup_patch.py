# ============================================================
# File   : core/startup/open_position_stale_db_cleanup_patch.py
# Version: Ver01-CLOSE-STALE-DB-WHEN-BROKER-AUTHORITATIVE
# ------------------------------------------------------------
# broker API が正常に読めていて open_positions が空なのに、
# positions.db 側だけに古い OPEN 行が残る問題を掃除する。
#
# 症状:
#   [OPEN POSITION SYNC] DB scan ... symbols=['9716']
#   [OPEN POSITION SYNC] DB publish skipped because broker authoritative active ... current_symbols=[]
#
# 方針:
#   - broker_read_ok=True かつ mode=broker_credit_authoritative* の時だけ動く
#   - broker側に存在しない DB open_like 行を CLOSED_BROKER_NOT_OPEN に更新
#   - global_data.open_positions には戻さない
#
# ENV:
#   OPEN_POSITION_AUTO_CLOSE_STALE_DB=1
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_SYNC = None
_LAST_CLEAN_AT: dt.datetime | None = None

CLOSED_STATUS = "CLOSED_BROKER_NOT_OPEN"


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _norm(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _broker_authoritative_active() -> bool:
    try:
        from global_state import global_data
        read_ok = bool(getattr(global_data, "open_positions_broker_read_ok", False))
        mode = str(getattr(global_data, "open_positions_source_mode", "") or "")
        return bool(read_ok and mode.startswith("broker_credit_authoritative"))
    except Exception:
        return False


def _current_broker_symbols() -> set[str]:
    try:
        from global_state import global_data
        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict):
            return {_norm(k) for k in positions.keys() if _norm(k)}
    except Exception:
        pass
    return set()


def _status(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_open_like_status(v: Any) -> bool:
    st = _status(v)
    if st in {"CLOSED", "CLOSE", "EXITED", "EXIT", "DONE", "CANCELED", "CANCELLED", "REJECTED", "FAILED", CLOSED_STATUS}:
        return False
    return True


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _cleanup_stale_db_positions() -> int:
    global _LAST_CLEAN_AT

    if not _env_bool("OPEN_POSITION_AUTO_CLOSE_STALE_DB", True):
        return 0
    if not _broker_authoritative_active():
        return 0

    # 毎秒UPDATEを打たない。DB scanは走っても、cleanupは少し間引く。
    cooldown = max(1, _env_int("OPEN_POSITION_STALE_DB_CLEAN_INTERVAL_SEC", 30))
    now = dt.datetime.now()
    if _LAST_CLEAN_AT is not None and (now - _LAST_CLEAN_AT).total_seconds() < cooldown:
        return 0
    _LAST_CLEAN_AT = now

    broker_symbols = _current_broker_symbols()

    try:
        from database import Session_position
        from database.models import Position
    except Exception:
        logger.debug("[OPEN POSITION STALE DB CLEAN] import DB modules failed", exc_info=True)
        return 0

    session = None
    changed = 0
    changed_symbols: list[str] = []
    try:
        session = Session_position()
        rows = session.query(Position).all()
        for p in rows or []:
            symbol = _norm(getattr(p, "symbol", ""))
            if not symbol:
                continue
            if symbol in broker_symbols:
                continue
            if not _is_open_like_status(getattr(p, "status", "")):
                continue
            qty = _safe_float(getattr(p, "qty", 0), 0.0)
            price = _safe_float(getattr(p, "avg_price", 0), 0.0) or _safe_float(getattr(p, "price", 0), 0.0)
            if qty <= 0 or price <= 0:
                continue

            try:
                setattr(p, "status", CLOSED_STATUS)
            except Exception:
                pass
            try:
                setattr(p, "updated_at", now)
            except Exception:
                pass
            changed += 1
            changed_symbols.append(symbol)

        if changed:
            session.commit()
            logger.warning(
                "[OPEN POSITION STALE DB CLEAN] closed stale db positions count=%s symbols=%s broker_symbols=%s status=%s",
                changed,
                changed_symbols,
                sorted(broker_symbols),
                CLOSED_STATUS,
            )
        return changed
    except Exception as e:
        try:
            if session is not None:
                session.rollback()
        except Exception:
            pass
        logger.exception("[OPEN POSITION STALE DB CLEAN] failed err=%s", e)
        return 0
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass


def _patched_sync_open_positions_from_db(*args, **kwargs):
    result = _ORIG_SYNC(*args, **kwargs) if callable(_ORIG_SYNC) else {}
    try:
        _cleanup_stale_db_positions()
    except Exception:
        logger.exception("[OPEN POSITION STALE DB CLEAN] cleanup after sync failed")
    return result


def install() -> bool:
    global _INSTALLED, _ORIG_SYNC
    if _INSTALLED:
        return True
    try:
        import trading.position.open_position_sync as sync_mod

        cur = getattr(sync_mod, "sync_open_positions_from_db", None)
        if getattr(cur, "_open_position_stale_db_clean_v1", False):
            _INSTALLED = True
            return True

        _ORIG_SYNC = cur
        _patched_sync_open_positions_from_db._open_position_stale_db_clean_v1 = True  # type: ignore[attr-defined]
        sync_mod.sync_open_positions_from_db = _patched_sync_open_positions_from_db
        _INSTALLED = True
        logger.warning(
            "[OPEN POSITION STALE DB CLEAN] installed enabled=%s status=%s interval_sec=%s",
            _env_bool("OPEN_POSITION_AUTO_CLOSE_STALE_DB", True),
            CLOSED_STATUS,
            _env_int("OPEN_POSITION_STALE_DB_CLEAN_INTERVAL_SEC", 30),
        )
        return True
    except Exception as e:
        logger.exception("[OPEN POSITION STALE DB CLEAN] install failed err=%s", e)
        return False

try:
    install()
except Exception as e:
    logger.exception("[OPEN POSITION STALE DB CLEAN] auto install failed err=%s", e)

__all__ = ["install"]
