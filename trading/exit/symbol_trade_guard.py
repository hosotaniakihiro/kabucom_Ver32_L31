# ============================================================
# File   : trading/exit/symbol_trade_guard.py
# Version: V1.0-SYMBOL-COOLDOWN-AND-ENTRY-TIME-GUARD
# ------------------------------------------------------------
# 【概要】
#   スキャルピング用の銘柄別エントリー抑制。
#
# 【組み込み機能】
#   1. 損切り後15分クールダウン
#   2. 同一銘柄2連敗で当日停止
#   3. 一部利確後3分の再エントリー停止
#   4. 全利確後5分の再エントリー停止
#   5. 銘柄ごとの当日成績を保存
#   6. 時間帯フィルタ
#      - 09:00〜09:02 新規停止
#      - 11:20〜12:30 新規停止
#      - 12:30〜12:32 新規停止
#      - 15:23以降 新規停止
#
# 【保存先】
#   既定:
#     \\192.168.0.22\AutoStockBuyAndSell\raw_data\trade_guard\trade_guardYYYYMMDD.db
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


TRADE_GUARD_ENABLED = _env_bool("TRADE_GUARD_ENABLED", True)
ENTRY_TIME_GUARD_ENABLED = _env_bool("ENTRY_TIME_GUARD_ENABLED", True)
LOSS_COOLDOWN_MINUTES = int(float(os.getenv("LOSS_COOLDOWN_MINUTES", "15")))
PROFIT_COOLDOWN_MINUTES = int(float(os.getenv("PROFIT_COOLDOWN_MINUTES", "5")))
PARTIAL_PROFIT_COOLDOWN_MINUTES = int(float(os.getenv("PARTIAL_PROFIT_COOLDOWN_MINUTES", "3")))
MAX_DAILY_LOSSES_PER_SYMBOL = int(float(os.getenv("MAX_DAILY_LOSSES_PER_SYMBOL", "2")))

# 新規エントリー停止時刻。東証の15:30引けを前提に15:23。
ENTRY_STOP_AFTER = os.getenv("ENTRY_STOP_AFTER", "15:23")


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _default_db_path() -> str:
    base = os.getenv(
        "TRADE_GUARD_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\trade_guard",
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


def _connect() -> sqlite3.Connection:
    path = _db_path()
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("[TRADE GUARD] mkdir failed path=%s", path, exc_info=True)
    conn = sqlite3.connect(path, timeout=3.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    _ensure_schema(conn)
    return conn


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
    stop_after = _parse_time_hhmm(ENTRY_STOP_AFTER)

    windows = [
        (dt.time(9, 0), dt.time(9, 2), "ENTRY_TIME_BLOCK_OPENING_0900_0902"),
        (dt.time(11, 20), dt.time(12, 30), "ENTRY_TIME_BLOCK_LUNCH_1120_1230"),
        (dt.time(12, 30), dt.time(12, 32), "ENTRY_TIME_BLOCK_AFTER_LUNCH_1230_1232"),
        (stop_after, dt.time(23, 59, 59), "ENTRY_TIME_BLOCK_AFTER_1523"),
    ]
    for start, end, reason in windows:
        if start <= t <= end:
            return True, reason, {"now": n.strftime("%H:%M:%S"), "start": str(start), "end": str(end)}
    return False, "", {"now": n.strftime("%H:%M:%S")}


def is_entry_blocked(symbol: Any, now: Optional[dt.datetime] = None) -> Tuple[bool, str, Dict[str, Any]]:
    """新規エントリーしてよいかを判定する。"""
    symbol = _norm_symbol(symbol)
    if not TRADE_GUARD_ENABLED:
        return False, "", {}

    tb, tr, td = _time_block_reason(now)
    if tb:
        return True, tr, td

    if not symbol:
        return False, "", {}

    try:
        n = _now(now)
        with _connect() as conn:
            row = _get_row(conn, symbol)
            if int(row.get("day_blocked") or 0):
                return True, "SYMBOL_DAY_BLOCKED_AFTER_LOSSES", row
            until = _from_iso(row.get("cooldown_until"))
            if until and n < until:
                detail = dict(row)
                detail["now"] = _iso(n)
                detail["cooldown_until"] = _iso(until)
                return True, "SYMBOL_COOLDOWN_ACTIVE", detail
    except Exception:
        logger.exception("[TRADE GUARD] is_entry_blocked failed symbol=%s", symbol)
        return False, "", {"error": "trade_guard_failed"}

    return False, "", {}


def record_exit_event(symbol: Any, *, pnl: float, reason: str, now: Optional[dt.datetime] = None) -> None:
    """全返済イベントを記録し、次回エントリー抑制をセットする。"""
    symbol = _norm_symbol(symbol)
    if not TRADE_GUARD_ENABLED or not symbol:
        return
    n = _now(now)
    reason_s = str(reason or "")
    pnl_f = float(pnl or 0.0)
    loss_like = pnl_f < 0 or any(x in reason_s.upper() for x in ["STOP_LOSS", "LOSS", "ADVERSE"])

    try:
        with _connect() as conn:
            row = _get_row(conn, symbol)
            if loss_like:
                row["loss_count"] = int(row.get("loss_count") or 0) + 1
                cooldown_until = n + dt.timedelta(minutes=LOSS_COOLDOWN_MINUTES)
                row["cooldown_until"] = _iso(cooldown_until)
                if int(row["loss_count"]) >= MAX_DAILY_LOSSES_PER_SYMBOL:
                    row["day_blocked"] = 1
                    row["cooldown_until"] = _iso(n.replace(hour=23, minute=59, second=59, microsecond=0))
            else:
                row["win_count"] = int(row.get("win_count") or 0) + 1
                row["cooldown_until"] = _iso(n + dt.timedelta(minutes=PROFIT_COOLDOWN_MINUTES))
            row["last_exit_reason"] = reason_s
            row["last_exit_pnl"] = pnl_f
            row["last_event_time"] = _iso(n)
            _upsert(conn, row)
        logger.warning("[TRADE GUARD] record_exit symbol=%s pnl=%.4f reason=%s", symbol, pnl_f, reason_s)
    except Exception:
        logger.exception("[TRADE GUARD] record_exit failed symbol=%s", symbol)


def record_partial_profit_event(symbol: Any, *, reason: str, now: Optional[dt.datetime] = None) -> None:
    """一部利確後の短時間クールダウンを記録する。"""
    symbol = _norm_symbol(symbol)
    if not TRADE_GUARD_ENABLED or not symbol:
        return
    n = _now(now)
    try:
        with _connect() as conn:
            row = _get_row(conn, symbol)
            row["partial_profit_count"] = int(row.get("partial_profit_count") or 0) + 1
            row["cooldown_until"] = _iso(n + dt.timedelta(minutes=PARTIAL_PROFIT_COOLDOWN_MINUTES))
            row["last_exit_reason"] = str(reason or "PARTIAL_PROFIT")
            row["last_event_time"] = _iso(n)
            _upsert(conn, row)
        logger.warning("[TRADE GUARD] record_partial_profit symbol=%s reason=%s", symbol, reason)
    except Exception:
        logger.exception("[TRADE GUARD] record_partial_profit failed symbol=%s", symbol)


__all__ = [
    "is_entry_blocked",
    "record_exit_event",
    "record_partial_profit_event",
]
