# ============================================================
# File   : trading/backtest_replay/replay_parameter_sweep_v1.py
# Version: Ver01-REPLAY-PARAMETER-SWEEP
# ------------------------------------------------------------
# Replay Backtest Engine V2 を使い、閾値候補を一括比較する。
#
# 比較対象:
#   - min_ai_confidence
#   - min_quality_score
#   - max_spread_pct
#   - momentum threshold
#
# 実注文は出さない。保存済みaudit/executionを読むだけ。
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import product
from typing import Iterable, Optional

import pandas as pd

from .replay_backtest_engine_v2 import ReplayBacktestConfig, ReplayBacktestEngineV2

OUT_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\backtest_replay'


@dataclass
class SweepGrid:
    ai_confidence_values: tuple[float, ...] = (0.55, 0.60, 0.65, 0.70, 0.75)
    quality_score_values: tuple[float, ...] = (60.0, 70.0, 80.0)
    max_spread_pct_values: tuple[float, ...] = (0.10, 0.15, 0.20, 0.25)
    momentum_buy_values: tuple[float, ...] = (0.00, 0.03, 0.05, 0.08)


class ReplayParameterSweep:
    def __init__(
        self,
        trade_dates: Iterable[str],
        symbols: Optional[Iterable[str]] = None,
        grid: Optional[SweepGrid] = None,
    ):
        self.trade_dates = list(trade_dates)
        self.symbols = list(symbols) if symbols else None
        self.grid = grid or SweepGrid()

    def run(self) -> pd.DataFrame:
        rows = []
        g = self.grid

        for ai_conf, quality, spread, mom_buy in product(
            g.ai_confidence_values,
            g.quality_score_values,
            g.max_spread_pct_values,
            g.momentum_buy_values,
        ):
            cfg = ReplayBacktestConfig(
                min_ai_confidence=ai_conf,
                min_quality_score=quality,
                max_spread_pct=spread,
                min_momentum_pct_buy=mom_buy,
                min_momentum_pct_sell=-mom_buy,
            )
            try:
                engine = ReplayBacktestEngineV2(
                    trade_dates=self.trade_dates,
                    symbols=self.symbols,
                    config=cfg,
                )
                summary = engine.summarize()
                row = {
                    'min_ai_confidence': ai_conf,
                    'min_quality_score': quality,
                    'max_spread_pct': spread,
                    'momentum_abs': mom_buy,
                    **summary,
                }
                rows.append(row)
            except Exception as e:
                rows.append({
                    'min_ai_confidence': ai_conf,
                    'min_quality_score': quality,
                    'max_spread_pct': spread,
                    'momentum_abs': mom_buy,
                    'error': str(e),
                })

        df = pd.DataFrame(rows)
        if df.empty:
            return df

        for c in ('net_pnl', 'gross_pnl', 'win_rate', 'avg_pnl', 'trades', 'entry_ok'):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0)

        # 取引数が少なすぎる設定を過大評価しないため、簡易安定スコアを付与する。
        trades = df['trades'].clip(lower=1) if 'trades' in df.columns else 1
        df['stability_score'] = (
            df.get('net_pnl', 0)
            + df.get('win_rate', 0) * 1000
            + df.get('avg_pnl', 0) * 0.1
            - (1 / trades) * 500
        )
        df = df.sort_values(['stability_score', 'net_pnl', 'win_rate'], ascending=[False, False, False]).reset_index(drop=True)
        return df

    def export(self) -> dict:
        os.makedirs(OUT_DIR, exist_ok=True)
        key = f'{self.trade_dates[0]}_{self.trade_dates[-1]}' if self.trade_dates else datetime.now().strftime('%Y%m%d')
        prefix = os.path.join(OUT_DIR, f'replay_parameter_sweep_{key}')
        df = self.run()
        csv_path = f'{prefix}.csv'
        json_path = f'{prefix}_best.json'
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')

        best = df.iloc[0].to_dict() if not df.empty else {}
        result = {
            'trade_dates': self.trade_dates,
            'symbols': self.symbols,
            'grid': asdict(self.grid),
            'rows': int(len(df)),
            'best': best,
            'csv_path': csv_path,
            'json_path': json_path,
        }
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        return result


def run_replay_parameter_sweep(
    trade_dates: Iterable[str],
    symbols: Optional[Iterable[str]] = None,
    grid: Optional[SweepGrid] = None,
) -> dict:
    return ReplayParameterSweep(trade_dates=trade_dates, symbols=symbols, grid=grid).export()
