# ============================================================
# File   : trading/backtest_replay/daily_auto_learning_v1.py
# Version: Ver01-DAILY-AUTO-LEARNING
# ------------------------------------------------------------
# 毎日 Replay Trading を実行し、翌営業日向けの推奨パラメータを保存する。
# 安全のため、実運用設定は直接変更しない。
# 出力先: audit/replay_learning_YYYYMMDD.json / csv
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from itertools import product
from typing import Iterable, Optional

import pandas as pd

from .replay_trading_v1 import ReplayTradingEngine, ReplayTradingConfig

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


@dataclass(frozen=True)
class LearningGrid:
    stop_loss_values: tuple[float, ...] = (0.15, 0.20, 0.25, 0.30, 0.35)
    trail_drop_values: tuple[float, ...] = (0.10, 0.15, 0.20, 0.25, 0.30)
    stagnation_seconds_values: tuple[int, ...] = (120, 180, 300)
    ai_confidence_min_values: tuple[float, ...] = (0.55, 0.60, 0.65, 0.70)
    min_volume_values: tuple[float, ...] = (30000, 50000, 100000)
    min_turnover_values: tuple[float, ...] = (10000000, 20000000, 30000000)


class DailyAutoLearner:
    def __init__(self, trade_date: str, symbol: Optional[str] = None, grid: Optional[LearningGrid] = None):
        self.trade_date = trade_date
        self.symbol = symbol
        self.grid = grid or LearningGrid()

    def _out_csv(self) -> str:
        os.makedirs(BASE_DIR, exist_ok=True)
        suffix = f'_{self.symbol}' if self.symbol else ''
        return os.path.join(BASE_DIR, f'replay_learning_{self.trade_date}{suffix}.csv')

    def _out_json(self) -> str:
        os.makedirs(BASE_DIR, exist_ok=True)
        suffix = f'_{self.symbol}' if self.symbol else ''
        return os.path.join(BASE_DIR, f'replay_learning_{self.trade_date}{suffix}.json')

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

            try:
                engine = ReplayTradingEngine(
                    trade_date=self.trade_date,
                    symbol=self.symbol,
                    config=cfg,
                )
                summary = engine.summary()
                row = {
                    **asdict(cfg),
                    **summary,
                }
                rows.append(row)
            except Exception as e:
                rows.append({
                    **asdict(cfg),
                    'trade_date': self.trade_date,
                    'symbol': self.symbol,
                    'trades': 0,
                    'gross_pnl': 0.0,
                    'win_rate': 0.0,
                    'error': str(e),
                })

        df = pd.DataFrame(rows)
        if not df.empty:
            # トレード数が少なすぎるものを過大評価しない
            if 'trades' in df.columns:
                df['trades'] = pd.to_numeric(df['trades'], errors='coerce').fillna(0)
            if 'gross_pnl' in df.columns:
                df['gross_pnl'] = pd.to_numeric(df['gross_pnl'], errors='coerce').fillna(0.0)
            if 'win_rate' in df.columns:
                df['win_rate'] = pd.to_numeric(df['win_rate'], errors='coerce').fillna(0.0)

            df['score'] = df['gross_pnl'] + (df['win_rate'] * 1000.0) + (df['trades'].clip(upper=20) * 10.0)
            df = df.sort_values('score', ascending=False).reset_index(drop=True)

        return df

    def save_results(self, df: pd.DataFrame) -> dict:
        os.makedirs(BASE_DIR, exist_ok=True)
        csv_path = self._out_csv()
        json_path = self._out_json()

        if df.empty:
            result = {
                'trade_date': self.trade_date,
                'symbol': self.symbol,
                'ok': False,
                'reason': 'no results',
            }
        else:
            df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            best = df.iloc[0].to_dict()
            result = {
                'trade_date': self.trade_date,
                'symbol': self.symbol,
                'ok': True,
                'csv_path': csv_path,
                'best': best,
                'note': '推奨値です。実運用設定は自動変更していません。',
            }

        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)

        result['json_path'] = json_path
        return result

    def run_and_save(self, max_patterns: int | None = None) -> dict:
        df = self.run_grid(max_patterns=max_patterns)
        return self.save_results(df)


def run_daily_auto_learning(trade_date: str, symbol: Optional[str] = None, max_patterns: int | None = None) -> dict:
    return DailyAutoLearner(trade_date=trade_date, symbol=symbol).run_and_save(max_patterns=max_patterns)
