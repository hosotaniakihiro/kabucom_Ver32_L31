# ============================================================
# File   : trading/backtest_replay/parameter_optimizer_v1.py
# Version: Ver01-PARAMETER-OPTIMIZER
# ------------------------------------------------------------
# Replay / audit DB を使って、損切り・トレーリング・出来高・スプレッド
# 条件を複数パターンで比較するための簡易オプティマイザ。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Iterable, Optional

import pandas as pd

from .loader import ReplayLoader
from .pnl_simulator_v1 import ReplayPnLSimulator


@dataclass(frozen=True)
class ParameterSet:
    stop_loss_pct: float = 0.30
    trail_drop_pct: float = 0.30
    min_volume: float = 30000.0
    min_turnover: float = 10000000.0
    max_spread_pct: float = 0.20


class ReplayParameterOptimizer:
    def __init__(self, trade_date: str, symbol: Optional[str] = None):
        self.trade_date = trade_date
        self.symbol = symbol
        self.loader = ReplayLoader(trade_date)

    def _load_base_trades(self) -> pd.DataFrame:
        return ReplayPnLSimulator(self.trade_date, self.symbol).simulate()

    def _load_summary(self) -> pd.DataFrame:
        try:
            return self.loader.load_summary_1m(self.symbol)
        except Exception:
            return pd.DataFrame()

    def _load_spread(self) -> pd.DataFrame:
        try:
            return self.loader.load_spread_history(self.symbol)
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def _safe_numeric(df: pd.DataFrame, col: str) -> pd.Series:
        if col not in df.columns:
            return pd.Series([0.0] * len(df), index=df.index)
        return pd.to_numeric(df[col], errors='coerce').fillna(0.0)

    def evaluate(self, params: ParameterSet) -> dict:
        trades = self._load_base_trades()
        if trades.empty:
            return {
                'trade_date': self.trade_date,
                'symbol': self.symbol,
                **params.__dict__,
                'trades': 0,
                'gross_pnl': 0.0,
                'win_rate': 0.0,
                'note': 'no trades',
            }

        df = trades.copy()

        # 実際のexitを再計算する完全シミュレーションは Phase2 以降。
        # v1では既存tradeに対して、パラメータごとの概算ペナルティ/除外を評価する。
        df['gross_pnl'] = pd.to_numeric(df.get('gross_pnl'), errors='coerce').fillna(0.0)

        # spread 条件は spread_snapshots がある場合のみ適用。
        spread = self._load_spread()
        spread_penalty = 0.0
        if not spread.empty and 'spread_pct' in spread.columns:
            sp = pd.to_numeric(spread['spread_pct'], errors='coerce').fillna(0.0)
            too_wide_ratio = float((sp > params.max_spread_pct).mean()) if len(sp) else 0.0
            spread_penalty = abs(float(df['gross_pnl'].sum())) * too_wide_ratio * 0.05

        # 出来高/売買代金条件は summary がある場合のみ参考評価。
        summary = self._load_summary()
        liquidity_penalty = 0.0
        if not summary.empty:
            vol = self._safe_numeric(summary, 'volume')
            close = self._safe_numeric(summary, 'close_price')
            if close.sum() <= 0:
                close = self._safe_numeric(summary, 'close')
            turnover = vol * close
            low_liq_ratio = float(((vol < params.min_volume) | (turnover < params.min_turnover)).mean()) if len(summary) else 0.0
            liquidity_penalty = abs(float(df['gross_pnl'].sum())) * low_liq_ratio * 0.10

        gross = float(df['gross_pnl'].sum())
        adjusted = gross - spread_penalty - liquidity_penalty
        wins = int((df['gross_pnl'] > 0).sum())
        n = int(len(df))

        return {
            'trade_date': self.trade_date,
            'symbol': self.symbol,
            **params.__dict__,
            'trades': n,
            'wins': wins,
            'losses': n - wins,
            'gross_pnl': gross,
            'spread_penalty': spread_penalty,
            'liquidity_penalty': liquidity_penalty,
            'adjusted_pnl': adjusted,
            'win_rate': float(wins / n) if n else 0.0,
        }

    def grid_search(
        self,
        stop_loss_values: Iterable[float] = (0.20, 0.25, 0.30, 0.35),
        trail_drop_values: Iterable[float] = (0.20, 0.25, 0.30, 0.35),
        min_volume_values: Iterable[float] = (30000, 50000, 100000),
        min_turnover_values: Iterable[float] = (10000000, 20000000, 30000000),
        max_spread_pct_values: Iterable[float] = (0.10, 0.15, 0.20, 0.30),
    ) -> pd.DataFrame:
        rows = []
        for sl, tr, vol, turn, sp in product(
            stop_loss_values,
            trail_drop_values,
            min_volume_values,
            min_turnover_values,
            max_spread_pct_values,
        ):
            params = ParameterSet(
                stop_loss_pct=float(sl),
                trail_drop_pct=float(tr),
                min_volume=float(vol),
                min_turnover=float(turn),
                max_spread_pct=float(sp),
            )
            rows.append(self.evaluate(params))

        df = pd.DataFrame(rows)
        if not df.empty and 'adjusted_pnl' in df.columns:
            df = df.sort_values('adjusted_pnl', ascending=False).reset_index(drop=True)
        return df

    def best(self) -> dict:
        df = self.grid_search()
        if df.empty:
            return {}
        return df.iloc[0].to_dict()
