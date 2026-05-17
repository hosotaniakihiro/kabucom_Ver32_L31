# ============================================================
# File   : trading/backtest_replay/replay_trading_v1.py
# Version: Ver01-COMPLETE-REPLAY-TRADING
# ------------------------------------------------------------
# 過去データを使って、仮想エントリー → 仮想イグジットまでを再現する。
# v1 は安全な最小構成:
#   - AI Replay の AI_OK をエントリー候補にする
#   - 5秒足 close を価格として使う
#   - BUY / SELL 両対応
#   - stop_loss / trail_drop / stagnation exit を検証
#   - 実注文は絶対に出さない
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .loader import ReplayLoader
from .ai_replay_v1 import AIReplayEngine, AIReplayConfig


@dataclass
class ReplayTradingConfig:
    stop_loss_pct: float = 0.30
    trail_drop_pct: float = 0.30
    stagnation_seconds: int = 300
    stagnation_move_pct: float = 0.05
    min_close_price: float = 200.0
    min_volume: float = 30000.0
    min_turnover: float = 10000000.0
    max_positions: int = 1
    qty: int = 100
    ai_confidence_min: float = 0.55
    max_rows: int = 3000


class ReplayTradingEngine:
    def __init__(self, trade_date: str, symbol: Optional[str] = None, config: Optional[ReplayTradingConfig] = None):
        self.trade_date = trade_date
        self.symbol = symbol
        self.config = config or ReplayTradingConfig()
        self.loader = ReplayLoader(trade_date)

    @staticmethod
    def _safe_float(v, default: float = 0.0) -> float:
        try:
            if v is None or v == '':
                return default
            return float(v)
        except Exception:
            return default

    def _load_prices_5s(self) -> pd.DataFrame:
        df = self.loader.load_five_sec_bars(self.symbol)
        if df.empty:
            return df
        df = df.copy()
        df['event_time'] = pd.to_datetime(df.get('bucket_time'), errors='coerce')
        df['close_num'] = pd.to_numeric(df.get('close'), errors='coerce').fillna(0.0)
        df = df[df['close_num'] > 0].sort_values(['event_time', 'symbol']).reset_index(drop=True)
        return df

    def _load_ai_ok(self) -> pd.DataFrame:
        ai = AIReplayEngine(
            trade_date=self.trade_date,
            symbol=self.symbol,
            config=AIReplayConfig(
                min_close_price=self.config.min_close_price,
                min_volume=self.config.min_volume,
                min_turnover=self.config.min_turnover,
                max_rows=self.config.max_rows,
            ),
        ).replay()
        if ai.empty:
            return ai
        ai = ai.copy()
        ai['event_time'] = pd.to_datetime(ai.get('datetime'), errors='coerce')
        ai['ai_confidence'] = pd.to_numeric(ai.get('ai_confidence'), errors='coerce').fillna(0.0)
        ai = ai[(ai.get('ai_allow') == True) & (ai['ai_confidence'] >= self.config.ai_confidence_min)].copy()
        return ai.sort_values(['event_time', 'symbol', 'side']).reset_index(drop=True)

    def run(self) -> pd.DataFrame:
        prices = self._load_prices_5s()
        signals = self._load_ai_ok()

        if prices.empty or signals.empty:
            return pd.DataFrame()

        trades = []
        open_pos = {}

        # signal 時刻以降の最初の5秒足 close で仮想エントリー。
        for _, sig in signals.iterrows():
            symbol = str(sig.get('symbol') or '')
            side = str(sig.get('side') or '').upper()
            sig_time = sig.get('event_time')

            if not symbol or side not in ('BUY', 'SELL') or pd.isna(sig_time):
                continue

            if symbol in open_pos:
                continue
            if len(open_pos) >= self.config.max_positions:
                continue

            p = prices[(prices['symbol'].astype(str) == symbol) & (prices['event_time'] >= sig_time)].copy()
            if p.empty:
                continue

            entry_row = p.iloc[0]
            entry_time = entry_row['event_time']
            entry_price = self._safe_float(entry_row['close_num'], 0.0)
            if entry_price <= 0:
                continue

            highest = entry_price
            lowest = entry_price
            exit_time = None
            exit_price = None
            exit_reason = None

            future = p[p['event_time'] > entry_time].copy()
            for _, bar in future.iterrows():
                now = bar['event_time']
                price = self._safe_float(bar['close_num'], 0.0)
                if price <= 0:
                    continue

                highest = max(highest, price)
                lowest = min(lowest, price)
                holding_seconds = (now - entry_time).total_seconds()

                if side == 'BUY':
                    adverse_pct = (entry_price - price) / entry_price * 100.0
                    trail_pct = (highest - price) / highest * 100.0 if highest > 0 else 0.0
                    move_pct = abs(price - entry_price) / entry_price * 100.0
                else:
                    adverse_pct = (price - entry_price) / entry_price * 100.0
                    trail_pct = (price - lowest) / lowest * 100.0 if lowest > 0 else 0.0
                    move_pct = abs(entry_price - price) / entry_price * 100.0

                if adverse_pct >= self.config.stop_loss_pct:
                    exit_time = now
                    exit_price = price
                    exit_reason = f'STOP_LOSS_{self.config.stop_loss_pct}'
                    break

                if trail_pct >= self.config.trail_drop_pct:
                    exit_time = now
                    exit_price = price
                    exit_reason = f'TRAIL_DROP_{self.config.trail_drop_pct}'
                    break

                if holding_seconds >= self.config.stagnation_seconds and move_pct <= self.config.stagnation_move_pct:
                    exit_time = now
                    exit_price = price
                    exit_reason = 'STAGNATION_EXIT'
                    break

            if exit_time is None:
                if not future.empty:
                    last = future.iloc[-1]
                    exit_time = last['event_time']
                    exit_price = self._safe_float(last['close_num'], entry_price)
                    exit_reason = 'END_OF_DATA'
                else:
                    continue

            if side == 'BUY':
                pnl = (exit_price - entry_price) * self.config.qty
            else:
                pnl = (entry_price - exit_price) * self.config.qty

            trades.append({
                'trade_date': self.trade_date,
                'symbol': symbol,
                'side': side,
                'qty': self.config.qty,
                'signal_time': sig_time,
                'entry_time': entry_time,
                'exit_time': exit_time,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'highest_since_entry': highest,
                'lowest_since_entry': lowest,
                'exit_reason': exit_reason,
                'gross_pnl': pnl,
                'ai_confidence': sig.get('ai_confidence'),
                'ai_reason': sig.get('ai_reason'),
            })

        return pd.DataFrame(trades)

    def summary(self) -> dict:
        df = self.run()
        if df.empty:
            return {
                'trade_date': self.trade_date,
                'symbol': self.symbol,
                'trades': 0,
                'gross_pnl': 0.0,
                'win_rate': 0.0,
            }

        wins = int((df['gross_pnl'] > 0).sum())
        n = int(len(df))
        return {
            'trade_date': self.trade_date,
            'symbol': self.symbol,
            'trades': n,
            'wins': wins,
            'losses': n - wins,
            'gross_pnl': float(df['gross_pnl'].sum()),
            'avg_pnl': float(df['gross_pnl'].mean()),
            'win_rate': float(wins / n) if n else 0.0,
        }
