# ============================================================
# File   : core/startup/exit_closing_stale_reconcile_runtime_patch.py
# Version: V1-STALE-CLOSING-BROKER-RECONCILE
# ------------------------------------------------------------
# CLOSINGのまま長時間残った建玉を救済する。
#
# 方針:
#   - positions.db の CLOSING が一定秒数以上残ったら確認対象
#   - ブローカー建玉に同銘柄が残っていなければ CLOSED 確定
#   - ブローカー建玉に同銘柄が残っていれば OPEN に戻す
#   - ブローカー取得に失敗した場合は何もしない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_THREAD_STARTED = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _sym(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _row_age_sec(row: Any) -> float:
    now = dt.datetime.now()
    for attr in ("updated_at", "exit_time", "created_at", "entry_time"):
        try:
            raw = getattr(row, attr, None)
        except Exception:
            raw = None
        if not raw:
            continue
        try:
            if isinstance(raw, dt.datetime):
                return max(0.0, (now - raw.replace(tzinfo=None)).total_seconds())
            s = str(raw).strip().replace("T", " ")
            if "+" in s:
                s = s.split("+", 1)[0]
            if s.endswith("Z"):
                s = s[:-1]
            return max(0.0, (now - dt.datetime.fromisoformat(s)).total_seconds())
        except Exception:
            pass
    return 999999.0


def _load_broker_open_symbols() -> tuple[bool, set[str]]:
    # 1. global_data がブローカー同期済みならまずそこを見る。
    symbols: set[str] = set()
    try:
        from global_state import global_data
        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict):
            for k, v in positions.items():
                sym = _sym(k or (v.get("symbol") if isinstance(v, dict) else ""))
                if sym:
                    status = str(v.get("status") if isinstance(v, dict) else "OPEN").upper()
                    if status not in {"CLOSED", "CLOSE", "EXITED", "DONE", "CANCELED", "CANCELLED", "REJECTED"}:
                        symbols.add(sym)
            if symbols:
                return True, symbols
    except Exception:
        logger.debug("[EXIT CLOSING RECONCILE] global_data broker symbols failed", exc_info=True)

    # 2. kabu REST positions helper があれば使う。環境差を吸収するため複数候補。
    candidates = [
        ("kabu_api.positions", "get_positions"),
        ("kabu_api.position", "get_positions"),
        ("kabu_api.get_positions", "get_positions"),
        ("kabu_api.api_positions", "get_positions"),
    ]
    for mod_name, fn_name in candidates:
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            fn = getattr(mod, fn_name, None)
            if not callable(fn):
                continue
            ret = fn()
            rows = ret.get("Positions", ret.get("positions", [])) if isinstance(ret, dict) else ret
            if not isinstance(rows, list):
                continue
            for p in rows:
                if not isinstance(p, dict):
                    continue
                sym = _sym(p.get("Symbol") or p.get("symbol") or p.get("Code") or p.get("code"))
                qty = _f(p.get("LeavesQty") or p.get("HoldQty") or p.get("Qty") or p.get("qty") or p.get("LeavesQuantity"), 0.0)
                if sym and qty > 0:
                    symbols.add(sym)
            return True, symbols
        except Exception:
            continue

    return False, set()


def _close_row(row: Any, reason: str) -> None:
    now = dt.datetime.now()
    row.status = "CLOSED"
    for attr, value in [
        ("closed_time", now),
        ("close_time", now),
        ("exit_time", now),
        ("updated_at", now),
        ("exit_reason", reason),
        ("close_reason", reason),
    ]:
        try:
            if hasattr(row, attr):
                setattr(row, attr, value)
        except Exception:
            pass


def _open_row(row: Any, reason: str) -> None:
    row.status = "OPEN"
    for attr, value in [
        ("updated_at", dt.datetime.now()),
        ("exit_reason", reason),
        ("close_reason", reason),
    ]:
        try:
            if hasattr(row, attr):
                setattr(row, attr, value)
        except Exception:
            pass


def _loop() -> None:
    try:
        from database import Session_position
        from database.models import Position
    except Exception:
        logger.exception("[EXIT CLOSING RECONCILE] import DB failed")
        return

    interval = max(1.0, _env_float("EXIT_CLOSING_RECONCILE_INTERVAL_SEC", 5.0))
    stale_sec = max(2.0, _env_float("EXIT_CLOSING_STALE_SEC", 20.0))
    logger.warning("[EXIT CLOSING RECONCILE] loop start interval=%.1fs stale_sec=%.1fs", interval, stale_sec)

    while True:
        session = None
        try:
            ok, broker_symbols = _load_broker_open_symbols()
            if not ok:
                logger.debug("[EXIT CLOSING RECONCILE] broker symbols unavailable -> skip")
                time.sleep(interval)
                continue
            session = Session_position()
            rows = session.query(Position).filter(Position.status == "CLOSING").all()
            changed = 0
            for row in rows or []:
                sym = _sym(getattr(row, "symbol", ""))
                if not sym:
                    continue
                age = _row_age_sec(row)
                if age < stale_sec:
                    continue
                if sym in broker_symbols:
                    _open_row(row, "closing_stale_broker_still_open")
                    changed += 1
                    logger.warning("[EXIT CLOSING RECONCILE] stale CLOSING -> OPEN symbol=%s age=%.1fs broker_open=True", sym, age)
                else:
                    _close_row(row, "closing_stale_broker_flat_confirmed")
                    changed += 1
                    logger.warning("[EXIT CLOSING RECONCILE] stale CLOSING -> CLOSED symbol=%s age=%.1fs broker_open=False", sym, age)
                    try:
                        from global_state import global_data
                        positions = getattr(global_data, "open_positions", None)
                        if isinstance(positions, dict):
                            positions.pop(sym, None)
                    except Exception:
                        pass
            if changed:
                session.commit()
            else:
                session.rollback()
        except Exception:
            try:
                if session is not None:
                    session.rollback()
            except Exception:
                pass
            logger.exception("[EXIT CLOSING RECONCILE] loop error")
        finally:
            try:
                if session is not None:
                    session.close()
            except Exception:
                pass
        time.sleep(interval)


def install() -> bool:
    global _INSTALLED, _THREAD_STARTED
    if _INSTALLED:
        return True
    if not _env_bool("EXIT_CLOSING_RECONCILE_ENABLED", True):
        logger.warning("[EXIT CLOSING RECONCILE] disabled")
        return False
    if not _THREAD_STARTED:
        threading.Thread(target=_loop, daemon=True, name="exit_closing_reconcile_loop").start()
        _THREAD_STARTED = True
    _INSTALLED = True
    logger.warning(
        "[EXIT CLOSING RECONCILE] installed stale_sec=%.1f interval=%.1f",
        _env_float("EXIT_CLOSING_STALE_SEC", 20.0),
        _env_float("EXIT_CLOSING_RECONCILE_INTERVAL_SEC", 5.0),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[EXIT CLOSING RECONCILE] auto install failed")


__all__ = ["install"]
