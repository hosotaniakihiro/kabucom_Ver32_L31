# ============================================================
# File   : trading/backtest_replay/regime_adaptive_runtime_v1.py
# Version: Ver01-REGIME-ADAPTIVE-RUNTIME
# ------------------------------------------------------------
# Market Regime Detector の結果と Runtime Params を統合し、
# 相場状態ごとの安全な運用パラメータを返す。
# 実運用設定ファイルは直接変更しない。
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional

from .runtime_params_loader_v1 import RuntimeParams, load_runtime_params

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


@dataclass
class RegimeRuntimeParams:
    regime: str = 'UNKNOWN'
    allow_buy: bool = True
    allow_sell: bool = True
    stop_loss_pct: float = 0.30
    trail_drop_pct: float = 0.30
    stagnation_seconds: int = 300
    ai_confidence_min: float = 0.55
    min_volume: float = 30000.0
    min_turnover: float = 10000000.0
    max_spread_pct: float = 0.20
    source: str = 'default'
    reason: str = ''
    loaded: bool = False


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def _regime_json_path(trade_date: Optional[str] = None, symbol: Optional[str] = None) -> str:
    td = trade_date or _today()
    suffix = f'_{symbol}' if symbol else ''
    return os.path.join(BASE_DIR, f'market_regime_{td}{suffix}.json')


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


def load_market_regime(trade_date: Optional[str] = None, symbol: Optional[str] = None) -> dict:
    path = _regime_json_path(trade_date=trade_date, symbol=symbol)
    if not os.path.exists(path):
        return {
            'regime': 'UNKNOWN',
            'reason': f'regime json not found: {path}',
            'json_path': path,
        }
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f)
        d['json_path'] = path
        return d
    except Exception as e:
        return {
            'regime': 'UNKNOWN',
            'reason': f'regime json load failed: {e}',
            'json_path': path,
        }


def build_regime_runtime_params(trade_date: Optional[str] = None, symbol: Optional[str] = None) -> RegimeRuntimeParams:
    base: RuntimeParams = load_runtime_params(trade_date=trade_date, symbol=symbol)
    regime_info = load_market_regime(trade_date=trade_date, symbol=symbol)
    regime = str(regime_info.get('regime') or 'UNKNOWN').upper()

    p = RegimeRuntimeParams(
        regime=regime,
        stop_loss_pct=_safe_float(base.stop_loss_pct, 0.30),
        trail_drop_pct=_safe_float(base.trail_drop_pct, 0.30),
        stagnation_seconds=_safe_int(base.stagnation_seconds, 300),
        ai_confidence_min=_safe_float(base.ai_confidence_min, 0.55),
        min_volume=_safe_float(base.min_volume, 30000.0),
        min_turnover=_safe_float(base.min_turnover, 10000000.0),
        max_spread_pct=0.20,
        source=f'regime_adaptive_runtime|base={base.source}',
        reason=str(regime_info.get('reason') or ''),
        loaded=bool(base.loaded),
    )

    # ========================================================
    # レジーム別安全補正
    # ========================================================
    if regime in ('CRASH', 'CRASH_HIGH_VOL'):
        # 暴落日はBUYを弱める。SELLは許可。
        p.allow_buy = False
        p.allow_sell = True
        p.stop_loss_pct = min(p.stop_loss_pct, 0.20)
        p.trail_drop_pct = min(p.trail_drop_pct, 0.20)
        p.ai_confidence_min = max(p.ai_confidence_min, 0.65)
        p.min_volume = max(p.min_volume, 100000.0)
        p.min_turnover = max(p.min_turnover, 30000000.0)
        p.max_spread_pct = 0.10
        p.reason += '|CRASH guard: BUY disabled, liquidity/spread tightened'

    elif regime == 'HIGH_VOL':
        p.allow_buy = True
        p.allow_sell = True
        p.stop_loss_pct = min(p.stop_loss_pct, 0.25)
        p.trail_drop_pct = min(p.trail_drop_pct, 0.20)
        p.ai_confidence_min = max(p.ai_confidence_min, 0.60)
        p.min_volume = max(p.min_volume, 50000.0)
        p.min_turnover = max(p.min_turnover, 20000000.0)
        p.max_spread_pct = 0.15
        p.reason += '|HIGH_VOL guard: tighter stop/trail'

    elif regime == 'LOW_LIQUIDITY':
        p.allow_buy = True
        p.allow_sell = True
        p.stop_loss_pct = min(p.stop_loss_pct, 0.20)
        p.trail_drop_pct = min(p.trail_drop_pct, 0.15)
        p.ai_confidence_min = max(p.ai_confidence_min, 0.70)
        p.min_volume = max(p.min_volume, 100000.0)
        p.min_turnover = max(p.min_turnover, 50000000.0)
        p.max_spread_pct = 0.08
        p.reason += '|LOW_LIQUIDITY guard: very strict liquidity/spread'

    elif regime == 'INAGO_MOMENTUM':
        p.allow_buy = True
        p.allow_sell = True
        p.stop_loss_pct = min(p.stop_loss_pct, 0.25)
        p.trail_drop_pct = min(p.trail_drop_pct, 0.15)
        p.stagnation_seconds = min(p.stagnation_seconds, 180)
        p.ai_confidence_min = max(p.ai_confidence_min, 0.60)
        p.min_volume = max(p.min_volume, 50000.0)
        p.min_turnover = max(p.min_turnover, 20000000.0)
        p.max_spread_pct = 0.12
        p.reason += '|INAGO_MOMENTUM guard: quick trail/stagnation'

    elif regime == 'NORMAL':
        p.allow_buy = True
        p.allow_sell = True
        p.reason += '|NORMAL: base runtime params used'

    else:
        # 不明なら安全側
        p.allow_buy = True
        p.allow_sell = True
        p.ai_confidence_min = max(p.ai_confidence_min, 0.65)
        p.min_volume = max(p.min_volume, 50000.0)
        p.min_turnover = max(p.min_turnover, 20000000.0)
        p.max_spread_pct = 0.15
        p.reason += '|UNKNOWN guard: conservative params'

    return p


def save_regime_runtime_params(trade_date: Optional[str] = None, symbol: Optional[str] = None) -> dict:
    os.makedirs(BASE_DIR, exist_ok=True)
    td = trade_date or _today()
    suffix = f'_{symbol}' if symbol else ''
    path = os.path.join(BASE_DIR, f'regime_runtime_params_{td}{suffix}.json')
    p = build_regime_runtime_params(trade_date=td, symbol=symbol)
    d = asdict(p)
    d['json_path'] = path
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2, default=str)
    return d


def get_regime_param(name: str, default: Any = None, trade_date: Optional[str] = None, symbol: Optional[str] = None) -> Any:
    p = build_regime_runtime_params(trade_date=trade_date, symbol=symbol)
    return getattr(p, name, default)
