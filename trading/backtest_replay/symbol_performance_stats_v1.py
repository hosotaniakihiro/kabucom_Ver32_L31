# ============================================================
# File   : trading/backtest_replay/symbol_performance_stats_v1.py
# Version: Ver01-SYMBOL-PERFORMANCE-STATS
# ------------------------------------------------------------
# 銘柄別の勝率・損益・取引回数を集計する。
#
# 入力:
#   - runtime_state_YYYYMMDD.db / executions_runtime
#   - replay_backtest_engine_v2 の execution pairing
#
# 出力:
#   - symbol_performance_YYYYMMDD_YYYYMMDD.csv
#   - symbol_performance_latest.db / symbol_performance_latest
#
# 目的:
#   - 勝率が低い銘柄をENTRY前に除外
#   - 高勝率・低損失の銘柄だけを優先
#   - PUSH監視銘柄選定にも利用可能にする
# ============================================================

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable, Optional

import pandas as pd

from .replay_backtest_engine_v2 import ReplayBacktestConfig, ReplayBacktestEngineV2

OUT_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\backtest_replay'
STATS_DB_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\symbol_stats'


@dataclass
class SymbolPerformanceConfig:
    min_trades_for_decision: int = 3
    min_win_rate: float = 0.55
    min_avg_pnl: float = 0.0
    max_avg_loss: float = -1500.0
    blacklist_win_rate: float = 0.40
    blacklist_min_trades: int = 3


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def _safe_num(s, default: float = 0.0):
    return pd.to_numeric(s, errors='coerce').fillna(default)


class SymbolPerformanceStatsBuilder:
    def __init__(
        self,
        trade_dates: Iterable[str],
        symbols: Optional[Iterable[str]] = None,
        replay_config: Optional[ReplayBacktestConfig] = None,
        perf_config: Optional[SymbolPerformanceConfig] = None,
    ):
        self.trade_dates = list(trade_dates)
        self.symbols = list(symbols) if symbols else None
        self.replay_config = replay_config or ReplayBacktestConfig()
        self.perf_config = perf_config or SymbolPerformanceConfig()

    def load_trades(self) -> pd.DataFrame:
        engine = ReplayBacktestEngineV2(
            trade_dates=self.trade_dates,
            symbols=self.symbols,
            config=self.replay_config,
        )
        executions = engine.load_all_executions()
        trades = engine.estimate_pnl_from_executions(executions)
        return trades

    def build(self) -> pd.DataFrame:
        trades = self.load_trades()
        if trades.empty:
            return pd.DataFrame()

        x = trades.copy()
        x['net_pnl_n'] = _safe_num(x.get('net_pnl', 0.0))
        x['gross_pnl_n'] = _safe_num(x.get('gross_pnl', 0.0))
        x['is_win'] = x['net_pnl_n'] > 0
        x['is_loss'] = x['net_pnl_n'] < 0

        g = x.groupby('symbol', dropna=False)
        stats = g.agg(
            trades=('symbol', 'size'),
            wins=('is_win', 'sum'),
            losses=('is_loss', 'sum'),
            net_pnl=('net_pnl_n', 'sum'),
            gross_pnl=('gross_pnl_n', 'sum'),
            avg_pnl=('net_pnl_n', 'mean'),
            median_pnl=('net_pnl_n', 'median'),
            max_profit=('net_pnl_n', 'max'),
            max_loss=('net_pnl_n', 'min'),
        ).reset_index()

        stats['win_rate'] = stats['wins'] / stats['trades'].clip(lower=1)
        stats['loss_rate'] = stats['losses'] / stats['trades'].clip(lower=1)

        cfg = self.perf_config
        stats['allow_entry'] = (
            (stats['trades'] >= cfg.min_trades_for_decision) &
            (stats['win_rate'] >= cfg.min_win_rate) &
            (stats['avg_pnl'] >= cfg.min_avg_pnl)
        )

        stats['blacklist'] = (
            (stats['trades'] >= cfg.blacklist_min_trades) &
            (
                (stats['win_rate'] <= cfg.blacklist_win_rate) |
                (stats['avg_pnl'] < cfg.max_avg_loss)
            )
        )

        # 未判定: 取引数不足。強制禁止ではなく、低優先に使う。
        stats['insufficient_samples'] = stats['trades'] < cfg.min_trades_for_decision

        stats['symbol_quality_score'] = (
            stats['win_rate'] * 100
            + stats['avg_pnl'] * 0.01
            + stats['net_pnl'] * 0.001
            - stats['loss_rate'] * 30
        )

        stats['trade_dates'] = ','.join(self.trade_dates)
        stats['generated_at'] = datetime.now().isoformat(timespec='seconds')
        stats = stats.sort_values(['blacklist', 'allow_entry', 'symbol_quality_score', 'net_pnl'], ascending=[True, False, False, False])
        return stats.reset_index(drop=True)

    def export(self) -> dict:
        os.makedirs(OUT_DIR, exist_ok=True)
        os.makedirs(STATS_DB_DIR, exist_ok=True)

        key = f'{self.trade_dates[0]}_{self.trade_dates[-1]}' if self.trade_dates else _today()
        csv_path = os.path.join(OUT_DIR, f'symbol_performance_{key}.csv')
        json_path = os.path.join(OUT_DIR, f'symbol_performance_{key}_summary.json')
        db_path = os.path.join(STATS_DB_DIR, 'symbol_performance_latest.db')

        stats = self.build()
        stats.to_csv(csv_path, index=False, encoding='utf-8-sig')

        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            stats.to_sql('symbol_performance_latest', conn, if_exists='replace', index=False)
            conn.execute('CREATE INDEX IF NOT EXISTS idx_symbol_performance_symbol ON symbol_performance_latest(symbol)')
            conn.commit()

        result = {
            'trade_dates': self.trade_dates,
            'symbols': self.symbols,
            'replay_config': asdict(self.replay_config),
            'performance_config': asdict(self.perf_config),
            'rows': int(len(stats)),
            'allow_entry_count': int(stats['allow_entry'].sum()) if not stats.empty and 'allow_entry' in stats.columns else 0,
            'blacklist_count': int(stats['blacklist'].sum()) if not stats.empty and 'blacklist' in stats.columns else 0,
            'csv_path': csv_path,
            'json_path': json_path,
            'db_path': db_path,
            'generated_at': datetime.now().isoformat(timespec='seconds'),
        }

        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)

        return result


def build_symbol_performance_stats(
    trade_dates: Iterable[str],
    symbols: Optional[Iterable[str]] = None,
    replay_config: Optional[ReplayBacktestConfig] = None,
    perf_config: Optional[SymbolPerformanceConfig] = None,
) -> dict:
    return SymbolPerformanceStatsBuilder(
        trade_dates=trade_dates,
        symbols=symbols,
        replay_config=replay_config,
        perf_config=perf_config,
    ).export()
