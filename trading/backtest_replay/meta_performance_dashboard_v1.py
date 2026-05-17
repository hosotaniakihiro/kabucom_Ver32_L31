# ============================================================
# File   : trading/backtest_replay/meta_performance_dashboard_v1.py
# Version: Ver01-META-PERFORMANCE-DASHBOARD
# ------------------------------------------------------------
# Replay / Audit / Failure Mining / Regime / Strategy Rotation の結果を集計し、
# 「どの戦略・時間帯・相場・負け方が強い/弱いか」を見える化する。
# 実注文は出さない。読み取り専用。
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable, Optional

import pandas as pd

from .replay_trading_v1 import ReplayTradingEngine, ReplayTradingConfig
from .failure_pattern_miner_v1 import FailurePatternMiner
from .market_regime_detector_v1 import MarketRegimeDetector

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


@dataclass
class DashboardConfig:
    qty: int = 100
    stop_loss_pct: float = 0.30
    trail_drop_pct: float = 0.30
    stagnation_seconds: int = 300
    ai_confidence_min: float = 0.55


class MetaPerformanceDashboard:
    def __init__(self, trade_dates: Iterable[str], symbol: Optional[str] = None, config: Optional[DashboardConfig] = None):
        self.trade_dates = list(trade_dates)
        self.symbol = symbol
        self.config = config or DashboardConfig()

    def _replay_config(self) -> ReplayTradingConfig:
        return ReplayTradingConfig(
            qty=self.config.qty,
            stop_loss_pct=self.config.stop_loss_pct,
            trail_drop_pct=self.config.trail_drop_pct,
            stagnation_seconds=self.config.stagnation_seconds,
            ai_confidence_min=self.config.ai_confidence_min,
        )

    def load_trades(self) -> pd.DataFrame:
        frames = []
        for td in self.trade_dates:
            try:
                df = ReplayTradingEngine(td, symbol=self.symbol, config=self._replay_config()).run()
                if df is not None and not df.empty:
                    df = df.copy()
                    df['trade_date'] = td
                    frames.append(df)
            except Exception as e:
                frames.append(pd.DataFrame([{
                    'trade_date': td,
                    'symbol': self.symbol,
                    'error': str(e),
                }]))

        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True, sort=False)
        if 'entry_time' in out.columns:
            out['entry_time'] = pd.to_datetime(out['entry_time'], errors='coerce')
            out['entry_hour'] = out['entry_time'].dt.hour
            out['entry_minute'] = out['entry_time'].dt.minute
            out['time_bucket'] = out['entry_time'].dt.strftime('%H:%M')
            out['session_bucket'] = out['entry_time'].dt.hour.apply(self._session_bucket)
        return out

    @staticmethod
    def _session_bucket(hour: float) -> str:
        try:
            h = int(hour)
        except Exception:
            return 'UNKNOWN'
        if h < 9:
            return 'PRE_MARKET'
        if h == 9:
            return 'OPEN_9'
        if h == 10:
            return 'MORNING_10'
        if h == 11:
            return 'LATE_MORNING_11'
        if h == 12:
            return 'LUNCH_12'
        if h == 13:
            return 'AFTERNOON_13'
        if h == 14:
            return 'AFTERNOON_14'
        if h >= 15:
            return 'CLOSE_15'
        return 'UNKNOWN'

    @staticmethod
    def _summarize_group(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()
        x = df.copy()
        x['gross_pnl'] = pd.to_numeric(x.get('gross_pnl'), errors='coerce').fillna(0.0)
        x['win'] = x['gross_pnl'] > 0
        grouped = x.groupby(group_cols, dropna=False).agg(
            trades=('gross_pnl', 'count'),
            gross_pnl=('gross_pnl', 'sum'),
            avg_pnl=('gross_pnl', 'mean'),
            wins=('win', 'sum'),
        ).reset_index()
        grouped['win_rate'] = grouped['wins'] / grouped['trades'].clip(lower=1)
        grouped = grouped.sort_values(['gross_pnl', 'win_rate'], ascending=[False, False]).reset_index(drop=True)
        return grouped

    def summarize_overall(self) -> dict:
        df = self.load_trades()
        if df.empty or 'gross_pnl' not in df.columns:
            return {
                'trade_dates': self.trade_dates,
                'symbol': self.symbol,
                'trades': 0,
                'gross_pnl': 0.0,
                'win_rate': 0.0,
            }
        pnl = pd.to_numeric(df['gross_pnl'], errors='coerce').fillna(0.0)
        wins = int((pnl > 0).sum())
        n = int(len(df))
        return {
            'trade_dates': self.trade_dates,
            'symbol': self.symbol,
            'trades': n,
            'wins': wins,
            'losses': n - wins,
            'gross_pnl': float(pnl.sum()),
            'avg_pnl': float(pnl.mean()) if n else 0.0,
            'win_rate': float(wins / n) if n else 0.0,
            'best_trade': float(pnl.max()) if n else 0.0,
            'worst_trade': float(pnl.min()) if n else 0.0,
        }

    def summarize_by_side(self) -> pd.DataFrame:
        df = self.load_trades()
        if df.empty or 'side' not in df.columns:
            return pd.DataFrame()
        return self._summarize_group(df, ['side'])

    def summarize_by_exit_reason(self) -> pd.DataFrame:
        df = self.load_trades()
        if df.empty or 'exit_reason' not in df.columns:
            return pd.DataFrame()
        return self._summarize_group(df, ['exit_reason'])

    def summarize_by_session(self) -> pd.DataFrame:
        df = self.load_trades()
        if df.empty or 'session_bucket' not in df.columns:
            return pd.DataFrame()
        return self._summarize_group(df, ['session_bucket'])

    def summarize_by_regime(self) -> pd.DataFrame:
        rows = []
        for td in self.trade_dates:
            try:
                regime = MarketRegimeDetector(td, symbol=self.symbol).detect()
                summary = ReplayTradingEngine(td, symbol=self.symbol, config=self._replay_config()).summary()
                rows.append({
                    'trade_date': td,
                    'symbol': self.symbol,
                    'regime': regime.get('regime'),
                    'regime_reason': regime.get('reason'),
                    **summary,
                })
            except Exception as e:
                rows.append({
                    'trade_date': td,
                    'symbol': self.symbol,
                    'regime': 'ERROR',
                    'error': str(e),
                })
        df = pd.DataFrame(rows)
        if df.empty or 'regime' not in df.columns:
            return df
        df['gross_pnl'] = pd.to_numeric(df.get('gross_pnl'), errors='coerce').fillna(0.0)
        df['trades'] = pd.to_numeric(df.get('trades'), errors='coerce').fillna(0)
        df['win_rate'] = pd.to_numeric(df.get('win_rate'), errors='coerce').fillna(0.0)
        return df.sort_values('trade_date').reset_index(drop=True)

    def summarize_failure_patterns(self) -> pd.DataFrame:
        rows = []
        for td in self.trade_dates:
            try:
                result = FailurePatternMiner(td, symbol=self.symbol).summary()
                patterns = result.get('patterns') or {}
                loss_pnl = result.get('loss_pnl_by_pattern') or {}
                for p, count in patterns.items():
                    rows.append({
                        'trade_date': td,
                        'symbol': self.symbol,
                        'failure_pattern': p,
                        'loss_count': count,
                        'loss_pnl': loss_pnl.get(p, 0.0),
                    })
            except Exception as e:
                rows.append({
                    'trade_date': td,
                    'symbol': self.symbol,
                    'failure_pattern': 'ERROR',
                    'error': str(e),
                })
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        if 'loss_count' in df.columns:
            df['loss_count'] = pd.to_numeric(df['loss_count'], errors='coerce').fillna(0)
        if 'loss_pnl' in df.columns:
            df['loss_pnl'] = pd.to_numeric(df['loss_pnl'], errors='coerce').fillna(0.0)
        return df.sort_values(['loss_count', 'loss_pnl'], ascending=[False, True]).reset_index(drop=True)

    def export(self) -> dict:
        os.makedirs(BASE_DIR, exist_ok=True)
        key = f'{self.trade_dates[0]}_{self.trade_dates[-1]}' if self.trade_dates else 'empty'
        suffix = f'_{self.symbol}' if self.symbol else ''
        prefix = os.path.join(BASE_DIR, f'meta_performance_{key}{suffix}')

        outputs = {}
        outputs['trades_csv'] = f'{prefix}_trades.csv'
        outputs['side_csv'] = f'{prefix}_by_side.csv'
        outputs['exit_csv'] = f'{prefix}_by_exit_reason.csv'
        outputs['session_csv'] = f'{prefix}_by_session.csv'
        outputs['regime_csv'] = f'{prefix}_by_regime.csv'
        outputs['failure_csv'] = f'{prefix}_failure_patterns.csv'
        outputs['summary_json'] = f'{prefix}_summary.json'

        self.load_trades().to_csv(outputs['trades_csv'], index=False, encoding='utf-8-sig')
        self.summarize_by_side().to_csv(outputs['side_csv'], index=False, encoding='utf-8-sig')
        self.summarize_by_exit_reason().to_csv(outputs['exit_csv'], index=False, encoding='utf-8-sig')
        self.summarize_by_session().to_csv(outputs['session_csv'], index=False, encoding='utf-8-sig')
        self.summarize_by_regime().to_csv(outputs['regime_csv'], index=False, encoding='utf-8-sig')
        self.summarize_failure_patterns().to_csv(outputs['failure_csv'], index=False, encoding='utf-8-sig')

        summary = self.summarize_overall()
        summary['outputs'] = outputs
        with open(outputs['summary_json'], 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

        return summary


def export_meta_performance_dashboard(trade_dates: Iterable[str], symbol: Optional[str] = None) -> dict:
    return MetaPerformanceDashboard(trade_dates=trade_dates, symbol=symbol).export()
