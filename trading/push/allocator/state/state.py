# ============================================================
# File   : trading/push/allocator/state.py
# Version: Ver1.0-PRODUCTION-PUSH-ALLOCATOR-STATE
# ------------------------------------------------------------
# ✔ push allocator state manager
# ✔ current push symbol tracking
# ✔ min_hold enforcement
# ✔ churn prevention
# ✔ runtime safe
# ✔ production ready
# ============================================================

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Dict, Set, List

logger = logging.getLogger(__name__)


# ============================================================
# symbol state
# ============================================================

@dataclass
class SymbolState:
    """
    各銘柄の状態
    """

    symbol: str
    added_ts: float = field(default_factory=time.time)
    last_score: float = 0.0


# ============================================================
# allocator state
# ============================================================

class AllocatorState:
    """
    push slot allocator の状態管理

    保持する情報

    ・現在 push 中の銘柄
    ・登録時間（min_hold制御）
    ・前回 score
    """

    def __init__(self):

        # symbol -> SymbolState
        self._states: Dict[str, SymbolState] = {}

    # --------------------------------------------------------
    # current symbols
    # --------------------------------------------------------

    def current_symbols(self) -> Set[str]:
        return set(self._states.keys())

    # --------------------------------------------------------
    # contains
    # --------------------------------------------------------

    def contains(self, symbol: str) -> bool:
        return symbol in self._states

    # --------------------------------------------------------
    # add
    # --------------------------------------------------------

    def add(self, symbol: str, score: float = 0.0):

        if symbol in self._states:
            return

        self._states[symbol] = SymbolState(
            symbol=symbol,
            last_score=score,
        )

    # --------------------------------------------------------
    # remove
    # --------------------------------------------------------

    def remove(self, symbol: str):

        if symbol in self._states:
            del self._states[symbol]

    # --------------------------------------------------------
    # bulk update
    # --------------------------------------------------------

    def update_symbols(self, symbols: Set[str], scores: Dict[str, float] | None = None):
        """
        allocator結果を反映
        """

        scores = scores or {}

        new_symbols = set(symbols)
        old_symbols = set(self._states.keys())

        to_add = new_symbols - old_symbols
        to_remove = old_symbols - new_symbols

        for s in to_add:
            self.add(s, scores.get(s, 0.0))

        for s in to_remove:
            self.remove(s)

        for s in new_symbols:
            if s in self._states:
                self._states[s].last_score = scores.get(
                    s, self._states[s].last_score
                )

    # --------------------------------------------------------
    # min_hold check
    # --------------------------------------------------------

    def can_remove(self, symbol: str, min_hold_seconds: int) -> bool:

        st = self._states.get(symbol)

        if st is None:
            return True

        held = time.time() - st.added_ts

        return held >= min_hold_seconds

    # --------------------------------------------------------
    # score
    # --------------------------------------------------------

    def last_score(self, symbol: str) -> float:

        st = self._states.get(symbol)

        if st is None:
            return 0.0

        return st.last_score

    # --------------------------------------------------------
    # debug
    # --------------------------------------------------------

    def snapshot(self) -> List[str]:
        return list(self._states.keys())


# ============================================================
# singleton
# ============================================================

allocator_state = AllocatorState()