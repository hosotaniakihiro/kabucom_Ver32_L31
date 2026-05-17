# ============================================================
# File   : trading/backtest_replay/engine.py
# Version: Ver01-REPLAY-ENGINE
# ------------------------------------------------------------
# 指定日の市場状態を時系列で再生する基盤。
# まずは DataFrame を統合し、時系列イベント列を作る。
# 将来的には AI/ENTRY/EXIT を再実行可能にする。
# ============================================================

from __future__ import annotations

import pandas as pd

from .loader import ReplayLoader


class ReplayEngine:
    def __init__(self, trade_date: str, symbol: str | None = None):
        self.trade_date = trade_date
        self.symbol = symbol
        self.loader = ReplayLoader(trade_date)

    def build_event_timeline(self) -> pd.DataFrame:
        frames = []

        try:
            df = self.loader.load_summary_1m(self.symbol).copy()
            if not df.empty:
                df['event_type'] = 'SUMMARY_1M'
                df['event_time'] = df['datetime']
                frames.append(df)
        except Exception:
            pass

        try:
            df = self.loader.load_ranking(self.symbol).copy()
            if not df.empty:
                df['event_type'] = 'RANKING'
                df['event_time'] = df['datetime']
                frames.append(df)
        except Exception:
            pass

        try:
            df = self.loader.load_candidate_history(self.symbol).copy()
            if not df.empty:
                df['event_type'] = 'CANDIDATE'
                df['event_time'] = df['datetime']
                frames.append(df)
        except Exception:
            pass

        try:
            df = self.loader.load_order_history(self.symbol).copy()
            if not df.empty:
                df['event_type'] = 'ORDER'
                df['event_time'] = df['datetime']
                frames.append(df)
        except Exception:
            pass

        try:
            df = self.loader.load_exit_history(self.symbol).copy()
            if not df.empty:
                df['event_type'] = 'EXIT'
                df['event_time'] = df['datetime']
                frames.append(df)
        except Exception:
            pass

        try:
            df = self.loader.load_five_sec_bars(self.symbol).copy()
            if not df.empty:
                df['event_type'] = 'BAR_5S'
                df['event_time'] = df['bucket_time']
                frames.append(df)
        except Exception:
            pass

        try:
            df = self.loader.load_spread_history(self.symbol).copy()
            if not df.empty:
                df['event_type'] = 'SPREAD'
                df['event_time'] = df['datetime']
                frames.append(df)
        except Exception:
            pass

        if not frames:
            return pd.DataFrame()

        merged = pd.concat(frames, ignore_index=True, sort=False)
        merged['event_time'] = pd.to_datetime(merged['event_time'], errors='coerce')
        merged = merged.sort_values('event_time').reset_index(drop=True)

        return merged

    def replay(self, limit: int | None = None) -> pd.DataFrame:
        df = self.build_event_timeline()
        if limit and limit > 0:
            return df.head(limit)
        return df
