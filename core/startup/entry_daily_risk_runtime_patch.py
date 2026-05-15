# ============================================================
# File   : core/startup/entry_daily_risk_runtime_patch.py
# Version: V1.1-COUNT-FILLED-ONLY
# ------------------------------------------------------------
# 導入ルール:
#   1. BUY新規停止
#   2. 同一銘柄2連敗で当日停止
#      - 既存 trading.exit.symbol_trade_guard の loss_count を利用
#   3. 同一銘柄の当日最大エントリー2回
#      - 注意: 未約定取消では回数を増やさない
#      - 実際に約定し、返済イベントが出た時だけ回数を増やす
#   4. 1銘柄の当日損失 -2,000円で停止
#
# 環境変数:
#   ENTRY_BUY_ENABLED=0
#   ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL=2
#   ENTRY_SYMBOL_MAX_DAILY_LOSS_YEN=-2000
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _norm_symbol(symbol: Any) -> str:
    s = str(symbol or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _db_path() -> str:
    base = os.getenv(
        "TRADE_GUARD_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\trade_guard",
    )
    return os.getenv("TRADE_GUARD_DB_PATH", str(Path(base) / f"trade_guard{_today()}.db"))


def _connect() -> sqlite3.Connection:
    path = _db_path()
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    conn = sqlite3.connect(path, timeout=3.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_daily_entry_risk (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            entry_count INTEGER NOT NULL DEFAULT 0,
            daily_pnl REAL NOT NULL DEFAULT 0,
            last_entry_time TEXT DEFAULT '',
            last_exit_time TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            PRIMARY KEY (trade_date, symbol)
        )
        """
    )
    conn.commit()


def _now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _get_row(symbol: str) -> Dict[str, Any]:
    symbol = _norm_symbol(symbol)
    with _connect() as conn:
        cur = conn.execute(
            "SELECT entry_count, daily_pnl, last_entry_time, last_exit_time FROM symbol_daily_entry_risk WHERE trade_date=? AND symbol=?",
            (_today(), symbol),
        )
        row = cur.fetchone()
        if row:
            return {
                "entry_count": int(row[0] or 0),
                "daily_pnl": float(row[1] or 0.0),
                "last_entry_time": row[2] or "",
                "last_exit_time": row[3] or "",
            }
    return {"entry_count": 0, "daily_pnl": 0.0, "last_entry_time": "", "last_exit_time": ""}


def _record_actual_trade(symbol: str, pnl: float) -> None:
    """実際に約定して返済された時だけ、当日エントリー回数と損益を更新する。"""
    symbol = _norm_symbol(symbol)
    if not symbol:
        return
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO symbol_daily_entry_risk (trade_date, symbol, entry_count, daily_pnl, last_entry_time, last_exit_time, updated_at)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(trade_date, symbol) DO UPDATE SET
                entry_count = entry_count + 1,
                daily_pnl = daily_pnl + excluded.daily_pnl,
                last_exit_time = excluded.last_exit_time,
                updated_at = excluded.updated_at
            """,
            (_today(), symbol, float(pnl or 0.0), _now_iso(), _now_iso(), _now_iso()),
        )
        conn.commit()
    logger.warning("[ENTRY DAILY RISK] actual_trade recorded symbol=%s pnl=%s row=%s", symbol, pnl, _get_row(symbol))


def _risk_block_reason(symbol: str, side: str) -> Tuple[bool, str, Dict[str, Any]]:
    symbol = _norm_symbol(symbol)
    side_u = str(side or "").upper()
    if side_u == "BUY" and not _env_bool("ENTRY_BUY_ENABLED", False):
        return True, "BUY_DISABLED_BY_DAILY_RISK_PATCH", {"symbol": symbol, "side": side_u}
    row = _get_row(symbol)
    max_entries = _env_int("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL", 2)
    max_loss = _env_float("ENTRY_SYMBOL_MAX_DAILY_LOSS_YEN", -2000.0)
    if max_entries > 0 and int(row.get("entry_count") or 0) >= max_entries:
        return True, "SYMBOL_DAILY_ENTRY_LIMIT", {"symbol": symbol, "side": side_u, "max_entries": max_entries, **row}
    if float(row.get("daily_pnl") or 0.0) <= max_loss:
        return True, "SYMBOL_DAILY_LOSS_LIMIT", {"symbol": symbol, "side": side_u, "max_loss": max_loss, **row}
    return False, "", row


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_controller as ec
        import trading.exit.symbol_trade_guard as stg
    except Exception:
        logger.exception("[ENTRY DAILY RISK] import failed")
        return False

    old_execute = getattr(ec, "_execute_best_candidate", None)
    old_record_exit = getattr(stg, "record_exit_event", None)
    if not callable(old_execute):
        logger.warning("[ENTRY DAILY RISK] _execute_best_candidate not callable")
        return False

    if not getattr(old_execute, "_entry_daily_risk_wrapped_v11", False):
        def _execute_best_candidate_daily_risk(item: dict, boost_active: bool) -> bool:
            try:
                symbol = _norm_symbol(item.get("symbol"))
                side = str(item.get("side") or "").upper()
                blocked, reason, detail = _risk_block_reason(symbol, side)
                if blocked:
                    try:
                        ec._log_skip(symbol, reason, **detail)
                    except Exception:
                        logger.warning("[ENTRY DAILY RISK] blocked symbol=%s reason=%s detail=%s", symbol, reason, detail)
                    return False
            except Exception:
                logger.exception("[ENTRY DAILY RISK] precheck failed")
            # ここでは entry_count を増やさない。
            # order_id が返っても、2秒未約定キャンセルされる可能性があるため。
            return old_execute(item, boost_active=boost_active)
        _execute_best_candidate_daily_risk._entry_daily_risk_wrapped_v11 = True  # type: ignore[attr-defined]
        _execute_best_candidate_daily_risk._original = old_execute  # type: ignore[attr-defined]
        ec._execute_best_candidate = _execute_best_candidate_daily_risk

    if callable(old_record_exit) and not getattr(old_record_exit, "_entry_daily_actual_trade_wrapped_v11", False):
        def _record_exit_event_daily_actual_trade(symbol: Any, *, pnl: float, reason: str, now=None) -> None:
            try:
                old_record_exit(symbol, pnl=pnl, reason=reason, now=now)
            finally:
                try:
                    _record_actual_trade(symbol, float(pnl or 0.0))
                except Exception:
                    logger.exception("[ENTRY DAILY RISK] record actual trade failed symbol=%s", symbol)
        _record_exit_event_daily_actual_trade._entry_daily_actual_trade_wrapped_v11 = True  # type: ignore[attr-defined]
        _record_exit_event_daily_actual_trade._original = old_record_exit  # type: ignore[attr-defined]
        stg.record_exit_event = _record_exit_event_daily_actual_trade

    _INSTALLED = True
    logger.warning(
        "[ENTRY DAILY RISK] installed v1.1 buy_enabled=%s max_actual_trades_per_symbol=%s max_daily_loss=%s count_mode=actual_exit_only",
        _env_bool("ENTRY_BUY_ENABLED", False),
        _env_int("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL", 2),
        _env_float("ENTRY_SYMBOL_MAX_DAILY_LOSS_YEN", -2000.0),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY DAILY RISK] auto install failed")

__all__ = ["install"]
