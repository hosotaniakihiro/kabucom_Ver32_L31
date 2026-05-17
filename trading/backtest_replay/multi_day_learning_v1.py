# ============================================================
# File   : trading/backtest_replay/multi_day_learning_v1.py
# Version: Ver01-MULTI-DAY-LEARNING
# ------------------------------------------------------------
# 1日だけの偶然を避けるため、複数日で Replay Trading を実行し、
# 安定して良いパラメータを選ぶ。
# 実運用設定は直接変更しない。
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import asdict
from itertools import product
from typing import Iterable, Optional

import pandas as pd

from .daily_auto_learning_v1 import LearningGrid
from .replay_trading_v1 import ReplayTradingEngine, ReplayTradingConfig

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


class MultiDayLearner:
    def __init__(self, trade_dates: Iterable[str], symbol: Optional[str] = None, grid: Optional[LearningGrid] = None):
        self.trade_dates = list(trade_dates)
        self.symbol = symbol
        self.grid = grid or LearningGrid()

    def _out_csv(self) -> str:
        os.makedirs(BASE_DIR, exist_ok=True)
        key = f'{self.trade_dates[0]}_{self.trade_dates[-1]}' if self.trade_dates else 'empty'
        suffix = f'_{self.symbol}' if self.symbol else ''
        return os.path.join(BASE_DIR, f'multi_day_learning_{key}{suffix}.csv')

    def _out_json(self) -> str:
        os.makedirs(BASE_DIR, exist_ok=True)
        key = f'{self.trade_dates[0]}_{self.trade_dates[-1]}' if self.trade_dates else 'empty'
        suffix = f'_{self.symbol}' if self.symbol else ''
        return os.path.join(BASE_DIR, f'multi_day_learning_{key}{suffix}.json')

    def run_grid(self, max_patterns: int | None = None) -> pd.DataFrame:
        rows = []
        n = 0

        for sl, tr, stg, conf, vol, turn in product(
            self.grid.stop_loss_values,
            self.grid.trail_drop_values,
            self.grid.stagnation_seconds_values,
            self.grid.ai_confidence_min_values,
            self.grid.min_volume_values,
            self.grid.min_turnover_values,
        ):
            n += 1
            if max_patterns and n > max_patterns:
                break

            cfg = ReplayTradingConfig(
                stop_loss_pct=float(sl),
                trail_drop_pct=float(tr),
                stagnation_seconds=int(stg),
                ai_confidence_min=float(conf),
                min_volume=float(vol),
                min_turnover=float(turn),
                qty=100,
            )

            daily = []
            for td in self.trade_dates:
                try:
                    s = ReplayTradingEngine(td, symbol=self.symbol, config=cfg).summary()
                    daily.append(s)
                except Exception as e:
                    daily.append({
                        'trade_date': td,
                        'trades': 0,
                        'gross_pnl': 0.0,
                        'win_rate': 0.0,
                        'error': str(e),
                    })

            df = pd.DataFrame(daily)
            if df.empty:
                continue

            df['trades'] = pd.to_numeric(df.get('trades'), errors='coerce').fillna(0)
            df['gross_pnl'] = pd.to_numeric(df.get('gross_pnl'), errors='coerce').fillna(0.0)
            df['win_rate'] = pd.to_numeric(df.get('win_rate'), errors='coerce').fillna(0.0)

            total_trades = int(df['trades'].sum())
            total_pnl = float(df['gross_pnl'].sum())
            avg_win_rate = float(df['win_rate'].mean()) if len(df) else 0.0
            profitable_days = int((df['gross_pnl'] > 0).sum())
            losing_days = int((df['gross_pnl'] < 0).sum())
            worst_day = float(df['gross_pnl'].min()) if len(df) else 0.0

            # 安定性を重視: pnlだけでなく、勝ち日数と最悪日を評価
            stability_score = (
                total_pnl
                + avg_win_rate * 1000.0
                + profitable_days * 300.0
                - losing_days * 200.0
                + worst_day * 0.30
                + min(total_trades, 100) * 5.0
            )

            rows.append({
                **asdict(cfg),
                'symbol': self.symbol,
                'days': len(self.trade_dates),
                'trade_dates': ','.join(self.trade_dates),
                'total_trades': total_trades,
                'total_pnl': total_pnl,
                'avg_win_rate': avg_win_rate,
                'profitable_days': profitable_days,
                'losing_days': losing_days,
                'worst_day_pnl': worst_day,
                'stability_score': stability_score,
            })

        out = pd.DataFrame(rows)
        if not out.empty:
            out = out.sort_values('stability_score', ascending=False).reset_index(drop=True)
        return out

    def save_results(self, df: pd.DataFrame) -> dict:
        os.makedirs(BASE_DIR, exist_ok=True)
        csv_path = self._out_csv()
        json_path = self._out_json()

        if df.empty:
            result = {
                'ok': False,
                'reason': 'no results',
                'trade_dates': self.trade_dates,
                'symbol': self.symbol,
            }
        else:
            df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            best = df.iloc[0].to_dict()
            result = {
                'ok': True,
                'trade_dates': self.trade_dates,
                'symbol': self.symbol,
                'csv_path': csv_path,
                'best': best,
                'note': '複数日評価の推奨値です。実運用設定は直接変更していません。',
            }

        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)

        result['json_path'] = json_path
        return result

    def run_and_save(self, max_patterns: int | None = None) -> dict:
        df = self.run_grid(max_patterns=max_patterns)
        return self.save_results(df)


def run_multi_day_learning(trade_dates: Iterable[str], symbol: Optional[str] = None, max_patterns: int | None = None) -> dict:
    return MultiDayLearner(trade_dates=trade_dates, symbol=symbol).run_and_save(max_patterns=max_patterns)
