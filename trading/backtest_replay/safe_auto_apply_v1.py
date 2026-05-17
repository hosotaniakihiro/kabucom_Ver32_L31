# ============================================================
# File   : trading/backtest_replay/safe_auto_apply_v1.py
# Version: Ver01-SAFE-AUTO-APPLY
# ------------------------------------------------------------
# Daily Auto Learning の結果を翌営業日向け runtime 設定として安全反映する。
# 実運用設定ファイルは直接上書きしない。
# 出力先: audit/replay_runtime_params_YYYYMMDD.json
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Optional

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'


@dataclass
class SafeApplyGuard:
    min_trades: int = 3
    min_win_rate: float = 0.35
    min_gross_pnl: float = -3000.0
    max_stop_loss_pct: float = 0.50
    min_stop_loss_pct: float = 0.10
    max_trail_drop_pct: float = 0.50
    min_trail_drop_pct: float = 0.05
    max_ai_confidence_change: float = 0.15
    default_ai_confidence_min: float = 0.55


class SafeAutoApply:
    def __init__(self, learning_trade_date: str, apply_trade_date: str, symbol: Optional[str] = None, guard: Optional[SafeApplyGuard] = None):
        self.learning_trade_date = learning_trade_date
        self.apply_trade_date = apply_trade_date
        self.symbol = symbol
        self.guard = guard or SafeApplyGuard()

    def _learning_json_path(self) -> str:
        suffix = f'_{self.symbol}' if self.symbol else ''
        return os.path.join(BASE_DIR, f'replay_learning_{self.learning_trade_date}{suffix}.json')

    def _runtime_json_path(self) -> str:
        suffix = f'_{self.symbol}' if self.symbol else ''
        return os.path.join(BASE_DIR, f'replay_runtime_params_{self.apply_trade_date}{suffix}.json')

    def _load_learning_result(self) -> dict:
        path = self._learning_json_path()
        if not os.path.exists(path):
            return {
                'ok': False,
                'reason': f'learning json not found: {path}',
            }
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    @staticmethod
    def _safe_float(v, default: float = 0.0) -> float:
        try:
            if v is None or v == '':
                return default
            return float(v)
        except Exception:
            return default

    @staticmethod
    def _safe_int(v, default: int = 0) -> int:
        try:
            if v is None or v == '':
                return default
            return int(float(v))
        except Exception:
            return default

    def validate(self, learning: dict) -> tuple[bool, list[str]]:
        reasons = []
        if not learning.get('ok'):
            reasons.append(str(learning.get('reason') or 'learning result not ok'))
            return False, reasons

        best = learning.get('best') or {}
        trades = self._safe_int(best.get('trades'), 0)
        win_rate = self._safe_float(best.get('win_rate'), 0.0)
        gross_pnl = self._safe_float(best.get('gross_pnl'), 0.0)
        stop_loss_pct = self._safe_float(best.get('stop_loss_pct'), 0.30)
        trail_drop_pct = self._safe_float(best.get('trail_drop_pct'), 0.30)
        ai_confidence_min = self._safe_float(best.get('ai_confidence_min'), self.guard.default_ai_confidence_min)

        if trades < self.guard.min_trades:
            reasons.append(f'trades too few: {trades} < {self.guard.min_trades}')
        if win_rate < self.guard.min_win_rate:
            reasons.append(f'win_rate too low: {win_rate:.3f} < {self.guard.min_win_rate:.3f}')
        if gross_pnl < self.guard.min_gross_pnl:
            reasons.append(f'gross_pnl too low: {gross_pnl:.1f} < {self.guard.min_gross_pnl:.1f}')
        if not (self.guard.min_stop_loss_pct <= stop_loss_pct <= self.guard.max_stop_loss_pct):
            reasons.append(f'stop_loss_pct out of range: {stop_loss_pct}')
        if not (self.guard.min_trail_drop_pct <= trail_drop_pct <= self.guard.max_trail_drop_pct):
            reasons.append(f'trail_drop_pct out of range: {trail_drop_pct}')

        if abs(ai_confidence_min - self.guard.default_ai_confidence_min) > self.guard.max_ai_confidence_change:
            reasons.append(
                f'ai_confidence change too large: {ai_confidence_min} default={self.guard.default_ai_confidence_min}'
            )

        return len(reasons) == 0, reasons

    def build_runtime_params(self, learning: dict) -> dict:
        best = learning.get('best') or {}
        return {
            'apply_trade_date': self.apply_trade_date,
            'learning_trade_date': self.learning_trade_date,
            'symbol': self.symbol,
            'source': 'daily_auto_learning_v1',
            'safe_apply': True,
            'params': {
                'stop_loss_pct': self._safe_float(best.get('stop_loss_pct'), 0.30),
                'trail_drop_pct': self._safe_float(best.get('trail_drop_pct'), 0.30),
                'stagnation_seconds': self._safe_int(best.get('stagnation_seconds'), 300),
                'ai_confidence_min': self._safe_float(best.get('ai_confidence_min'), 0.55),
                'min_volume': self._safe_float(best.get('min_volume'), 30000.0),
                'min_turnover': self._safe_float(best.get('min_turnover'), 10000000.0),
            },
            'learning_summary': {
                'trades': self._safe_int(best.get('trades'), 0),
                'gross_pnl': self._safe_float(best.get('gross_pnl'), 0.0),
                'win_rate': self._safe_float(best.get('win_rate'), 0.0),
                'score': self._safe_float(best.get('score'), 0.0),
            },
            'guard': asdict(self.guard),
            'note': 'このJSONは推奨runtime値です。実運用側が読み込むまで本設定には反映されません。',
        }

    def apply(self) -> dict:
        os.makedirs(BASE_DIR, exist_ok=True)
        learning = self._load_learning_result()
        ok, reasons = self.validate(learning)
        out_path = self._runtime_json_path()

        if not ok:
            result = {
                'ok': False,
                'apply_trade_date': self.apply_trade_date,
                'learning_trade_date': self.learning_trade_date,
                'symbol': self.symbol,
                'reasons': reasons,
                'learning_json': self._learning_json_path(),
                'runtime_json': out_path,
                'note': '安全ガードNGのため runtime params は反映しません。',
            }
        else:
            result = self.build_runtime_params(learning)
            result['ok'] = True
            result['runtime_json'] = out_path

        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)

        return result


def safe_auto_apply(learning_trade_date: str, apply_trade_date: str, symbol: Optional[str] = None) -> dict:
    return SafeAutoApply(
        learning_trade_date=learning_trade_date,
        apply_trade_date=apply_trade_date,
        symbol=symbol,
    ).apply()
