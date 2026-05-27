# ============================================================
# File   : trading/exit/symbol_trade_guard.py
# Version: V1.3-SYMBOL-TRADE-GUARD-JOURNAL-MODE-THROTTLE
# ------------------------------------------------------------
# 【概要】
#   スキャルピング用の銘柄別エントリー抑制。
#
# V1.3:
#   - NAS/SMB上SQLiteで接続のたびに PRAGMA journal_mode を実行しない
#   - database is locked 時は一定時間 journal_mode 再試行を抑制
#   - journal_mode失敗は trade guard 判定を壊さず継続
#   - ログ連打とロック悪化を抑止
#
# V1.2:
#   - V1.1 の _today() 定義インデントを修正
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_JOURNAL_MODE_OK_PATHS: set[str] = set()
_JOURNAL_MODE_SKIP_UNTIL: dict[str, float] = {}
_JOURNAL_MODE_LAST_WARN_AT: dict[str, float] = {}


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


TRADE_GUARD_ENABLED = _env_bool("TRADE_GUARD_ENABLED", True)
ENTRY_TIME_GUARD_ENABLED = _env_bool("ENTRY_TIME_GUARD_ENABLED", True)
LOSS_COOLDOWN_MINUTES = int(float(os.getenv("LOSS_COOLDOWN_MINUTES", "15")))
PROFIT_COOLDOWN_MINUTES = int(float(os.getenv("PROFIT_COOLDOWN_MINUTES", "5")))
PARTIAL_PROFIT_COOLDOWN_MINUTES = int(float(os.getenv("PARTIAL_PROFIT_COOLDOWN_MINUTES", "3")))
MAX_DAILY_LOSSES_PER_SYMBOL = int(float(os.getenv("MAX_DAILY_LOSSES_PER_SYMBOL", "2")))
ENTRY_STOP_AFTER = os.getenv("ENTRY_STOP_AFTER", "15:23")


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _default_db_path() -> str:
    base = os.getenv(
        "TRADE_GUARD_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\trade_guard",
    )
    return str(Path(base) / f"trade_guard{_today()}.db")


def _fallback_db_path() -> str:
    base = os.getenv(
        "TRADE_GUARD_FALLBACK_DB_DIR",
        r"F:\script\python\kabu\runtime_cache\trade_guard",
    )
    return str(Path(base) / f"trade_guard{_today()}.db")


def _db_path() -> str:
    return os.getenv("TRADE_GUARD_DB_PATH", _default_db_path())


def _parse_time_hhmm(s: str) -> dt.time:
    h, m = str(s).strip().split(":", 1)
    return dt.time(int(h), int(m))


def _norm_symbol(symbol: Any) -> str:
    s = str(symbol or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _now(now: Optional[dt.datetime] = None) -> dt.datetime:
    return now or dt.datetime.now()


def _iso(x: Optional[dt.datetime]) -> str:
    if x is None:
        return ""
    return x.replace(microsecond=0).isoformat(sep=" ")


def _from_iso(s: Any) -> Optional[dt.datetime]:
    try:
        if s is None or str(s).strip() == "":
            return None
        return dt.datetime.fromisoformat(str(s).strip().replace("T", " "))
    except Exception:
        return None


def _is_nas_like_path(path: str) -> bool:
    try:
        p = str(path or "")
        return p.startswith("\\\\") or p.startswith("//")
    except Exception:
        return False


def _open_sqlite(path: str) -> sqlite3.Connection:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("[TRADE GUARD] mkdir failed path=%s", path, exc_info=True)
    conn = sqlite3.connect(path, timeout=_env_float("TRADE_GUARD_SQLITE_TIMEOUT_SEC", 3.0))
    conn.execute(f"PRAGMA busy_timeout={int(_env_float('TRADE_GUARD_SQLITE_BUSY_TIMEOUT_MS', 3000.0))}")
    return conn


def _journal_warn_throttled(path: str, message: str, *args, exc_info: bool = False) -> None:
    now = time.time()
    interval = max(1.0, _env_float("TRADE_GUARD_JOURNAL_WARN_INTERVAL_SEC", 60.0))
    last = _JOURNAL_MODE_LAST_WARN_AT.get(path, 0.0)
    if now - last >= interval:
        _JOURNAL_MODE_LAST_WARN_AT[path] = now
        logger.warning(message, *args, exc_info=exc_info)
    else:
        logger.debug(message, *args, exc_info=exc_info)


def _apply_journal_mode(conn: sqlite3.Connection, path: str) -> None:
    if not _env_bool("TRADE_GUARD_APPLY_JOURNAL_MODE", True):
        return

    path_key = str(path)
    if path_key in _JOURNAL_MODE_OK_PATHS:
        return

    now = time.time()
    skip_until = float(_JOURNAL_MODE_SKIP_UNTIL.get(path_key, 0.0) or 0.0)
    if now < skip_until:
        logger.debug(
            "[TRADE GUARD] journal_mode skipped by throttle path=%s remain=%.3fs",
            path,
            skip_until - now,
        )
        return

    desired = str(os.getenv("TRADE_GUARD_SQLITE_JOURNAL_MODE", "")).strip().upper()
    if not desired:
        # NAS/SMBはDELETE既定。ただし毎回PRAGMA実行するとロック原因になるため成功後は記憶する。
        desired = "DELETE" if _is_nas_like_path(path) else "WAL"

    try:
        conn.execute(f"PRAGMA journal_mode={desired}")
        _JOURNAL_MODE_OK_PATHS.add(path_key)
        logger.info("[TRADE GUARD] journal_mode applied path=%s mode=%s", path, desired)
        return
    except sqlite3.OperationalError as e:
        cooldown = max(5.0, _env_float("TRADE_GUARD_JOURNAL_RETRY_COOLDOWN_SEC", 300.0))
        _JOURNAL_MODE_SKIP_UNTIL[path_key] = time.time() + cooldown
        _journal_warn_throttled(
            path_key,
            "[TRADE GUARD] journal_mode skipped after lock path=%s mode=%s err=%s retry_after=%.1fs",
            path,
            desired,
            e,
            cooldown,
            exc_info=False,
        )
        return
    except Exception as e:
        cooldown = max(5.0, _env_float("TRADE_GUARD_JOURNAL_RETRY_COOLDOWN_SEC", 300.0))
        _JOURNAL_MODE_SKIP_UNTIL[path_key] = time.time() + cooldown
        _journal_warn_throttled(
            path_key,
            "[TRADE GUARD] journal_mode failed path=%s mode=%s err=%s retry_after=%.1fs",
            path,
            desired,
            e,
            cooldown,
            exc_info=True,
        )
        return


def _connect_path(path: str) -> sqlite3.Connection:
    conn = _open_sqlite(path)
    try:
        _apply_journal_mode(conn, path)
        _ensure_schema(conn)
        return conn
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise


def _connect() -> sqlite3.Connection:
    path = _db_path()
    try:
        return _connect_path(path)
    except Exception as e:
        if not _env_bool("TRADE_GUARD_USE_LOCAL_FALLBACK_ON_IO_ERROR", True):
            raise
        fb = _fallback_db_path()
        logger.warning(
            "[TRADE GUARD] primary db connect failed path=%s err=%s -> fallback local db=%s",
            path,
            e,
            fb,
            exc_info=True,
        )
        return _connect_path(fb)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_trade_guard (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            loss_count INTEGER NOT NULL DEFAULT 0,
            win_count INTEGER NOT NULL DEFAULT 0,
            partial_profit_count INTEGER NOT NULL DEFAULT 0,
            cooldown_until TEXT DEFAULT '',
            day_blocked INTEGER NOT NULL DEFAULT 0,
            last_exit_reason TEXT DEFAULT '',
            last_exit_pnl REAL DEFAULT 0,
            last_event_time TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            PRIMARY KEY (trade_date, symbol)
        )
        """
    )
    conn.commit()


def _get_row(conn: sqlite3.Connection, symbol: str) -> Dict[str, Any]:
    trade_date = _today()
    cur = conn.execute(
        "SELECT trade_date, symbol, loss_count, win_count, partial_profit_count, cooldown_until, day_blocked, last_exit_reason, last_exit_pnl, last_event_time, updated_at FROM symbol_trade_guard WHERE trade_date=? AND symbol=?",
        (trade_date, symbol),
    )
    row = cur.fetchone()
    if row:
        keys = [
            "trade_date", "symbol", "loss_count", "win_count", "partial_profit_count",
            "cooldown_until", "day_blocked", "last_exit_reason", "last_exit_pnl",
            "last_event_time", "updated_at",
        ]
        return dict(zip(keys, row))
    return {
        "trade_date": trade_date,
        "symbol": symbol,
        "loss_count": 0,
        "win_count": 0,
        "partial_profit_count": 0,
        "cooldown_until": "",
        "day_blocked": 0,
        "last_exit_reason": "",
        "last_exit_pnl": 0.0,
        "last_event_time": "",
        "updated_at": "",
    }


def _upsert(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    row["updated_at"] = _iso(dt.datetime.now())
    conn.execute(
        """
        INSERT INTO symbol_trade_guard (
            trade_date, symbol, loss_count, win_count, partial_profit_count,
            cooldown_until, day_blocked, last_exit_reason, last_exit_pnl,
            last_event_time, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, symbol) DO UPDATE SET
            loss_count=excluded.loss_count,
            win_count=excluded.win_count,
            partial_profit_count=excluded.partial_profit_count,
            cooldown_until=excluded.cooldown_until,
            day_blocked=excluded.day_blocked,
            last_exit_reason=excluded.last_exit_reason,
            last_exit_pnl=excluded.last_exit_pnl,
            last_event_time=excluded.last_event_time,
            updated_at=excluded.updated_at
        """,
        (
            row["trade_date"], row["symbol"], int(row.get("loss_count") or 0),
            int(row.get("win_count") or 0), int(row.get("partial_profit_count") or 0),
            str(row.get("cooldown_until") or ""), int(row.get("day_blocked") or 0),
            str(row.get("last_exit_reason") or ""), float(row.get("last_exit_pnl") or 0.0),
            str(row.get("last_event_time") or ""), str(row.get("updated_at") or ""),
        ),
    )
    conn.commit()


def _time_block_reason(now: Optional[dt.datetime] = None) -> Tuple[bool, str, Dict[str, Any]]:
    if not ENTRY_TIME_GUARD_ENABLED:
        return False, "", {}
    n = _now(now)
    t = n.time()
    stop_t = _parse_time_hhmm(ENTRY_STOP_AFTER)
    if t >= stop_t:
        return True, "after_entry_stop_time", {"now": t.strftime("%H:%M"), "entry_stop_after": ENTRY_STOP_AFTER}
    return False, "", {}


def check_entry_allowed(symbol: Any, now: Optional[dt.datetime] = None) -> Tuple[bool, str, Dict[str, Any]]:
    if not TRADE_GUARD_ENABLED:
        return True, "trade_guard_disabled", {}
    sym = _norm_symbol(symbol)
    if not sym:
        return False, "symbol_empty", {}
    blocked, reason, meta = _time_block_reason(now)
    if blocked:
        return False, reason, meta
    try:
        with _connect() as conn:
            row = _get_row(conn, sym)
        n = _now(now)
        cooldown_until = _from_iso(row.get("cooldown_until"))
        if int(row.get("day_blocked") or 0):
            return False, "symbol_day_blocked", row
        if cooldown_until is not None and n < cooldown_until:
            row["cooldown_remaining_sec"] = int((cooldown_until - n).total_seconds())
            return False, "symbol_cooldown", row
        if int(row.get("loss_count") or 0) >= MAX_DAILY_LOSSES_PER_SYMBOL:
            return False, "symbol_loss_limit", row
        return True, "ok", row
    except Exception as e:
        logger.warning("[TRADE GUARD] check failed symbol=%s err=%s -> allow fail-open", sym, e, exc_info=True)
        return True, "trade_guard_error_fail_open", {"error": str(e)}


def record_exit(symbol: Any, pnl: float = 0.0, reason: str = "") -> None:
    if not TRADE_GUARD_ENABLED:
        return
    sym = _norm_symbol(symbol)
    if not sym:
        return
    try:
        with _connect() as conn:
            row = _get_row(conn, sym)
            pnl_f = float(pnl or 0.0)
            if pnl_f < 0:
                row["loss_count"] = int(row.get("loss_count") or 0) + 1
                row["cooldown_until"] = _iso(dt.datetime.now() + dt.timedelta(minutes=LOSS_COOLDOWN_MINUTES))
                if int(row["loss_count"]) >= MAX_DAILY_LOSSES_PER_SYMBOL:
                    row["day_blocked"] = 1
            elif pnl_f > 0:
                row["win_count"] = int(row.get("win_count") or 0) + 1
                row["cooldown_until"] = _iso(dt.datetime.now() + dt.timedelta(minutes=PROFIT_COOLDOWN_MINUTES))
            else:
                row["partial_profit_count"] = int(row.get("partial_profit_count") or 0) + 1
                row["cooldown_until"] = _iso(dt.datetime.now() + dt.timedelta(minutes=PARTIAL_PROFIT_COOLDOWN_MINUTES))
            row["last_exit_reason"] = str(reason or "")
            row["last_exit_pnl"] = pnl_f
            row["last_event_time"] = _iso(dt.datetime.now())
            _upsert(conn, row)
            logger.info("[TRADE GUARD] record_exit symbol=%s pnl=%.2f reason=%s row=%s", sym, pnl_f, reason, row)
    except Exception:
        logger.exception("[TRADE GUARD] record_exit failed symbol=%s", sym)


def record_entry(symbol: Any) -> None:
    if not TRADE_GUARD_ENABLED:
        return
    sym = _norm_symbol(symbol)
    if not sym:
        return
    try:
        with _connect() as conn:
            row = _get_row(conn, sym)
            row["last_event_time"] = _iso(dt.datetime.now())
            _upsert(conn, row)
    except Exception:
        logger.exception("[TRADE GUARD] record_entry failed symbol=%s", sym)


__all__ = ["check_entry_allowed", "record_exit", "record_entry"]
