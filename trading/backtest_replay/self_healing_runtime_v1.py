# ============================================================
# File   : trading/backtest_replay/self_healing_runtime_v1.py
# Version: Ver01-SELF-HEALING-RUNTIME
# ------------------------------------------------------------
# Failure Pattern Miner の結果を読み、負け方に応じて翌日用の
# 防御強化 runtime params を生成する。
# 実運用設定ファイルは直接変更しない。
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional

from .regime_adaptive_runtime_v1 import RegimeRuntimeParams, build_regime_runtime_params

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


@dataclass
class SelfHealingGuard:
    min_losses_to_apply: int = 2
    wide_spread_ratio_trigger: float = 0.30
    low_liquidity_ratio_trigger: float = 0.30
    stagnation_ratio_trigger: float = 0.30
    high_vol_reversal_ratio_trigger: float = 0.30
    large_loss_ratio_trigger: float = 0.20

    strict_max_spread_pct: float = 0.08
    strict_min_volume: float = 100000.0
    strict_min_turnover: float = 50000000.0
    strict_ai_confidence_min: float = 0.70
    strict_stop_loss_pct: float = 0.20
    strict_trail_drop_pct: float = 0.15
    strict_stagnation_seconds: int = 180


@dataclass
class SelfHealingParams:
    apply_trade_date: str
    learning_trade_date: str
    symbol: Optional[str]
    base_regime: str
    allow_buy: bool
    allow_sell: bool
    stop_loss_pct: float
    trail_drop_pct: float
    stagnation_seconds: int
    ai_confidence_min: float
    min_volume: float
    min_turnover: float
    max_spread_pct: float
    healing_enabled: bool
    healing_reasons: list[str]
    source: str = 'self_healing_runtime_v1'


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def _failure_json_path(trade_date: str, symbol: Optional[str] = None) -> str:
    suffix = f'_{symbol}' if symbol else ''
    return os.path.join(BASE_DIR, f'failure_patterns_{trade_date}{suffix}.json')


def _runtime_json_path(apply_trade_date: str, symbol: Optional[str] = None) -> str:
    suffix = f'_{symbol}' if symbol else ''
    return os.path.join(BASE_DIR, f'self_healing_runtime_{apply_trade_date}{suffix}.json')


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == '':
            return default
        return int(float(v))
    except Exception:
        return default


class SelfHealingRuntime:
    def __init__(
        self,
        learning_trade_date: str,
        apply_trade_date: Optional[str] = None,
        symbol: Optional[str] = None,
        guard: Optional[SelfHealingGuard] = None,
    ):
        self.learning_trade_date = learning_trade_date
        self.apply_trade_date = apply_trade_date or _today()
        self.symbol = symbol
        self.guard = guard or SelfHealingGuard()

    def load_failure_patterns(self) -> dict:
        path = _failure_json_path(self.learning_trade_date, self.symbol)
        if not os.path.exists(path):
            return {
                'ok': False,
                'reason': f'failure pattern json not found: {path}',
                'losses': 0,
                'patterns': {},
                'json_path': path,
            }
        try:
            with open(path, 'r', encoding='utf-8') as f:
                d = json.load(f)
            d['ok'] = True
            d['json_path'] = path
            return d
        except Exception as e:
            return {
                'ok': False,
                'reason': f'failure pattern json load failed: {e}',
                'losses': 0,
                'patterns': {},
                'json_path': path,
            }

    def build(self) -> SelfHealingParams:
        base: RegimeRuntimeParams = build_regime_runtime_params(
            trade_date=self.apply_trade_date,
            symbol=self.symbol,
        )
        failure = self.load_failure_patterns()
        patterns = failure.get('patterns') or {}
        losses = _safe_int(failure.get('losses'), 0)

        p = SelfHealingParams(
            apply_trade_date=self.apply_trade_date,
            learning_trade_date=self.learning_trade_date,
            symbol=self.symbol,
            base_regime=base.regime,
            allow_buy=base.allow_buy,
            allow_sell=base.allow_sell,
            stop_loss_pct=base.stop_loss_pct,
            trail_drop_pct=base.trail_drop_pct,
            stagnation_seconds=base.stagnation_seconds,
            ai_confidence_min=base.ai_confidence_min,
            min_volume=base.min_volume,
            min_turnover=base.min_turnover,
            max_spread_pct=base.max_spread_pct,
            healing_enabled=False,
            healing_reasons=[],
        )

        if not failure.get('ok'):
            p.healing_reasons.append(str(failure.get('reason') or 'failure json not ok'))
            return p

        if losses < self.guard.min_losses_to_apply:
            p.healing_reasons.append(f'losses too few: {losses} < {self.guard.min_losses_to_apply}')
            return p

        def ratio(name: str) -> float:
            return _safe_float(patterns.get(name), 0.0) / max(1, losses)

        wide_spread_ratio = ratio('WIDE_SPREAD_LOSS')
        low_liq_ratio = ratio('LOW_LIQUIDITY_LOSS')
        stagnation_ratio = ratio('STAGNATION_LOSS')
        high_vol_ratio = ratio('HIGH_VOL_REVERSAL_LOSS')
        large_loss_ratio = ratio('LARGE_LOSS')

        if wide_spread_ratio >= self.guard.wide_spread_ratio_trigger:
            p.max_spread_pct = min(p.max_spread_pct, self.guard.strict_max_spread_pct)
            p.ai_confidence_min = max(p.ai_confidence_min, self.guard.strict_ai_confidence_min)
            p.healing_enabled = True
            p.healing_reasons.append(f'wide spread loss ratio high: {wide_spread_ratio:.2f}')

        if low_liq_ratio >= self.guard.low_liquidity_ratio_trigger:
            p.min_volume = max(p.min_volume, self.guard.strict_min_volume)
            p.min_turnover = max(p.min_turnover, self.guard.strict_min_turnover)
            p.ai_confidence_min = max(p.ai_confidence_min, self.guard.strict_ai_confidence_min)
            p.healing_enabled = True
            p.healing_reasons.append(f'low liquidity loss ratio high: {low_liq_ratio:.2f}')

        if stagnation_ratio >= self.guard.stagnation_ratio_trigger:
            p.stagnation_seconds = min(p.stagnation_seconds, self.guard.strict_stagnation_seconds)
            p.trail_drop_pct = min(p.trail_drop_pct, self.guard.strict_trail_drop_pct)
            p.healing_enabled = True
            p.healing_reasons.append(f'stagnation loss ratio high: {stagnation_ratio:.2f}')

        if high_vol_ratio >= self.guard.high_vol_reversal_ratio_trigger:
            p.stop_loss_pct = min(p.stop_loss_pct, self.guard.strict_stop_loss_pct)
            p.trail_drop_pct = min(p.trail_drop_pct, self.guard.strict_trail_drop_pct)
            p.ai_confidence_min = max(p.ai_confidence_min, 0.65)
            p.healing_enabled = True
            p.healing_reasons.append(f'high vol reversal ratio high: {high_vol_ratio:.2f}')

        if large_loss_ratio >= self.guard.large_loss_ratio_trigger:
            p.stop_loss_pct = min(p.stop_loss_pct, self.guard.strict_stop_loss_pct)
            p.max_spread_pct = min(p.max_spread_pct, 0.10)
            p.min_volume = max(p.min_volume, 50000.0)
            p.min_turnover = max(p.min_turnover, 30000000.0)
            p.healing_enabled = True
            p.healing_reasons.append(f'large loss ratio high: {large_loss_ratio:.2f}')

        if not p.healing_enabled:
            p.healing_reasons.append('no failure pattern trigger')

        return p

    def save(self) -> dict:
        os.makedirs(BASE_DIR, exist_ok=True)
        path = _runtime_json_path(self.apply_trade_date, self.symbol)
        p = self.build()
        d = asdict(p)
        d['guard'] = asdict(self.guard)
        d['json_path'] = path
        d['note'] = 'Self-healing runtime候補です。実運用側が読み込むまで本設定には反映されません。'
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2, default=str)
        return d


def build_self_healing_runtime(
    learning_trade_date: str,
    apply_trade_date: Optional[str] = None,
    symbol: Optional[str] = None,
) -> dict:
    return SelfHealingRuntime(
        learning_trade_date=learning_trade_date,
        apply_trade_date=apply_trade_date,
        symbol=symbol,
    ).save()
