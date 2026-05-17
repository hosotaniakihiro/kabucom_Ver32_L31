# ============================================================
# File   : trading/backtest_replay/failure_pattern_miner_v1.py
# Version: Ver01-FAILURE-PATTERN-MINER
# ------------------------------------------------------------
# 負けトレードを自動分類する。
# Trade Replay / Replay Trading / Spread / 5秒足 / Exit履歴を使い、
# どの負け方が多いかを見える化する。
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .loader import ReplayLoader
from .replay_trading_v1 import ReplayTradingEngine, ReplayTradingConfig

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


@dataclass
class FailurePatternThresholds:
    wide_spread_pct: float = 0.15
    low_volume: float = 30000.0
    low_turnover: float = 10000000.0
    high_vol_range_pct: float = 1.0
    stagnation_seconds: int = 300
    small_move_pct: float = 0.05
    large_loss_yen: float = -3000.0


class FailurePatternMiner:
    def __init__(self, trade_date: str, symbol: Optional[str] = None, thresholds: Optional[FailurePatternThresholds] = None):
        self.trade_date = trade_date
        self.symbol = symbol
        self.thresholds = thresholds or FailurePatternThresholds()
        self.loader = ReplayLoader(trade_date)

    @staticmethod
    def _num(s, default=0.0):
        try:
            return pd.to_numeric(s, errors='coerce').fillna(default)
        except Exception:
            return pd.Series(dtype=float)

    def _load_trades(self) -> pd.DataFrame:
        try:
            return ReplayTradingEngine(
                trade_date=self.trade_date,
                symbol=self.symbol,
                config=ReplayTradingConfig(),
            ).run()
        except Exception:
            return pd.DataFrame()

    def _load_spread(self) -> pd.DataFrame:
        try:
            return self.loader.load_spread_history(self.symbol)
        except Exception:
            return pd.DataFrame()

    def _load_summary(self) -> pd.DataFrame:
        try:
            return self.loader.load_summary_1m(self.symbol)
        except Exception:
            return pd.DataFrame()

    def _spread_stats(self, symbol: str, start, end) -> dict:
        sp = self._load_spread()
        if sp.empty or 'datetime' not in sp.columns:
            return {'spread_pct_avg': 0.0, 'spread_pct_max': 0.0}
        sp = sp.copy()
        sp['datetime'] = pd.to_datetime(sp['datetime'], errors='coerce')
        if 'symbol' in sp.columns:
            sp = sp[sp['symbol'].astype(str) == str(symbol)]
        sp = sp[(sp['datetime'] >= start) & (sp['datetime'] <= end)]
        if sp.empty or 'spread_pct' not in sp.columns:
            return {'spread_pct_avg': 0.0, 'spread_pct_max': 0.0}
        x = self._num(sp['spread_pct'])
        return {
            'spread_pct_avg': float(x.mean()) if len(x) else 0.0,
            'spread_pct_max': float(x.max()) if len(x) else 0.0,
        }

    def _liquidity_stats(self, symbol: str, around_time) -> dict:
        sm = self._load_summary()
        if sm.empty:
            return {'volume': 0.0, 'turnover': 0.0}
        sm = sm.copy()
        if 'datetime' in sm.columns:
            sm['datetime'] = pd.to_datetime(sm['datetime'], errors='coerce')
        if 'symbol' in sm.columns:
            sm = sm[sm['symbol'].astype(str) == str(symbol)]
        if 'datetime' in sm.columns and not pd.isna(around_time):
            sm['delta'] = (sm['datetime'] - around_time).abs()
            sm = sm.sort_values('delta')
        if sm.empty:
            return {'volume': 0.0, 'turnover': 0.0}
        r = sm.iloc[0]
        close = r.get('close_price', r.get('close', 0))
        volume = r.get('volume', 0)
        try:
            turnover = float(close or 0) * float(volume or 0)
        except Exception:
            turnover = 0.0
        return {
            'volume': float(volume or 0),
            'turnover': turnover,
        }

    def classify_trade(self, trade: dict) -> dict:
        symbol = str(trade.get('symbol') or '')
        entry_time = pd.to_datetime(trade.get('entry_time'), errors='coerce')
        exit_time = pd.to_datetime(trade.get('exit_time'), errors='coerce')
        pnl = float(trade.get('gross_pnl') or 0.0)
        exit_reason = str(trade.get('exit_reason') or '')
        entry_price = float(trade.get('entry_price') or 0.0)
        exit_price = float(trade.get('exit_price') or 0.0)
        high = float(trade.get('highest_since_entry') or entry_price)
        low = float(trade.get('lowest_since_entry') or entry_price)

        spread = self._spread_stats(symbol, entry_time, exit_time)
        liq = self._liquidity_stats(symbol, entry_time)

        hold_sec = 0.0
        if not pd.isna(entry_time) and not pd.isna(exit_time):
            hold_sec = (exit_time - entry_time).total_seconds()

        move_pct = 0.0
        if entry_price > 0 and exit_price > 0:
            move_pct = abs(exit_price - entry_price) / entry_price * 100.0

        pattern = 'OTHER_LOSS'
        reasons = []

        if spread['spread_pct_max'] >= self.thresholds.wide_spread_pct:
            pattern = 'WIDE_SPREAD_LOSS'
            reasons.append(f"spread_pct_max={spread['spread_pct_max']:.4f}")

        if liq['volume'] < self.thresholds.low_volume or liq['turnover'] < self.thresholds.low_turnover:
            if pattern == 'OTHER_LOSS':
                pattern = 'LOW_LIQUIDITY_LOSS'
            reasons.append(f"volume={liq['volume']:.0f} turnover={liq['turnover']:.0f}")

        if 'STAGNATION' in exit_reason.upper() or (hold_sec >= self.thresholds.stagnation_seconds and move_pct <= self.thresholds.small_move_pct):
            if pattern == 'OTHER_LOSS':
                pattern = 'STAGNATION_LOSS'
            reasons.append(f"hold_sec={hold_sec:.1f} move_pct={move_pct:.4f}")

        if entry_price > 0:
            range_pct = (high - low) / entry_price * 100.0
            if range_pct >= self.thresholds.high_vol_range_pct:
                if pattern == 'OTHER_LOSS':
                    pattern = 'HIGH_VOL_REVERSAL_LOSS'
                reasons.append(f"range_pct={range_pct:.4f}")

        if pnl <= self.thresholds.large_loss_yen:
            if pattern == 'OTHER_LOSS':
                pattern = 'LARGE_LOSS'
            reasons.append(f"pnl={pnl:.1f}")

        return {
            **trade,
            'failure_pattern': pattern,
            'failure_reasons': '|'.join(reasons),
            'spread_pct_avg': spread['spread_pct_avg'],
            'spread_pct_max': spread['spread_pct_max'],
            'entry_volume': liq['volume'],
            'entry_turnover': liq['turnover'],
            'holding_seconds': hold_sec,
            'move_pct': move_pct,
        }

    def mine(self) -> pd.DataFrame:
        trades = self._load_trades()
        if trades.empty:
            return pd.DataFrame()
        losses = trades[pd.to_numeric(trades.get('gross_pnl'), errors='coerce').fillna(0) < 0].copy()
        if losses.empty:
            return pd.DataFrame()
        rows = [self.classify_trade(r.to_dict()) for _, r in losses.iterrows()]
        return pd.DataFrame(rows)

    def summary(self) -> dict:
        df = self.mine()
        if df.empty:
            return {
                'trade_date': self.trade_date,
                'symbol': self.symbol,
                'losses': 0,
                'patterns': {},
            }
        return {
            'trade_date': self.trade_date,
            'symbol': self.symbol,
            'losses': int(len(df)),
            'patterns': df['failure_pattern'].value_counts().to_dict(),
            'loss_pnl_by_pattern': df.groupby('failure_pattern')['gross_pnl'].sum().to_dict(),
        }

    def export_csv(self) -> str:
        os.makedirs(BASE_DIR, exist_ok=True)
        suffix = f'_{self.symbol}' if self.symbol else ''
        path = os.path.join(BASE_DIR, f'failure_patterns_{self.trade_date}{suffix}.csv')
        df = self.mine()
        if df.empty:
            pd.DataFrame([{
                'trade_date': self.trade_date,
                'symbol': self.symbol,
                'message': 'no losses or no data',
            }]).to_csv(path, index=False, encoding='utf-8-sig')
        else:
            df.to_csv(path, index=False, encoding='utf-8-sig')
        return path

    def save_summary_json(self) -> dict:
        os.makedirs(BASE_DIR, exist_ok=True)
        suffix = f'_{self.symbol}' if self.symbol else ''
        path = os.path.join(BASE_DIR, f'failure_patterns_{self.trade_date}{suffix}.json')
        result = self.summary()
        result['csv_path'] = self.export_csv()
        result['json_path'] = path
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        return result


def mine_failure_patterns(trade_date: str, symbol: Optional[str] = None) -> dict:
    return FailurePatternMiner(trade_date=trade_date, symbol=symbol).save_summary_json()
