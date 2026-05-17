from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .loader import ReplayLoader


@dataclass
class PnLConfig:
    commission_per_trade: float = 0.0
    slippage_bps: float = 0.0
    borrow_fee_per_trade: float = 0.0


class ReplayPnLSimulator:
    def __init__(self, trade_date: str, symbol: Optional[str] = None, config: Optional[PnLConfig] = None):
        self.trade_date = trade_date
        self.symbol = symbol
        self.loader = ReplayLoader(trade_date)
        self.config = config or PnLConfig()

    def _load_fills(self) -> pd.DataFrame:
        df = self.loader.load_order_history(self.symbol)
        if df.empty:
            return df

        if 'datetime' in df.columns:
            df['datetime'] = pd.to_datetime(df['datetime'], errors='coerce')

        if 'filled_price' in df.columns:
            df['exec_price'] = pd.to_numeric(df['filled_price'], errors='coerce')
        else:
            df['exec_price'] = pd.to_numeric(df.get('price'), errors='coerce')

        df = df[df['exec_price'].fillna(0) > 0].copy()
        df = df.sort_values('datetime').reset_index(drop=True)
        return df

    def simulate(self) -> pd.DataFrame:
        fills = self._load_fills()
        if fills.empty:
            return pd.DataFrame()

        trades = []
        open_positions = {}

        for _, row in fills.iterrows():
            symbol = str(row.get('symbol') or '')
            side = str(row.get('side') or '').upper()
            qty = int(float(row.get('qty') or 0))
            price = float(row.get('exec_price') or 0)
            dt = row.get('datetime')

            if not symbol or qty <= 0 or price <= 0:
                continue

            current = open_positions.get(symbol)

            if current is None:
                open_positions[symbol] = {
                    'side': side,
                    'qty': qty,
                    'price': price,
                    'time': dt,
                }
                continue

            if current['side'] == side:
                continue

            if current['side'] == 'BUY':
                gross = (price - current['price']) * qty
            else:
                gross = (current['price'] - price) * qty

            trades.append({
                'symbol': symbol,
                'entry_side': current['side'],
                'entry_price': current['price'],
                'exit_price': price,
                'qty': qty,
                'entry_time': current['time'],
                'exit_time': dt,
                'gross_pnl': gross,
            })

            open_positions.pop(symbol, None)

        return pd.DataFrame(trades)

    def summary(self) -> dict:
        df = self.simulate()
        if df.empty:
            return {
                'trade_date': self.trade_date,
                'trades': 0,
                'gross_pnl': 0.0,
            }

        return {
            'trade_date': self.trade_date,
            'trades': int(len(df)),
            'gross_pnl': float(df['gross_pnl'].sum()),
            'win_rate': float((df['gross_pnl'] > 0).mean()),
        }
