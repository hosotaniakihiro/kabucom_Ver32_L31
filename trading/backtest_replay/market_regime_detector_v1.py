# ============================================================
# File   : trading/backtest_replay/market_regime_detector_v1.py
# Version: Ver01-MARKET-REGIME-DETECTOR
# ------------------------------------------------------------
# Replay / Summary / Ranking / Spread データから相場レジームを判定する。
# 目的:
#   - 通常相場
#   - 暴落相場
#   - 高ボラ相場
#   - 閑散相場
#   - イナゴ祭り相場
# ごとに、Replay最適化パラメータを切り替える土台にする。
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd

from .loader import ReplayLoader

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


@dataclass
class RegimeThresholds:
    crash_down_ratio: float = 0.60
    high_vol_range_pct: float = 1.20
    low_liquidity_avg_volume: float = 30000.0
    inago_up_ratio: float = 0.55
    wide_spread_pct: float = 0.20


class MarketRegimeDetector:
    def __init__(self, trade_date: str, symbol: Optional[str] = None, thresholds: Optional[RegimeThresholds] = None):
        self.trade_date = trade_date
        self.symbol = symbol
        self.thresholds = thresholds or RegimeThresholds()
        self.loader = ReplayLoader(trade_date)

    @staticmethod
    def _safe_num(s: pd.Series) -> pd.Series:
        return pd.to_numeric(s, errors='coerce').fillna(0.0)

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

    def detect(self) -> dict:
        summary = self._load_summary()
        spread = self._load_spread()

        result = {
            'trade_date': self.trade_date,
            'symbol': self.symbol,
            'regime': 'UNKNOWN',
            'reason': '',
            'metrics': {},
            'thresholds': asdict(self.thresholds),
        }

        if summary.empty:
            result['reason'] = 'summary empty'
            return result

        df = summary.copy()

        close_col = 'close_price' if 'close_price' in df.columns else 'close'
        open_col = 'open_price' if 'open_price' in df.columns else 'open'
        high_col = 'high_price' if 'high_price' in df.columns else 'high'
        low_col = 'low_price' if 'low_price' in df.columns else 'low'

        close = self._safe_num(df.get(close_col, pd.Series(dtype=float)))
        open_ = self._safe_num(df.get(open_col, pd.Series(dtype=float)))
        high = self._safe_num(df.get(high_col, pd.Series(dtype=float)))
        low = self._safe_num(df.get(low_col, pd.Series(dtype=float)))
        volume = self._safe_num(df.get('volume', pd.Series(dtype=float)))

        valid = close > 0
        if valid.sum() <= 0:
            result['reason'] = 'no valid close'
            return result

        up_ratio = float((close[valid] > open_[valid]).mean()) if len(close[valid]) else 0.0
        down_ratio = float((close[valid] < open_[valid]).mean()) if len(close[valid]) else 0.0
        avg_volume = float(volume[valid].mean()) if len(volume[valid]) else 0.0

        range_pct = pd.Series([0.0] * len(df), index=df.index)
        mask = close > 0
        range_pct.loc[mask] = (high[mask] - low[mask]) / close[mask] * 100.0
        avg_range_pct = float(range_pct[mask].mean()) if mask.sum() else 0.0

        avg_spread_pct = 0.0
        wide_spread_ratio = 0.0
        if not spread.empty and 'spread_pct' in spread.columns:
            sp = self._safe_num(spread['spread_pct'])
            avg_spread_pct = float(sp.mean()) if len(sp) else 0.0
            wide_spread_ratio = float((sp > self.thresholds.wide_spread_pct).mean()) if len(sp) else 0.0

        metrics = {
            'up_ratio': up_ratio,
            'down_ratio': down_ratio,
            'avg_volume': avg_volume,
            'avg_range_pct': avg_range_pct,
            'avg_spread_pct': avg_spread_pct,
            'wide_spread_ratio': wide_spread_ratio,
            'rows': int(len(df)),
        }
        result['metrics'] = metrics

        if down_ratio >= self.thresholds.crash_down_ratio and avg_range_pct >= self.thresholds.high_vol_range_pct:
            result['regime'] = 'CRASH_HIGH_VOL'
            result['reason'] = 'down_ratio and volatility high'
        elif down_ratio >= self.thresholds.crash_down_ratio:
            result['regime'] = 'CRASH'
            result['reason'] = 'down_ratio high'
        elif avg_range_pct >= self.thresholds.high_vol_range_pct:
            result['regime'] = 'HIGH_VOL'
            result['reason'] = 'avg_range_pct high'
        elif avg_volume <= self.thresholds.low_liquidity_avg_volume or wide_spread_ratio >= 0.50:
            result['regime'] = 'LOW_LIQUIDITY'
            result['reason'] = 'volume low or spread wide'
        elif up_ratio >= self.thresholds.inago_up_ratio and avg_range_pct >= 0.70:
            result['regime'] = 'INAGO_MOMENTUM'
            result['reason'] = 'up_ratio and volatility high'
        else:
            result['regime'] = 'NORMAL'
            result['reason'] = 'default normal'

        return result

    def save(self) -> dict:
        os.makedirs(BASE_DIR, exist_ok=True)
        result = self.detect()
        suffix = f'_{self.symbol}' if self.symbol else ''
        path = os.path.join(BASE_DIR, f'market_regime_{self.trade_date}{suffix}.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        result['json_path'] = path
        return result


def detect_market_regime(trade_date: str, symbol: Optional[str] = None) -> dict:
    return MarketRegimeDetector(trade_date=trade_date, symbol=symbol).save()
