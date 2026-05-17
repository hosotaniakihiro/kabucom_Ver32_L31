# ============================================================
# File   : trading/backtest_replay/pnl_simulator.py
# Version: Ver01-PNL-SIMULATOR
# ------------------------------------------------------------
# audit_YYYYMMDD.db の order_history / exit_history から
# バックテスト用の概算損益を計算する。
# ------------------------------------------------------------
# 注意:
#   - 実手数料・金利・貸株料は初期値0
#   - FILLED が不足する日は、ORDER_ACCEPTED等から推定する
#   - まずは検証用の概算集計基盤
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from .loader import ReplayLoader


@dataclass
class PnLConfig:
    commission_per_trade: float = 0.0
    borrow_fee_per_trade: float = 0.0
    default_qty: int = 100
    slippage_bps: float = 0.0


class PnLSimulator:
    def __init__(self, trade_date: str, symbol: str | None = None, config: PnLConfig | None = None):
        self.trade_date = trade_date
        self.symbol = symbol
        self.config = config or PnLConfig()
        self.loader = ReplayLoader(trade_date)

    @staticmethod
    def _safe_float(v: Any, default: float = 0.0) -> float:
        try:
            if v is None or v == '':
                return default
            return float(v)
        except Exception:
            return default

    @staticmethod
    def _safe_int(v: Any, default: int = 0) -> int:
        try:
            if v is None or v == '':
                return default
            return int(float(v))
        except Exception:
            return default

    def _apply_slippage(self, price: float, side: str, is_entry: bool) -> float:
        bps = self.config.slippage_bps
        if price <= 0 or bps <= 0:
            return price

        rate = bps / 10000.0
        side_u = str(side).upper()

        # BUY entry / SELL exit は高く約定、SELL entry / BUY exit は安く約定として保守的に扱う
        if is_entry:
            if side_u == 'BUY':
                return price * (1.0 + rate)
            if side_u == 'SELL':
                return price * (1.0 - rate)
        else:
            if side_u == 'BUY':
                return price * (1.0 - rate)
            if side_u == 'SELL':
                return price * (1.0 + rate)

        return price

    def load_orders(self) -> pd.DataFrame:
        try:
            return self.loader.load_order_history(self.symbol)
        except Exception:
            return pd.DataFrame()

    def load_exits(self) -> pd.DataFrame:
        try:
            return self.loader.load_exit_history(self.symbol)
        except Exception:
            return pd.DataFrame()

    def build_trades(self) -> pd.DataFrame:
        orders = self.load_orders()
        exits = self.load_exits()

        if orders.empty:
            return pd.DataFrame()

        orders = orders.copy()
        orders['datetime'] = pd.to_datetime(orders.get('datetime'), errors='coerce')
        orders = orders.sort_values('datetime')

        # entry候補: FILLED / ORDER_ACCEPTED / SENT / CANDIDATE_SELECTED のうち価格があるもの
        entry_orders = orders[orders.get('status', '').astype(str).isin([
            'FILLED', 'ORDER_ACCEPTED', 'SENT', 'CANDIDATE_SELECTED'
        ])].copy()

        if entry_orders.empty:
            return pd.DataFrame()

        if exits is not None and not exits.empty:
            exits = exits.copy()
            exits['datetime'] = pd.to_datetime(exits.get('datetime'), errors='coerce')
            exits = exits.sort_values('datetime')
        else:
            exits = pd.DataFrame()

        rows = []

        for _, ent in entry_orders.iterrows():
            symbol = str(ent.get('symbol') or '')
            side = str(ent.get('side') or '').upper()
            if not symbol or side not in ('BUY', 'SELL'):
                continue

            entry_time = ent.get('datetime')
            if pd.isna(entry_time):
                continue

            qty = self._safe_int(ent.get('qty'), self.config.default_qty)
            if qty <= 0:
                qty = self.config.default_qty

            entry_price = self._safe_float(ent.get('filled_price'), 0.0)
            if entry_price <= 0:
                entry_price = self._safe_float(ent.get('price'), 0.0)
            if entry_price <= 0:
                continue

            exit_row = None
            if not exits.empty:
                xs = exits[(exits.get('symbol').astype(str) == symbol) & (exits['datetime'] >= entry_time)]
                if not xs.empty:
                    exit_row = xs.iloc[0]

            if exit_row is None:
                continue

            exit_time = exit_row.get('datetime')
            exit_price = self._safe_float(exit_row.get('current_price'), 0.0)
            if exit_price <= 0:
                continue

            entry_price_adj = self._apply_slippage(entry_price, side, is_entry=True)
            exit_price_adj = self._apply_slippage(exit_price, side, is_entry=False)

            if side == 'BUY':
                gross_pnl = (exit_price_adj - entry_price_adj) * qty
            else:
                gross_pnl = (entry_price_adj - exit_price_adj) * qty

            cost = self.config.commission_per_trade * 2 + self.config.borrow_fee_per_trade
            net_pnl = gross_pnl - cost

            rows.append({
                'symbol': symbol,
                'side': side,
                'qty': qty,
                'entry_time': entry_time,
                'exit_time': exit_time,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'entry_price_adj': entry_price_adj,
                'exit_price_adj': exit_price_adj,
                'exit_reason': exit_row.get('exit_reason'),
                'gross_pnl': gross_pnl,
                'cost': cost,
                'net_pnl': net_pnl,
                'win': 1 if net_pnl > 0 else 0,
                'holding_seconds': (exit_time - entry_time).total_seconds() if pd.notna(exit_time) else None,
            })

        if not rows:
            return pd.DataFrame()

        return pd.DataFrame(rows)

    def summarize(self) -> dict:
        trades = self.build_trades()
        if trades.empty:
            return {
                'trade_date': self.trade_date,
                'symbol': self.symbol,
                'trades': 0,
                'net_pnl': 0.0,
                'gross_pnl': 0.0,
                'win_rate': 0.0,
                'avg_pnl': 0.0,
                'max_win': 0.0,
                'max_loss': 0.0,
            }

        return {
            'trade_date': self.trade_date,
            'symbol': self.symbol,
            'trades': int(len(trades)),
            'net_pnl': float(trades['net_pnl'].sum()),
            'gross_pnl': float(trades['gross_pnl'].sum()),
            'win_rate': float(trades['win'].mean()),
            'avg_pnl': float(trades['net_pnl'].mean()),
            'max_win': float(trades['net_pnl'].max()),
            'max_loss': float(trades['net_pnl'].min()),
        }
