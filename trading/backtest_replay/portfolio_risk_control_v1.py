# ============================================================
# File   : trading/backtest_replay/portfolio_risk_control_v1.py
# Version: Ver01-PORTFOLIO-RISK-CONTROL
# ------------------------------------------------------------
# Portfolio / Exposure / BUY-SELL bias / 同時ポジション数を制御する。
# Strategy Rotation の結果を読み、さらに安全側へ補正する runtime guard。
# 実運用設定ファイルは直接変更しない。
# 出力先: audit/portfolio_risk_YYYYMMDD.json
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Optional

from .strategy_rotation_engine_v1 import StrategyRotationEngine

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


@dataclass
class PortfolioRiskLimits:
    max_positions: int = 1
    max_total_exposure_yen: float = 500000.0
    max_symbol_exposure_yen: float = 500000.0
    max_buy_positions: int = 1
    max_sell_positions: int = 1
    max_entries_per_minute: int = 1
    block_new_entry_when_loss_streak: int = 3
    reduce_size_when_drawdown_yen: float = -5000.0
    emergency_stop_drawdown_yen: float = -15000.0


@dataclass
class PortfolioRiskDecision:
    trade_date: str
    symbol: Optional[str]
    allowed: bool
    allow_buy: bool
    allow_sell: bool
    max_positions: int
    max_total_exposure_yen: float
    max_symbol_exposure_yen: float
    max_entries_per_minute: int
    size_multiplier: float
    emergency_stop: bool
    reasons: list[str]
    source: str = 'portfolio_risk_control_v1'


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


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


def _out_path(trade_date: str, symbol: Optional[str] = None) -> str:
    suffix = f'_{symbol}' if symbol else ''
    return os.path.join(BASE_DIR, f'portfolio_risk_{trade_date}{suffix}.json')


class PortfolioRiskController:
    def __init__(self, trade_date: Optional[str] = None, symbol: Optional[str] = None, limits: Optional[PortfolioRiskLimits] = None):
        self.trade_date = trade_date or _today()
        self.symbol = symbol
        self.limits = limits or PortfolioRiskLimits()

    def evaluate(
        self,
        *,
        open_positions: Any = None,
        current_total_exposure_yen: Any = 0,
        current_symbol_exposure_yen: Any = 0,
        buy_positions: Any = 0,
        sell_positions: Any = 0,
        realized_pnl_today: Any = 0,
        consecutive_losses: Any = 0,
    ) -> PortfolioRiskDecision:
        rotation = StrategyRotationEngine(trade_date=self.trade_date, symbol=self.symbol).build()
        reasons = list(getattr(rotation, 'reasons', []) or [])

        allowed = True
        emergency_stop = False
        size_multiplier = 1.0

        max_positions = min(_safe_int(getattr(rotation, 'max_positions', self.limits.max_positions), self.limits.max_positions), self.limits.max_positions)
        max_entries_per_minute = min(
            _safe_int(getattr(rotation, 'max_entries_per_minute', self.limits.max_entries_per_minute), self.limits.max_entries_per_minute),
            self.limits.max_entries_per_minute,
        )

        allow_buy = bool(getattr(rotation, 'allow_buy', True))
        allow_sell = bool(getattr(rotation, 'allow_sell', True))

        pos_count = 0
        try:
            if isinstance(open_positions, dict):
                pos_count = len(open_positions)
            elif isinstance(open_positions, (list, tuple, set)):
                pos_count = len(open_positions)
            else:
                pos_count = _safe_int(open_positions, 0)
        except Exception:
            pos_count = 0

        total_exposure = _safe_float(current_total_exposure_yen, 0.0)
        symbol_exposure = _safe_float(current_symbol_exposure_yen, 0.0)
        buy_count = _safe_int(buy_positions, 0)
        sell_count = _safe_int(sell_positions, 0)
        pnl = _safe_float(realized_pnl_today, 0.0)
        loss_streak = _safe_int(consecutive_losses, 0)

        if pos_count >= max_positions:
            allowed = False
            reasons.append(f'max positions reached: {pos_count} >= {max_positions}')

        if total_exposure >= self.limits.max_total_exposure_yen:
            allowed = False
            reasons.append(f'total exposure limit reached: {total_exposure:.0f}')

        if symbol_exposure >= self.limits.max_symbol_exposure_yen:
            allowed = False
            reasons.append(f'symbol exposure limit reached: {symbol_exposure:.0f}')

        if buy_count >= self.limits.max_buy_positions:
            allow_buy = False
            reasons.append(f'buy position limit reached: {buy_count}')

        if sell_count >= self.limits.max_sell_positions:
            allow_sell = False
            reasons.append(f'sell position limit reached: {sell_count}')

        if loss_streak >= self.limits.block_new_entry_when_loss_streak:
            allowed = False
            reasons.append(f'loss streak block: {loss_streak}')

        if pnl <= self.limits.emergency_stop_drawdown_yen:
            allowed = False
            allow_buy = False
            allow_sell = False
            emergency_stop = True
            size_multiplier = 0.0
            reasons.append(f'emergency stop drawdown: {pnl:.0f}')
        elif pnl <= self.limits.reduce_size_when_drawdown_yen:
            size_multiplier = 0.5
            reasons.append(f'drawdown size reduced: {pnl:.0f}')

        if not allow_buy and not allow_sell:
            allowed = False
            reasons.append('both BUY and SELL disabled')

        return PortfolioRiskDecision(
            trade_date=self.trade_date,
            symbol=self.symbol,
            allowed=allowed,
            allow_buy=allow_buy,
            allow_sell=allow_sell,
            max_positions=max_positions,
            max_total_exposure_yen=self.limits.max_total_exposure_yen,
            max_symbol_exposure_yen=self.limits.max_symbol_exposure_yen,
            max_entries_per_minute=max_entries_per_minute,
            size_multiplier=size_multiplier,
            emergency_stop=emergency_stop,
            reasons=reasons,
        )

    def save_default(self) -> dict:
        os.makedirs(BASE_DIR, exist_ok=True)
        d = asdict(self.evaluate())
        d['limits'] = asdict(self.limits)
        d['json_path'] = _out_path(self.trade_date, self.symbol)
        d['note'] = 'Portfolio risk runtime候補です。実運用側が読み込むまで本設定には反映されません。'
        with open(d['json_path'], 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=2, default=str)
        return d


def build_portfolio_risk(trade_date: Optional[str] = None, symbol: Optional[str] = None) -> dict:
    return PortfolioRiskController(trade_date=trade_date, symbol=symbol).save_default()


def can_enter_by_portfolio_risk(
    *,
    side: str,
    trade_date: Optional[str] = None,
    symbol: Optional[str] = None,
    open_positions: Any = None,
    current_total_exposure_yen: Any = 0,
    current_symbol_exposure_yen: Any = 0,
    buy_positions: Any = 0,
    sell_positions: Any = 0,
    realized_pnl_today: Any = 0,
    consecutive_losses: Any = 0,
) -> tuple[bool, dict]:
    decision = PortfolioRiskController(trade_date=trade_date, symbol=symbol).evaluate(
        open_positions=open_positions,
        current_total_exposure_yen=current_total_exposure_yen,
        current_symbol_exposure_yen=current_symbol_exposure_yen,
        buy_positions=buy_positions,
        sell_positions=sell_positions,
        realized_pnl_today=realized_pnl_today,
        consecutive_losses=consecutive_losses,
    )
    side_u = str(side or '').upper()
    ok = bool(decision.allowed)
    if side_u == 'BUY' and not decision.allow_buy:
        ok = False
    if side_u == 'SELL' and not decision.allow_sell:
        ok = False
    return ok, asdict(decision)
