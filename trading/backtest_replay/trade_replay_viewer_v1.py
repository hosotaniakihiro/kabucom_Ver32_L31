# ============================================================
# File   : trading/backtest_replay/trade_replay_viewer_v1.py
# Version: Ver01-TRADE-REPLAY-VIEWER
# ------------------------------------------------------------
# 指定日・指定銘柄の AI / ORDER / EXIT / 5秒足 / spread を
# 時系列で結合し、「なぜ負けたか」を確認するためのビューを作る。
# 実注文は出さない。読み取り専用。
# ============================================================

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .engine import ReplayEngine
from .loader import ReplayLoader

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


@dataclass
class ReplayViewConfig:
    before_minutes: int = 3
    after_minutes: int = 10
    max_rows: int = 5000


class TradeReplayViewer:
    def __init__(self, trade_date: str, symbol: str, config: Optional[ReplayViewConfig] = None):
        self.trade_date = trade_date
        self.symbol = str(symbol)
        self.config = config or ReplayViewConfig()
        self.loader = ReplayLoader(trade_date)
        self.engine = ReplayEngine(trade_date, symbol=self.symbol)

    def build_timeline(self) -> pd.DataFrame:
        df = self.engine.replay()
        if df.empty:
            return df

        df = df.copy()
        df['event_time'] = pd.to_datetime(df.get('event_time'), errors='coerce')
        df = df.dropna(subset=['event_time']).sort_values('event_time').reset_index(drop=True)

        if self.config.max_rows and len(df) > self.config.max_rows:
            df = df.tail(self.config.max_rows).copy()

        return df

    def build_trade_windows(self) -> pd.DataFrame:
        """
        order_history の ORDER / FILLED / CANCELLED 周辺だけを切り出す。
        """
        timeline = self.build_timeline()
        if timeline.empty:
            return pd.DataFrame()

        order_events = timeline[timeline.get('event_type') == 'ORDER'].copy()
        if order_events.empty:
            return timeline

        windows = []
        for _, o in order_events.iterrows():
            t = o.get('event_time')
            if pd.isna(t):
                continue
            start = t - pd.Timedelta(minutes=self.config.before_minutes)
            end = t + pd.Timedelta(minutes=self.config.after_minutes)
            w = timeline[(timeline['event_time'] >= start) & (timeline['event_time'] <= end)].copy()
            if not w.empty:
                w['window_anchor_time'] = t
                w['window_anchor_status'] = o.get('status')
                w['window_anchor_order_type'] = o.get('order_type')
                windows.append(w)

        if not windows:
            return pd.DataFrame()

        out = pd.concat(windows, ignore_index=True, sort=False)
        out = out.drop_duplicates(subset=['event_time', 'event_type', 'symbol'], keep='first')
        out = out.sort_values('event_time').reset_index(drop=True)
        return out

    def summarize(self) -> dict:
        df = self.build_timeline()
        if df.empty:
            return {
                'trade_date': self.trade_date,
                'symbol': self.symbol,
                'rows': 0,
                'message': 'no replay data',
            }

        out = {
            'trade_date': self.trade_date,
            'symbol': self.symbol,
            'rows': int(len(df)),
            'event_types': df['event_type'].value_counts().to_dict() if 'event_type' in df.columns else {},
        }

        try:
            orders = df[df['event_type'] == 'ORDER'].copy()
            out['orders'] = int(len(orders))
            if not orders.empty and 'status' in orders.columns:
                out['order_status'] = orders['status'].fillna('').value_counts().to_dict()
        except Exception:
            pass

        try:
            exits = df[df['event_type'] == 'EXIT'].copy()
            out['exits'] = int(len(exits))
            if not exits.empty and 'exit_reason' in exits.columns:
                out['exit_reasons'] = exits['exit_reason'].fillna('').value_counts().to_dict()
        except Exception:
            pass

        try:
            spread = df[df['event_type'] == 'SPREAD'].copy()
            if not spread.empty and 'spread_pct' in spread.columns:
                sp = pd.to_numeric(spread['spread_pct'], errors='coerce').fillna(0)
                out['spread_pct_avg'] = float(sp.mean())
                out['spread_pct_max'] = float(sp.max())
        except Exception:
            pass

        return out

    def export_csv(self, only_trade_windows: bool = True) -> str:
        os.makedirs(BASE_DIR, exist_ok=True)
        if only_trade_windows:
            df = self.build_trade_windows()
            suffix = 'trade_windows'
        else:
            df = self.build_timeline()
            suffix = 'timeline'

        path = os.path.join(BASE_DIR, f'trade_replay_{self.trade_date}_{self.symbol}_{suffix}.csv')
        if df.empty:
            pd.DataFrame([{
                'trade_date': self.trade_date,
                'symbol': self.symbol,
                'message': 'no replay data',
            }]).to_csv(path, index=False, encoding='utf-8-sig')
        else:
            df.to_csv(path, index=False, encoding='utf-8-sig')
        return path


def export_trade_replay(trade_date: str, symbol: str, only_trade_windows: bool = True) -> dict:
    viewer = TradeReplayViewer(trade_date=trade_date, symbol=symbol)
    csv_path = viewer.export_csv(only_trade_windows=only_trade_windows)
    summary = viewer.summarize()
    summary['csv_path'] = csv_path
    return summary
