# ============================================================
# File   : trading/backtest_replay/loader.py
# Version: Ver01-REPLAY-LOADER
# ============================================================

from __future__ import annotations

import sqlite3
from typing import Optional

import pandas as pd

from .paths import ReplayPaths


class ReplayLoader:
    def __init__(self, trade_date: str):
        self.trade_date = trade_date
        self.paths = ReplayPaths(trade_date)

    def _read_sql(self, db_path: str, sql: str) -> pd.DataFrame:
        conn = sqlite3.connect(db_path)
        try:
            return pd.read_sql_query(sql, conn)
        finally:
            conn.close()

    def load_summary_1m(self, symbol: Optional[str] = None) -> pd.DataFrame:
        sql = 'SELECT * FROM stock_summary_1min'
        if symbol:
            sql += f" WHERE symbol='{symbol}'"
        sql += ' ORDER BY datetime'
        return self._read_sql(self.paths.summary_db, sql)

    def load_ranking(self, symbol: Optional[str] = None) -> pd.DataFrame:
        sql = 'SELECT * FROM ranking_snapshot_1min'
        if symbol:
            sql += f" WHERE symbol='{symbol}'"
        sql += ' ORDER BY datetime'
        return self._read_sql(self.paths.ranking_db, sql)

    def load_candidate_history(self, symbol: Optional[str] = None) -> pd.DataFrame:
        sql = 'SELECT * FROM candidate_history'
        if symbol:
            sql += f" WHERE symbol='{symbol}'"
        sql += ' ORDER BY datetime'
        return self._read_sql(self.paths.audit_db, sql)

    def load_order_history(self, symbol: Optional[str] = None) -> pd.DataFrame:
        sql = 'SELECT * FROM order_history'
        if symbol:
            sql += f" WHERE symbol='{symbol}'"
        sql += ' ORDER BY datetime'
        return self._read_sql(self.paths.audit_db, sql)

    def load_exit_history(self, symbol: Optional[str] = None) -> pd.DataFrame:
        sql = 'SELECT * FROM exit_history'
        if symbol:
            sql += f" WHERE symbol='{symbol}'"
        sql += ' ORDER BY datetime'
        return self._read_sql(self.paths.audit_db, sql)

    def load_five_sec_bars(self, symbol: Optional[str] = None) -> pd.DataFrame:
        sql = 'SELECT * FROM five_sec_bars'
        if symbol:
            sql += f" WHERE symbol='{symbol}'"
        sql += ' ORDER BY bucket_time'
        return self._read_sql(self.paths.market_audit_db, sql)

    def load_spread_history(self, symbol: Optional[str] = None) -> pd.DataFrame:
        sql = 'SELECT * FROM spread_snapshots'
        if symbol:
            sql += f" WHERE symbol='{symbol}'"
        sql += ' ORDER BY datetime'
        return self._read_sql(self.paths.market_audit_db, sql)
