# ============================================================
# File   : core/global_context/bandit_state.py
# Version: V32-FINAL-BANDIT-STATE
# ------------------------------------------------------------
# ✔ exit_bandit の保持
# ✔ get_weights / update を委譲
# ✔ スレッド安全（参照のため軽い）
# ============================================================

from __future__ import annotations

import logging
from threading import Lock
from typing import Any, Dict

logger = logging.getLogger(__name__)


class BanditState:
    def __init__(self):
        self._lock = Lock()
        self.bandit: Any = None

    def set_bandit(self, bandit):
        with self._lock:
            self.bandit = bandit

    def get_weights(self, cluster_id: int, regime: int, inago_state: int) -> Dict[str, float]:
        try:
            b = self.bandit
            if b is None:
                return {"w_collapse": 0.6, "w_hold": 0.4, "w_take": 0.5}
            return b.get_weights(cluster_id=cluster_id, regime=regime, inago_state=inago_state)
        except Exception:
            logger.exception("BanditState.get_weights failed")
            return {"w_collapse": 0.6, "w_hold": 0.4, "w_take": 0.5}

    def update(self, cluster_id: int, regime: int, inago_state: int, reward: float):
        try:
            b = self.bandit
            if b is None:
                return
            b.update(cluster_id=cluster_id, regime=regime, inago_state=inago_state, reward=reward)
        except Exception:
            logger.exception("BanditState.update failed")