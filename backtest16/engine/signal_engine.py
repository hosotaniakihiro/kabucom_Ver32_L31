# ============================================================
# backtest16/engine/signal_engine.py
# ------------------------------------------------------------
# SymbolDay に全戦略(または指定戦略)を適用してシグナルを集める。
# ============================================================

from __future__ import annotations

from typing import List, Optional

from backtest16.features import SymbolDay
from backtest16.engine.execution_engine import Signal
from backtest16.strategies import all_strategies


def detect_signals(sd: SymbolDay, params: dict, strategy_ids: Optional[list] = None) -> List[Signal]:
    out: List[Signal] = []
    strats = all_strategies()
    ids = strategy_ids or list(strats.keys())
    for sid in ids:
        strat = strats.get(sid)
        if strat is None:
            continue
        try:
            sigs = strat.detect(sd, params)
        except Exception:
            sigs = []
        out.extend(sigs or [])
    return out
