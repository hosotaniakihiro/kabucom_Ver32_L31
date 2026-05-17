# ============================================================
# File   : trading/backtest_replay/walk_forward_test_v1.py
# Version: Ver01-WALK-FORWARD-TEST
# ------------------------------------------------------------
# 過去日で最適化し、未来日で検証する Walk Forward Test。
#
# 目的:
#   - Parameter Sweep の過剰最適化を防ぐ
#   - train期間で良かった設定が test期間でも効くか確認する
#   - 実運用に採用してよい閾値候補を選別する
#
# 実注文は出さない。保存済み audit / execution を読むだけ。
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable, Optional

import pandas as pd

from .replay_backtest_engine_v2 import ReplayBacktestConfig, ReplayBacktestEngineV2
from .replay_parameter_sweep_v1 import ReplayParameterSweep, SweepGrid

OUT_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\backtest_replay'


@dataclass
class WalkForwardConfig:
    top_n_train_configs: int = 10
    min_train_trades: int = 1
    min_test_trades: int = 1
    require_test_positive: bool = False


def _to_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default


def _to_int(v, default: int = 0) -> int:
    try:
        if v is None or v == '':
            return default
        return int(float(v))
    except Exception:
        return default


class WalkForwardTest:
    def __init__(
        self,
        train_dates: Iterable[str],
        test_dates: Iterable[str],
        symbols: Optional[Iterable[str]] = None,
        grid: Optional[SweepGrid] = None,
        config: Optional[WalkForwardConfig] = None,
    ):
        self.train_dates = list(train_dates)
        self.test_dates = list(test_dates)
        self.symbols = list(symbols) if symbols else None
        self.grid = grid or SweepGrid()
        self.config = config or WalkForwardConfig()

    def _row_to_replay_config(self, row: dict) -> ReplayBacktestConfig:
        mom_abs = _to_float(row.get('momentum_abs'), 0.03)
        return ReplayBacktestConfig(
            min_ai_confidence=_to_float(row.get('min_ai_confidence'), 0.55),
            min_quality_score=_to_float(row.get('min_quality_score'), 70.0),
            max_spread_pct=_to_float(row.get('max_spread_pct'), 0.20),
            min_momentum_pct_buy=mom_abs,
            min_momentum_pct_sell=-mom_abs,
        )

    def run_train_sweep(self) -> pd.DataFrame:
        sweep = ReplayParameterSweep(
            trade_dates=self.train_dates,
            symbols=self.symbols,
            grid=self.grid,
        )
        df = sweep.run()
        if df.empty:
            return df

        if 'trades' in df.columns:
            df = df[pd.to_numeric(df['trades'], errors='coerce').fillna(0) >= self.config.min_train_trades]

        return df.head(self.config.top_n_train_configs).reset_index(drop=True)

    def test_configs(self, train_top: pd.DataFrame) -> pd.DataFrame:
        rows = []
        if train_top.empty:
            return pd.DataFrame()

        for rank, r in train_top.reset_index(drop=True).iterrows():
            rd = r.to_dict()
            cfg = self._row_to_replay_config(rd)
            engine = ReplayBacktestEngineV2(
                trade_dates=self.test_dates,
                symbols=self.symbols,
                config=cfg,
            )
            test_summary = engine.summarize()

            row = {
                'train_rank': int(rank + 1),
                'min_ai_confidence': cfg.min_ai_confidence,
                'min_quality_score': cfg.min_quality_score,
                'max_spread_pct': cfg.max_spread_pct,
                'momentum_abs': cfg.min_momentum_pct_buy,
                'train_stability_score': _to_float(rd.get('stability_score'), 0.0),
                'train_net_pnl': _to_float(rd.get('net_pnl'), 0.0),
                'train_win_rate': _to_float(rd.get('win_rate'), 0.0),
                'train_trades': _to_int(rd.get('trades'), 0),
                'test_net_pnl': _to_float(test_summary.get('net_pnl'), 0.0),
                'test_gross_pnl': _to_float(test_summary.get('gross_pnl'), 0.0),
                'test_win_rate': _to_float(test_summary.get('win_rate'), 0.0),
                'test_avg_pnl': _to_float(test_summary.get('avg_pnl'), 0.0),
                'test_trades': _to_int(test_summary.get('trades'), 0),
                'test_entry_ok': _to_int(test_summary.get('entry_ok'), 0),
            }
            rows.append(row)

        out = pd.DataFrame(rows)
        if out.empty:
            return out

        out['generalization_score'] = (
            out['test_net_pnl']
            + out['test_win_rate'] * 1000
            + out['test_avg_pnl'] * 0.1
            - (1 / out['test_trades'].clip(lower=1)) * 500
        )

        out['overfit_gap'] = out['train_net_pnl'] - out['test_net_pnl']
        out['test_ok'] = out['test_trades'] >= self.config.min_test_trades
        if self.config.require_test_positive:
            out['test_ok'] = out['test_ok'] & (out['test_net_pnl'] > 0)

        out = out.sort_values(
            ['test_ok', 'generalization_score', 'test_net_pnl', 'test_win_rate'],
            ascending=[False, False, False, False],
        ).reset_index(drop=True)
        return out

    def run(self) -> dict:
        train_top = self.run_train_sweep()
        test_df = self.test_configs(train_top)

        best = test_df.iloc[0].to_dict() if not test_df.empty else {}
        result = {
            'train_dates': self.train_dates,
            'test_dates': self.test_dates,
            'symbols': self.symbols,
            'grid': asdict(self.grid),
            'config': asdict(self.config),
            'train_top_rows': int(len(train_top)),
            'test_rows': int(len(test_df)),
            'best': best,
        }
        return result

    def export(self) -> dict:
        os.makedirs(OUT_DIR, exist_ok=True)
        train_key = f'{self.train_dates[0]}_{self.train_dates[-1]}' if self.train_dates else datetime.now().strftime('%Y%m%d')
        test_key = f'{self.test_dates[0]}_{self.test_dates[-1]}' if self.test_dates else datetime.now().strftime('%Y%m%d')
        prefix = os.path.join(OUT_DIR, f'walk_forward_{train_key}_to_{test_key}')

        train_top = self.run_train_sweep()
        test_df = self.test_configs(train_top)
        result = self.run()

        paths = {
            'train_top_csv': f'{prefix}_train_top.csv',
            'test_result_csv': f'{prefix}_test_result.csv',
            'summary_json': f'{prefix}_summary.json',
        }

        train_top.to_csv(paths['train_top_csv'], index=False, encoding='utf-8-sig')
        test_df.to_csv(paths['test_result_csv'], index=False, encoding='utf-8-sig')
        result['paths'] = paths

        with open(paths['summary_json'], 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)

        return result


def run_walk_forward_test(
    train_dates: Iterable[str],
    test_dates: Iterable[str],
    symbols: Optional[Iterable[str]] = None,
    grid: Optional[SweepGrid] = None,
    config: Optional[WalkForwardConfig] = None,
) -> dict:
    return WalkForwardTest(
        train_dates=train_dates,
        test_dates=test_dates,
        symbols=symbols,
        grid=grid,
        config=config,
    ).export()
