# ============================================================
# File   : scripts/check_audit_logging_status.py
# Version: Ver01-AUDIT-LOGGING-STATUS-CHECKER
# ------------------------------------------------------------
# 監査ログ/バックテスト用DB保存が有効か確認する診断スクリプト。
# 実注文は出さない。
# ============================================================

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _print(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


def _table_names(db_path: str) -> list[str]:
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def main():
    _print("========== AUDIT LOGGING STATUS CHECK START ==========")
    _print(f"PROJECT_ROOT={PROJECT_ROOT}")

    try:
        from trading.audit_logging.install_audit_logging import install_audit_logging
        ok = install_audit_logging()
        _print(f"install_audit_logging ok={ok}")
    except Exception as e:
        _print(f"install_audit_logging failed: {e}")
        raise

    try:
        from trading.audit_logging.recorder import get_audit_db_path, ensure_audit_db
        ensure_audit_db()
        audit_db = get_audit_db_path()
        _print(f"audit_db={audit_db}")
        _print(f"audit_db_exists={os.path.exists(audit_db)}")
        _print(f"audit_tables={_table_names(audit_db)}")
    except Exception as e:
        _print(f"audit db check failed: {e}")
        raise

    try:
        from trading.audit_logging.five_sec_market_audit import get_market_audit_db_path, ensure_market_audit_db
        ensure_market_audit_db()
        market_db = get_market_audit_db_path()
        _print(f"market_audit_db={market_db}")
        _print(f"market_audit_db_exists={os.path.exists(market_db)}")
        _print(f"market_audit_tables={_table_names(market_db)}")
    except Exception as e:
        _print(f"market audit db check failed: {e}")
        raise

    try:
        from trading.audit_logging.entry_audit import audit_entry_skip, audit_order
        audit_entry_skip(symbol="__TEST__", reason="AUDIT_STATUS_CHECK", detail={"ok": True})
        audit_order(
            symbol="__TEST__",
            side="BUY",
            qty=100,
            order_type="TEST",
            price=100.0,
            status="STATUS_CHECK",
            reason="no real order",
        )
        _print("test audit rows inserted")
    except Exception as e:
        _print(f"test insert failed: {e}")
        raise

    _print("========== AUDIT LOGGING STATUS CHECK DONE ==========")


if __name__ == "__main__":
    main()
