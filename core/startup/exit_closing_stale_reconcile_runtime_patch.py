# ============================================================
# File   : core/startup/exit_closing_stale_reconcile_runtime_patch.py
# Version: V1.1-STALE-CLOSING-BROKER-REST-FIRST
# ------------------------------------------------------------
# CLOSINGのまま長時間残った建玉を救済する。
#
# 方針:
#   - positions.db の CLOSING が一定秒数以上残ったら確認対象
#   - 原則、ブローカーREST建玉取得に成功した時だけOPEN/CLOSEDへ救済
#   - ブローカー建玉に同銘柄が残っていなければ CLOSED 確定
#   - ブローカー建玉に同銘柄が残っていれば OPEN に戻す
#   - ブローカー取得に失敗した場合は何もしない
#
# V1.1:
#   - global_data.open_positions を最優先しない。
#   - メモリ状態でCLOSING救済を確定すると誤判定の危険があるため、
#     REST建玉取得成功時だけ確定判断する。
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
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


def _extract_position_rows(ret: Any) -> list[dict]:
    if isinstance(ret, dict):
        rows = ret.get("Positions") or ret.get("positions") or ret.get("Data") or ret.get("data") or []
    else:
        rows = ret
    if isinstance(rows, list):
        return [x for x in rows if isinstance(x, dict)]
    return []


def _rows_to_open_symbols(rows: list[dict]) -> set[str]:
    out: set[str] = set()
    for p in rows or []:
        sym = _sym(p.get("Symbol") or p.get("symbol") or p.get("Code") or p.get("code"))
        qty = _f(
            p.get("LeavesQty")
            or p.get("HoldQty")
            or p.get("Qty")
            or p.get("qty")
            or p.get("LeavesQuantity")
            or p.get("HoldQuantity"),
            0.0,
        )
        if sym and qty > 0:
            out.add(sym)
    return out


def _get_token() -> str | None:
    try:
        from token_manager import get_valid_token, refresh_token
        return get_valid_token() or refresh_token()
    except Exception:
        logger.debug("[EXIT CLOSING RECONCILE] token unavailable", exc_info=True)
        return None


def _fetch_positions_rest_direct() -> tuple[bool, set[str]]:
    token = _get_token()
    if not token:
        return False, set()
    base = os.getenv("KABUSAPI_BASE_URL", "http://localhost:18080/kabusapi").rstrip("/")
    # kabuS API positions は product 指定なしでも返る環境が多いが、念のため現物/信用候補も試す。
    urls = [
        f"{base}/positions",
        f"{base}/positions?{urllib.parse.urlencode({'product': 2})}",
        f"{base}/positions?{urllib.parse.urlencode({'product': 0})}",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"X-API-KEY": token}, method="GET")
            with urllib.request.urlopen(req, timeout=_env_float("EXIT_CLOSING_RECONCILE_REST_TIMEOUT_SEC", 1.5)) as res:
                raw = json.loads(res.read().decode("utf-8", errors="replace"))
            rows = _extract_position_rows(raw)
            symbols = _rows_to_open_symbols(rows)
            logger.info("[EXIT CLOSING RECONCILE] REST positions ok url=%s rows=%s symbols=%s", url, len(rows), sorted(symbols)[:20])
            return True, symbols
        except Exception:
            logger.debug("[EXIT CLOSING RECONCILE] direct REST positions failed url=%s", url, exc_info=True)
            continue
    return False, set()


def _load_broker_open_symbols() -> tuple[bool, set[str]]:
    # 1. まずブローカーREST helperを試す。
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
            rows = _extract_position_rows(ret)
            symbols = _rows_to_open_symbols(rows)
            logger.info("[EXIT CLOSING RECONCILE] helper positions ok module=%s rows=%s symbols=%s", mod_name, len(rows), sorted(symbols)[:20])
            return True, symbols
        except Exception:
            continue

    # 2. helperが無い/失敗する場合は直接REST。
    ok, symbols = _fetch_positions_rest_direct()
    if ok:
        return True, symbols

    # 3. 最後の補助: 明示許可時だけメモリを見る。デフォルトでは使わない。
    if not _env_bool("EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK", False):
        return False, set()
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
            logger.warning("[EXIT CLOSING RECONCILE] memory fallback used symbols=%s", sorted(symbols)[:20])
            return True, symbols
    except Exception:
        logger.debug("[EXIT CLOSING RECONCILE] memory fallback failed", exc_info=True)
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
    logger.warning("[EXIT CLOSING RECONCILE] loop start interval=%.1fs stale_sec=%.1fs rest_first=True", interval, stale_sec)

    while True:
        session = None
        try:
            ok, broker_symbols = _load_broker_open_symbols()
            if not ok:
                logger.debug("[EXIT CLOSING RECONCILE] broker REST symbols unavailable -> skip")
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
                    _open_row(row, "closing_stale_broker_rest_still_open")
                    changed += 1
                    logger.warning("[EXIT CLOSING RECONCILE] stale CLOSING -> OPEN symbol=%s age=%.1fs broker_open=True source=REST", sym, age)
                else:
                    _close_row(row, "closing_stale_broker_rest_flat_confirmed")
                    changed += 1
                    logger.warning("[EXIT CLOSING RECONCILE] stale CLOSING -> CLOSED symbol=%s age=%.1fs broker_open=False source=REST", sym, age)
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
        "[EXIT CLOSING RECONCILE] installed stale_sec=%.1f interval=%.1f rest_timeout=%.1f memory_fallback=%s",
        _env_float("EXIT_CLOSING_STALE_SEC", 20.0),
        _env_float("EXIT_CLOSING_RECONCILE_INTERVAL_SEC", 5.0),
        _env_float("EXIT_CLOSING_RECONCILE_REST_TIMEOUT_SEC", 1.5),
        _env_bool("EXIT_CLOSING_RECONCILE_ALLOW_MEMORY_FALLBACK", False),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[EXIT CLOSING RECONCILE] auto install failed")


__all__ = ["install"]
