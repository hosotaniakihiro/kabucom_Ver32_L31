# ============================================================
# File   : core/startup/entry_daily_risk_runtime_patch.py
# Version: V1.7-WINNING-SYMBOL-REENTRY
# ------------------------------------------------------------
# 導入ルール:
#   1. BUY新規も許可する
#   2. 同一銘柄は当日最大2回をデフォルトにする
#   3. 発注成功時点でも同一銘柄の当日エントリー済みとして記録する
#   4. 同一銘柄で負けた場合は停止できる
#   5. ただし勝敗が良い銘柄は当日再エントリーを許可する
#   6. 1銘柄の当日損失 -1500円で停止する
#   7. システム全体の当日損失が -50000円に到達したら新規停止する
#   8. 当日の返済済み取引回数が30回に到達したら新規停止する
#   9. 連敗が20回に到達したら新規停止する
#
# V1.7:
#   - 勝ち銘柄は再エントリー許可。
#   - 条件: win_count > loss_count かつ daily_pnl > 0。
#   - 勝ち銘柄は ENTRY_WINNING_SYMBOL_MAX_DAILY_ENTRIES まで許可する。
#   - 勝ち銘柄では、未決済/未約定を含む entry_sent_count だけで上限停止しない。
#     実際の返済済み entry_count を優先する。
#   - stop_after_first_loss は、ネット損益がプラスで勝ち越しなら停止しない。
#
# 環境変数:
#   ENTRY_BUY_ENABLED=1
#   ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL=2
#   ENTRY_WINNING_SYMBOL_REENTRY_ENABLED=1
#   ENTRY_WINNING_SYMBOL_MAX_DAILY_ENTRIES=4
#   ENTRY_WINNING_SYMBOL_MIN_DAILY_PNL=1
#   ENTRY_WINNING_SYMBOL_REQUIRE_WIN_GT_LOSS=1
#   ENTRY_WINNING_SYMBOL_IGNORE_SENT_ONLY=1
#   ENTRY_STOP_SYMBOL_AFTER_FIRST_LOSS=1
#   ENTRY_STOP_AFTER_FIRST_LOSS_ONLY_IF_NET_NEGATIVE=1
#   ENTRY_SYMBOL_MAX_DAILY_LOSS_YEN=-1500
#   ENTRY_GLOBAL_MAX_DAILY_LOSS_YEN=-50000
#   ENTRY_GLOBAL_MAX_DAILY_TRADES=30
#   ENTRY_GLOBAL_MAX_CONSECUTIVE_LOSSES=20
#   ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY=1
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


def _now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


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
            entry_sent_count INTEGER NOT NULL DEFAULT 0,
            daily_pnl REAL NOT NULL DEFAULT 0,
            win_count INTEGER NOT NULL DEFAULT 0,
            loss_count INTEGER NOT NULL DEFAULT 0,
            last_entry_time TEXT DEFAULT '',
            last_exit_time TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            PRIMARY KEY (trade_date, symbol)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS global_daily_entry_risk (
            trade_date TEXT PRIMARY KEY,
            trade_count INTEGER NOT NULL DEFAULT 0,
            entry_sent_count INTEGER NOT NULL DEFAULT 0,
            daily_pnl REAL NOT NULL DEFAULT 0,
            win_count INTEGER NOT NULL DEFAULT 0,
            loss_count INTEGER NOT NULL DEFAULT 0,
            consecutive_losses INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT ''
        )
        """
    )
    alter_stmts = [
        "ALTER TABLE symbol_daily_entry_risk ADD COLUMN win_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE symbol_daily_entry_risk ADD COLUMN loss_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE symbol_daily_entry_risk ADD COLUMN entry_sent_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE global_daily_entry_risk ADD COLUMN entry_sent_count INTEGER NOT NULL DEFAULT 0",
    ]
    for ddl in alter_stmts:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def _get_symbol_row(symbol: str) -> Dict[str, Any]:
    symbol = _norm_symbol(symbol)
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT entry_count, entry_sent_count, daily_pnl, win_count, loss_count, last_entry_time, last_exit_time
            FROM symbol_daily_entry_risk
            WHERE trade_date=? AND symbol=?
            """,
            (_today(), symbol),
        )
        row = cur.fetchone()
        if row:
            return {
                "entry_count": int(row[0] or 0),
                "entry_sent_count": int(row[1] or 0),
                "daily_pnl": float(row[2] or 0.0),
                "win_count": int(row[3] or 0),
                "loss_count": int(row[4] or 0),
                "last_entry_time": row[5] or "",
                "last_exit_time": row[6] or "",
            }
    return {"entry_count": 0, "entry_sent_count": 0, "daily_pnl": 0.0, "win_count": 0, "loss_count": 0, "last_entry_time": "", "last_exit_time": ""}


def _get_global_row() -> Dict[str, Any]:
    with _connect() as conn:
        cur = conn.execute(
            """
            SELECT trade_count, entry_sent_count, daily_pnl, win_count, loss_count, consecutive_losses, updated_at
            FROM global_daily_entry_risk
            WHERE trade_date=?
            """,
            (_today(),),
        )
        row = cur.fetchone()
        if row:
            return {
                "trade_count": int(row[0] or 0),
                "entry_sent_count": int(row[1] or 0),
                "daily_pnl": float(row[2] or 0.0),
                "win_count": int(row[3] or 0),
                "loss_count": int(row[4] or 0),
                "consecutive_losses": int(row[5] or 0),
                "updated_at": row[6] or "",
            }
    return {"trade_count": 0, "entry_sent_count": 0, "daily_pnl": 0.0, "win_count": 0, "loss_count": 0, "consecutive_losses": 0, "updated_at": ""}


def _record_entry_sent(symbol: str) -> None:
    """発注成功時点で、同一銘柄の当日再エントリー抑止用カウントを増やす。"""
    symbol = _norm_symbol(symbol)
    if not symbol:
        return

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO symbol_daily_entry_risk
                (trade_date, symbol, entry_count, entry_sent_count, daily_pnl, win_count, loss_count, last_entry_time, updated_at)
            VALUES (?, ?, 0, 1, 0, 0, 0, ?, ?)
            ON CONFLICT(trade_date, symbol) DO UPDATE SET
                entry_sent_count = entry_sent_count + 1,
                last_entry_time = excluded.last_entry_time,
                updated_at = excluded.updated_at
            """,
            (_today(), symbol, _now_iso(), _now_iso()),
        )
        conn.execute(
            """
            INSERT INTO global_daily_entry_risk
                (trade_date, trade_count, entry_sent_count, daily_pnl, win_count, loss_count, consecutive_losses, updated_at)
            VALUES (?, 0, 1, 0, 0, 0, 0, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                entry_sent_count = entry_sent_count + 1,
                updated_at = excluded.updated_at
            """,
            (_today(), _now_iso()),
        )
        conn.commit()

    logger.warning(
        "[ENTRY DAILY RISK] entry_sent recorded symbol=%s symbol_row=%s global_row=%s",
        symbol,
        _get_symbol_row(symbol),
        _get_global_row(),
    )


def _record_actual_trade(symbol: str, pnl: float) -> None:
    """実際に約定して返済された時、当日実現損益/勝敗を更新する。"""
    symbol = _norm_symbol(symbol)
    if not symbol:
        return
    pnl_f = float(pnl or 0.0)
    is_win = 1 if pnl_f > 0 else 0
    is_loss = 1 if pnl_f < 0 else 0
    new_consecutive_losses_expr = "consecutive_losses + 1" if is_loss else "0"

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO symbol_daily_entry_risk
                (trade_date, symbol, entry_count, entry_sent_count, daily_pnl, win_count, loss_count, last_entry_time, last_exit_time, updated_at)
            VALUES (?, ?, 1, 0, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, symbol) DO UPDATE SET
                entry_count = entry_count + 1,
                daily_pnl = daily_pnl + excluded.daily_pnl,
                win_count = win_count + excluded.win_count,
                loss_count = loss_count + excluded.loss_count,
                last_exit_time = excluded.last_exit_time,
                updated_at = excluded.updated_at
            """,
            (_today(), symbol, pnl_f, is_win, is_loss, _now_iso(), _now_iso(), _now_iso()),
        )
        conn.execute(
            """
            INSERT INTO global_daily_entry_risk
                (trade_date, trade_count, entry_sent_count, daily_pnl, win_count, loss_count, consecutive_losses, updated_at)
            VALUES (?, 1, 0, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                trade_count = trade_count + 1,
                daily_pnl = daily_pnl + excluded.daily_pnl,
                win_count = win_count + excluded.win_count,
                loss_count = loss_count + excluded.loss_count,
                consecutive_losses = """ + new_consecutive_losses_expr + """,
                updated_at = excluded.updated_at
            """,
            (_today(), pnl_f, is_win, is_loss, 1 if is_loss else 0, _now_iso()),
        )
        conn.commit()

    logger.warning(
        "[ENTRY DAILY RISK] actual_trade recorded symbol=%s pnl=%s symbol_row=%s global_row=%s",
        symbol,
        pnl_f,
        _get_symbol_row(symbol),
        _get_global_row(),
    )


def _is_winning_symbol(srow: Dict[str, Any]) -> bool:
    if not _env_bool("ENTRY_WINNING_SYMBOL_REENTRY_ENABLED", True):
        return False
    win_count = int(srow.get("win_count") or 0)
    loss_count = int(srow.get("loss_count") or 0)
    daily_pnl = float(srow.get("daily_pnl") or 0.0)
    min_pnl = _env_float("ENTRY_WINNING_SYMBOL_MIN_DAILY_PNL", 1.0)
    if daily_pnl < min_pnl:
        return False
    if _env_bool("ENTRY_WINNING_SYMBOL_REQUIRE_WIN_GT_LOSS", True):
        return win_count > loss_count
    return win_count >= 1


def _risk_block_reason(symbol: str, side: str) -> Tuple[bool, str, Dict[str, Any]]:
    symbol = _norm_symbol(symbol)
    side_u = str(side or "").upper()
    if side_u == "BUY" and not _env_bool("ENTRY_BUY_ENABLED", True):
        return True, "BUY_DISABLED_BY_DAILY_RISK_PATCH", {"symbol": symbol, "side": side_u}

    srow = _get_symbol_row(symbol)
    grow = _get_global_row()

    max_entries = _env_int("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL", 2)
    winning_max_entries = _env_int("ENTRY_WINNING_SYMBOL_MAX_DAILY_ENTRIES", 4)
    symbol_max_loss = _env_float("ENTRY_SYMBOL_MAX_DAILY_LOSS_YEN", -1500.0)
    stop_after_first_loss = _env_bool("ENTRY_STOP_SYMBOL_AFTER_FIRST_LOSS", True)
    stop_after_first_loss_only_net_negative = _env_bool("ENTRY_STOP_AFTER_FIRST_LOSS_ONLY_IF_NET_NEGATIVE", True)
    global_max_loss = _env_float("ENTRY_GLOBAL_MAX_DAILY_LOSS_YEN", -50000.0)
    global_max_trades = _env_int("ENTRY_GLOBAL_MAX_DAILY_TRADES", 30)
    global_max_consec_losses = _env_int("ENTRY_GLOBAL_MAX_CONSECUTIVE_LOSSES", 20)

    entry_count = int(srow.get("entry_count") or 0)
    entry_sent_count = int(srow.get("entry_sent_count") or 0)
    loss_count = int(srow.get("loss_count") or 0)
    daily_pnl = float(srow.get("daily_pnl") or 0.0)
    winning_symbol = _is_winning_symbol(srow)

    if winning_symbol and _env_bool("ENTRY_WINNING_SYMBOL_IGNORE_SENT_ONLY", True):
        symbol_seen_entries = entry_count
        count_mode = "actual_entry_count_only_for_winning_symbol"
    else:
        symbol_seen_entries = max(entry_count, entry_sent_count)
        count_mode = "max_entry_count_or_sent_count"

    effective_max_entries = max_entries
    if winning_symbol:
        effective_max_entries = max(max_entries, winning_max_entries)

    if global_max_trades > 0 and int(grow.get("trade_count") or 0) >= global_max_trades:
        return True, "GLOBAL_DAILY_TRADE_LIMIT", {"symbol": symbol, "side": side_u, "max_trades": global_max_trades, **grow}

    if float(grow.get("daily_pnl") or 0.0) <= global_max_loss:
        return True, "GLOBAL_DAILY_LOSS_LIMIT", {"symbol": symbol, "side": side_u, "max_loss": global_max_loss, **grow}

    if global_max_consec_losses > 0 and int(grow.get("consecutive_losses") or 0) >= global_max_consec_losses:
        return True, "GLOBAL_CONSECUTIVE_LOSS_LIMIT", {"symbol": symbol, "side": side_u, "max_consecutive_losses": global_max_consec_losses, **grow}

    if stop_after_first_loss and loss_count >= 1:
        if not (stop_after_first_loss_only_net_negative and winning_symbol and daily_pnl > 0):
            return True, "SYMBOL_STOP_AFTER_FIRST_LOSS", {
                "symbol": symbol,
                "side": side_u,
                "winning_symbol": winning_symbol,
                "stop_after_first_loss_only_net_negative": stop_after_first_loss_only_net_negative,
                **srow,
            }

    if float(srow.get("daily_pnl") or 0.0) <= symbol_max_loss:
        return True, "SYMBOL_DAILY_LOSS_LIMIT", {"symbol": symbol, "side": side_u, "max_loss": symbol_max_loss, **srow}

    if effective_max_entries > 0 and symbol_seen_entries >= effective_max_entries:
        detail = {
            "symbol": symbol,
            "side": side_u,
            "max_entries": effective_max_entries,
            "base_max_entries": max_entries,
            "winning_max_entries": winning_max_entries,
            "winning_symbol": winning_symbol,
            "symbol_seen_entries": symbol_seen_entries,
            "count_mode": count_mode,
            **srow,
        }
        return True, "SYMBOL_DAILY_ENTRY_LIMIT", detail

    return False, "", {
        "symbol": srow,
        "global": grow,
        "winning_symbol": winning_symbol,
        "effective_max_entries": effective_max_entries,
        "symbol_seen_entries": symbol_seen_entries,
        "count_mode": count_mode,
    }


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

    if not getattr(old_execute, "_entry_daily_risk_wrapped_v17", False):
        original = getattr(old_execute, "_original", None) if getattr(old_execute, "_entry_daily_risk_wrapped_v16", False) else old_execute
        if not callable(original):
            original = old_execute

        def _execute_best_candidate_daily_risk(item: dict, boost_active: bool) -> bool:
            symbol = ""
            side = ""
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

            ok = original(item, boost_active=boost_active)

            try:
                if bool(ok) and _env_bool("ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY", True):
                    _record_entry_sent(symbol or item.get("symbol"))
            except Exception:
                logger.exception("[ENTRY DAILY RISK] record entry_sent failed symbol=%s", symbol)

            return ok

        _execute_best_candidate_daily_risk._entry_daily_risk_wrapped_v16 = True  # type: ignore[attr-defined]
        _execute_best_candidate_daily_risk._entry_daily_risk_wrapped_v17 = True  # type: ignore[attr-defined]
        _execute_best_candidate_daily_risk._original = original  # type: ignore[attr-defined]
        ec._execute_best_candidate = _execute_best_candidate_daily_risk

    if callable(old_record_exit) and not getattr(old_record_exit, "_entry_daily_actual_trade_wrapped_v17", False):
        original_exit = getattr(old_record_exit, "_original", None) if getattr(old_record_exit, "_entry_daily_actual_trade_wrapped_v16", False) else old_record_exit
        if not callable(original_exit):
            original_exit = old_record_exit

        def _record_exit_event_daily_actual_trade(symbol: Any, *, pnl: float, reason: str, now=None) -> None:
            try:
                original_exit(symbol, pnl=pnl, reason=reason, now=now)
            finally:
                try:
                    _record_actual_trade(symbol, float(pnl or 0.0))
                except Exception:
                    logger.exception("[ENTRY DAILY RISK] record actual trade failed symbol=%s", symbol)

        _record_exit_event_daily_actual_trade._entry_daily_actual_trade_wrapped_v16 = True  # type: ignore[attr-defined]
        _record_exit_event_daily_actual_trade._entry_daily_actual_trade_wrapped_v17 = True  # type: ignore[attr-defined]
        _record_exit_event_daily_actual_trade._original = original_exit  # type: ignore[attr-defined]
        stg.record_exit_event = _record_exit_event_daily_actual_trade

    _INSTALLED = True
    logger.warning(
        "[ENTRY DAILY RISK] installed v1.7 buy_enabled=%s max_symbol_entries=%s winning_reentry=%s winning_max_entries=%s winning_ignore_sent_only=%s stop_after_first_loss=%s stop_first_loss_only_net_negative=%s symbol_max_loss=%s global_max_loss=%s global_max_trades=%s global_max_consecutive_losses=%s count_mode=entry_sent_and_actual_exit_with_winning_reentry",
        _env_bool("ENTRY_BUY_ENABLED", True),
        _env_int("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL", 2),
        _env_bool("ENTRY_WINNING_SYMBOL_REENTRY_ENABLED", True),
        _env_int("ENTRY_WINNING_SYMBOL_MAX_DAILY_ENTRIES", 4),
        _env_bool("ENTRY_WINNING_SYMBOL_IGNORE_SENT_ONLY", True),
        _env_bool("ENTRY_STOP_SYMBOL_AFTER_FIRST_LOSS", True),
        _env_bool("ENTRY_STOP_AFTER_FIRST_LOSS_ONLY_IF_NET_NEGATIVE", True),
        _env_float("ENTRY_SYMBOL_MAX_DAILY_LOSS_YEN", -1500.0),
        _env_float("ENTRY_GLOBAL_MAX_DAILY_LOSS_YEN", -50000.0),
        _env_int("ENTRY_GLOBAL_MAX_DAILY_TRADES", 30),
        _env_int("ENTRY_GLOBAL_MAX_CONSECUTIVE_LOSSES", 20),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY DAILY RISK] auto install failed")

__all__ = ["install"]
