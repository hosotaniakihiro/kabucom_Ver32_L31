# ============================================================
# File   : trading/backtest_replay/strategy_rotation_engine_v1.py
# Version: Ver01-STRATEGY-ROTATION-ENGINE
# ------------------------------------------------------------
# Market Regime / Self Healing / Runtime Params を統合し、
# 相場に応じて使う戦略を切り替える。
# 実運用設定ファイルは直接変更しない。
# 出力先: audit/strategy_rotation_YYYYMMDD.json
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional

from .regime_adaptive_runtime_v1 import build_regime_runtime_params

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


@dataclass
class StrategyRotationParams:
    trade_date: str
    symbol: Optional[str]
    regime: str
    mode: str
    allow_summary: bool
    allow_ranking: bool
    allow_tonosama: bool
    allow_early_scalp: bool
    allow_buy: bool
    allow_sell: bool
    max_positions: int
    max_entries_per_minute: int
    ai_confidence_min: float
    min_volume: float
    min_turnover: float
    max_spread_pct: float
    stop_loss_pct: float
    trail_drop_pct: float
    stagnation_seconds: int
    reasons: list[str]
    source: str = 'strategy_rotation_engine_v1'


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def _out_path(trade_date: str, symbol: Optional[str] = None) -> str:
    suffix = f'_{symbol}' if symbol else ''
    return os.path.join(BASE_DIR, f'strategy_rotation_{trade_date}{suffix}.json')


def _load_self_healing(trade_date: str, symbol: Optional[str] = None) -> dict:
    suffix = f'_{symbol}' if symbol else ''
    path = os.path.join(BASE_DIR, f'self_healing_runtime_{trade_date}{suffix}.json')
    if not os.path.exists(path):
        return {'ok': False, 'json_path': path, 'reason': 'self healing json not found'}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        d['ok'] = True
        d['json_path'] = path
        return d
    except Exception as e:
        return {'ok': False, 'json_path': path, 'reason': str(e)}


def _safe_float(v: Any, default: float) -> float:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int) -> int:
    try:
        if v is None or v == '':
            return default
        return int(float(v))
    except Exception:
        return default


class StrategyRotationEngine:
    def __init__(self, trade_date: Optional[str] = None, symbol: Optional[str] = None):
        self.trade_date = trade_date or _today()
        self.symbol = symbol

    def build(self) -> StrategyRotationParams:
        base = build_regime_runtime_params(trade_date=self.trade_date, symbol=self.symbol)
        healing = _load_self_healing(self.trade_date, self.symbol)
        regime = str(base.regime or 'UNKNOWN').upper()
        reasons: list[str] = [f'regime={regime}', f'base_reason={base.reason}']

        # base runtime
        allow_buy = bool(base.allow_buy)
        allow_sell = bool(base.allow_sell)
        stop_loss_pct = _safe_float(base.stop_loss_pct, 0.30)
        trail_drop_pct = _safe_float(base.trail_drop_pct, 0.30)
        stagnation_seconds = _safe_int(base.stagnation_seconds, 300)
        ai_confidence_min = _safe_float(base.ai_confidence_min, 0.55)
        min_volume = _safe_float(base.min_volume, 30000.0)
        min_turnover = _safe_float(base.min_turnover, 10000000.0)
        max_spread_pct = _safe_float(base.max_spread_pct, 0.20)

        if healing.get('ok') and healing.get('healing_enabled'):
            reasons.extend([f'healing={x}' for x in healing.get('healing_reasons', [])])
            allow_buy = bool(healing.get('allow_buy', allow_buy))
            allow_sell = bool(healing.get('allow_sell', allow_sell))
            stop_loss_pct = min(stop_loss_pct, _safe_float(healing.get('stop_loss_pct'), stop_loss_pct))
            trail_drop_pct = min(trail_drop_pct, _safe_float(healing.get('trail_drop_pct'), trail_drop_pct))
            stagnation_seconds = min(stagnation_seconds, _safe_int(healing.get('stagnation_seconds'), stagnation_seconds))
            ai_confidence_min = max(ai_confidence_min, _safe_float(healing.get('ai_confidence_min'), ai_confidence_min))
            min_volume = max(min_volume, _safe_float(healing.get('min_volume'), min_volume))
            min_turnover = max(min_turnover, _safe_float(healing.get('min_turnover'), min_turnover))
            max_spread_pct = min(max_spread_pct, _safe_float(healing.get('max_spread_pct'), max_spread_pct))
        else:
            reasons.append(f'healing_not_applied={healing.get("reason", "not enabled")}')

        # ====================================================
        # 戦略モード決定
        # ====================================================
        mode = 'NORMAL_MIXED'
        allow_summary = True
        allow_ranking = True
        allow_tonosama = True
        allow_early_scalp = True
        max_positions = 1
        max_entries_per_minute = 1

        if regime in ('CRASH', 'CRASH_HIGH_VOL'):
            mode = 'CRASH_SELL_DEFENSIVE'
            allow_buy = False
            allow_sell = True
            allow_summary = True
            allow_ranking = True
            allow_tonosama = False
            allow_early_scalp = False
            max_positions = 1
            max_entries_per_minute = 1
            reasons.append('CRASH: BUY disabled, SELL defensive only')

        elif regime == 'LOW_LIQUIDITY':
            mode = 'LOW_LIQUIDITY_DEFENSIVE'
            allow_summary = True
            allow_ranking = False
            allow_tonosama = False
            allow_early_scalp = False
            max_positions = 1
            max_entries_per_minute = 1
            reasons.append('LOW_LIQUIDITY: ranking/tonosama disabled')

        elif regime == 'HIGH_VOL':
            mode = 'HIGH_VOL_FAST_EXIT'
            allow_summary = True
            allow_ranking = True
            allow_tonosama = False
            allow_early_scalp = True
            max_positions = 1
            max_entries_per_minute = 1
            reasons.append('HIGH_VOL: fast exit, tonosama disabled')

        elif regime == 'INAGO_MOMENTUM':
            mode = 'INAGO_MOMENTUM_SCALP'
            allow_summary = True
            allow_ranking = True
            allow_tonosama = True
            allow_early_scalp = True
            max_positions = 1
            max_entries_per_minute = 2
            reasons.append('INAGO_MOMENTUM: momentum/scalp enabled with quick exit')

        elif regime == 'NORMAL':
            mode = 'NORMAL_MIXED'
            allow_summary = True
            allow_ranking = True
            allow_tonosama = True
            allow_early_scalp = True
            max_positions = 1
            max_entries_per_minute = 1
            reasons.append('NORMAL: all strategies allowed')

        else:
            mode = 'UNKNOWN_CONSERVATIVE'
            allow_summary = True
            allow_ranking = False
            allow_tonosama = False
            allow_early_scalp = False
            max_positions = 1
            max_entries_per_minute = 1
            ai_confidence_min = max(ai_confidence_min, 0.65)
            reasons.append('UNKNOWN: conservative summary only')

        return StrategyRotationParams(
            trade_date=self.trade_date,
            symbol=self.symbol,
            regime=regime,
            mode=mode,
            allow_summary=allow_summary,
            allow_ranking=allow_ranking,
            allow_tonosama=allow_tonosama,
            allow_early_scalp=allow_early_scalp,
            allow_buy=allow_buy,
            allow_sell=allow_sell,
            max_positions=max_positions,
            max_entries_per_minute=max_entries_per_minute,
            ai_confidence_min=ai_confidence_min,
            min_volume=min_volume,
            min_turnover=min_turnover,
            max_spread_pct=max_spread_pct,
            stop_loss_pct=stop_loss_pct,
            trail_drop_pct=trail_drop_pct,
            stagnation_seconds=stagnation_seconds,
            reasons=reasons,
        )

    def save(self) -> dict:
        os.makedirs(BASE_DIR, exist_ok=True)
        p = self.build()
        d = asdict(p)
        path = _out_path(self.trade_date, self.symbol)
        d['json_path'] = path
        d['note'] = 'Strategy rotation候補です。実運用側が読み込むまで本設定には反映されません。'
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2, default=str)
        return d


def build_strategy_rotation(trade_date: Optional[str] = None, symbol: Optional[str] = None) -> dict:
    return StrategyRotationEngine(trade_date=trade_date, symbol=symbol).save()


def get_strategy_param(name: str, default: Any = None, trade_date: Optional[str] = None, symbol: Optional[str] = None) -> Any:
    p = StrategyRotationEngine(trade_date=trade_date, symbol=symbol).build()
    return getattr(p, name, default)
