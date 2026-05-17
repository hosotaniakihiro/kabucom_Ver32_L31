# ============================================================
# File   : trading/backtest_replay/ai_replay_v1.py
# Version: Ver01-AI-REPLAY
# ------------------------------------------------------------
# 過去の summary / candidate データに対して、現在のAI判定を再実行する。
# 市場時間外にAI・score・filter変更の影響を確認するための土台。
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .loader import ReplayLoader


@dataclass
class AIReplayConfig:
    source: str = 'SUMMARY'
    interval: int = 1
    side: Optional[str] = None
    max_rows: int = 1000
    min_close_price: float = 200.0
    min_volume: float = 30000.0
    min_turnover: float = 10000000.0


class AIReplayEngine:
    def __init__(self, trade_date: str, symbol: Optional[str] = None, config: Optional[AIReplayConfig] = None):
        self.trade_date = trade_date
        self.symbol = symbol
        self.config = config or AIReplayConfig()
        self.loader = ReplayLoader(trade_date)

    @staticmethod
    def _safe_float(v, default: float = 0.0) -> float:
        try:
            if v is None or v == '':
                return default
            return float(v)
        except Exception:
            return default

    def _prepare_rows_from_summary(self) -> pd.DataFrame:
        df = self.loader.load_summary_1m(self.symbol)
        if df.empty:
            return df

        # 標準列へ寄せる
        if 'close_price' not in df.columns and 'close' in df.columns:
            df['close_price'] = df['close']
        if 'source' not in df.columns:
            df['source'] = self.config.source
        if 'interval' not in df.columns:
            df['interval'] = self.config.interval

        close = pd.to_numeric(df.get('close_price'), errors='coerce').fillna(0)
        volume = pd.to_numeric(df.get('volume'), errors='coerce').fillna(0)
        turnover = close * volume

        df = df[(close >= self.config.min_close_price) & (volume >= self.config.min_volume) & (turnover >= self.config.min_turnover)].copy()

        if self.config.max_rows and len(df) > self.config.max_rows:
            df = df.tail(self.config.max_rows).copy()

        return df.reset_index(drop=True)

    def replay(self) -> pd.DataFrame:
        df = self._prepare_rows_from_summary()
        if df.empty:
            return pd.DataFrame()

        try:
            from AI.entry_row_builder import build_entry_row
            from AI.entry_gate import ai_final_entry_check
        except Exception as e:
            return pd.DataFrame([{
                'trade_date': self.trade_date,
                'symbol': self.symbol,
                'error': f'AI imports failed: {e}',
            }])

        rows = []
        for _, r in df.iterrows():
            raw = r.to_dict()
            side_candidates = []
            if self.config.side:
                side_candidates = [self.config.side.upper()]
            else:
                side_candidates = ['BUY', 'SELL']

            for side in side_candidates:
                try:
                    raw2 = dict(raw)
                    raw2['side'] = side
                    raw2['entry_decision'] = side
                    raw2['source'] = raw2.get('source') or self.config.source
                    raw2['interval'] = raw2.get('interval') or self.config.interval

                    entry_row = build_entry_row(raw2)
                    if not entry_row:
                        rows.append({
                            'datetime': raw.get('datetime'),
                            'symbol': raw.get('symbol'),
                            'side': side,
                            'ai_allow': False,
                            'reason': 'ENTRY_ROW_EMPTY',
                        })
                        continue

                    ai = ai_final_entry_check(entry_row)
                    if not isinstance(ai, dict):
                        rows.append({
                            'datetime': raw.get('datetime'),
                            'symbol': raw.get('symbol'),
                            'side': side,
                            'ai_allow': False,
                            'reason': 'AI_RESULT_INVALID',
                        })
                        continue

                    rows.append({
                        'datetime': raw.get('datetime'),
                        'symbol': raw.get('symbol'),
                        'side': side,
                        'source': raw2.get('source'),
                        'interval': raw2.get('interval'),
                        'close_price': self._safe_float(raw.get('close_price'), 0.0),
                        'volume': self._safe_float(raw.get('volume'), 0.0),
                        'score': self._safe_float(raw.get('score'), 0.0),
                        'score_buy': self._safe_float(raw.get('score_buy'), 0.0),
                        'score_sell': self._safe_float(raw.get('score_sell'), 0.0),
                        'ai_allow': bool(ai.get('allow', False)),
                        'ai_confidence': self._safe_float(ai.get('confidence'), 0.0),
                        'ai_reason': ai.get('reason'),
                        'ai_raw': str(ai)[:2000],
                    })
                except Exception as e:
                    rows.append({
                        'datetime': raw.get('datetime'),
                        'symbol': raw.get('symbol'),
                        'side': side,
                        'ai_allow': False,
                        'reason': f'EXCEPTION:{e}',
                    })

        out = pd.DataFrame(rows)
        if not out.empty:
            out = out.sort_values(['datetime', 'symbol', 'side']).reset_index(drop=True)
        return out

    def summary(self) -> dict:
        df = self.replay()
        if df.empty:
            return {
                'trade_date': self.trade_date,
                'symbol': self.symbol,
                'rows': 0,
                'ai_ok': 0,
            }
        ok = int(df.get('ai_allow', pd.Series(dtype=bool)).fillna(False).sum())
        return {
            'trade_date': self.trade_date,
            'symbol': self.symbol,
            'rows': int(len(df)),
            'ai_ok': ok,
            'ai_ng': int(len(df) - ok),
            'ai_ok_rate': float(ok / len(df)) if len(df) else 0.0,
        }
