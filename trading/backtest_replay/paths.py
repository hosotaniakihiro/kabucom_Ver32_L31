# ============================================================
# File   : trading/backtest_replay/paths.py
# Version: Ver01-REPLAY-PATHS
# ============================================================

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ReplayPaths:
    trade_date: str

    @property
    def summary_db(self) -> str:
        return (
            r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary\'
            f'summary{self.trade_date}.db'
        )

    @property
    def ranking_db(self) -> str:
        return (
            r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\'
            f'ranking{self.trade_date}.db'
        )

    @property
    def push_db(self) -> str:
        return (
            r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\push\'
            f'push{self.trade_date}.db'
        )

    @property
    def audit_db(self) -> str:
        return (
            r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit\'
            f'audit_{self.trade_date}.db'
        )

    @property
    def market_audit_db(self) -> str:
        return (
            r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit\'
            f'market_audit_{self.trade_date}.db'
        )
