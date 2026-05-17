# ============================================================
# File   : trading/backtest_replay/runtime_params_loader_v1.py
# Version: Ver01-RUNTIME-PARAMS-LOADER
# ------------------------------------------------------------
# Safe Auto Apply が出力した runtime JSON を読み込む。
# main.py 起動時に load_runtime_params() を呼ぶことで、
# 前日学習結果を実運用側から参照できる。
# 実運用設定ファイルは直接上書きしない。
# ============================================================

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

BASE_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\audit'

_RUNTIME_PARAMS: dict[str, Any] = {}


@dataclass
class RuntimeParams:
    stop_loss_pct: float = 0.30
    trail_drop_pct: float = 0.30
    stagnation_seconds: int = 300
    ai_confidence_min: float = 0.55
    min_volume: float = 30000.0
    min_turnover: float = 10000000.0
    source: str = 'default'
    loaded: bool = False
    reason: str = ''


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def _runtime_json_path(trade_date: Optional[str] = None, symbol: Optional[str] = None) -> str:
    td = trade_date or _today()
    suffix = f'_{symbol}' if symbol else ''
    return os.path.join(BASE_DIR, f'replay_runtime_params_{td}{suffix}.json')


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


def load_runtime_params(trade_date: Optional[str] = None, symbol: Optional[str] = None) -> RuntimeParams:
    """runtime JSON を読み込み、RuntimeParams として返す。"""
    global _RUNTIME_PARAMS

    path = _runtime_json_path(trade_date=trade_date, symbol=symbol)
    if not os.path.exists(path):
        rp = RuntimeParams(
            loaded=False,
            reason=f'runtime json not found: {path}',
        )
        _RUNTIME_PARAMS = rp.__dict__.copy()
        return rp

    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception as e:
        rp = RuntimeParams(
            loaded=False,
            reason=f'load failed: {e}',
        )
        _RUNTIME_PARAMS = rp.__dict__.copy()
        return rp

    if not raw.get('ok') or not raw.get('safe_apply'):
        rp = RuntimeParams(
            loaded=False,
            reason=f'safe_apply not ok: {raw.get("reasons") or raw.get("reason")}',
        )
        _RUNTIME_PARAMS = rp.__dict__.copy()
        return rp

    params = raw.get('params') or {}
    rp = RuntimeParams(
        stop_loss_pct=_safe_float(params.get('stop_loss_pct'), 0.30),
        trail_drop_pct=_safe_float(params.get('trail_drop_pct'), 0.30),
        stagnation_seconds=_safe_int(params.get('stagnation_seconds'), 300),
        ai_confidence_min=_safe_float(params.get('ai_confidence_min'), 0.55),
        min_volume=_safe_float(params.get('min_volume'), 30000.0),
        min_turnover=_safe_float(params.get('min_turnover'), 10000000.0),
        source=str(raw.get('source') or 'replay_runtime_params'),
        loaded=True,
        reason=f'loaded: {path}',
    )
    _RUNTIME_PARAMS = rp.__dict__.copy()
    return rp


def get_runtime_params() -> RuntimeParams:
    """最後に読み込んだ runtime params を返す。未読込ならデフォルト。"""
    if not _RUNTIME_PARAMS:
        return RuntimeParams()
    return RuntimeParams(**_RUNTIME_PARAMS)


def get_param(name: str, default: Any = None) -> Any:
    """既存コードから単一パラメータを安全に取得する。"""
    rp = get_runtime_params()
    return getattr(rp, name, default)


def runtime_params_as_dict() -> dict:
    return get_runtime_params().__dict__.copy()
