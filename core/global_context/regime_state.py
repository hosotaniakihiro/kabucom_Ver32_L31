# ============================================================
# File   : core/global_context/regime_state.py
# Version: V32-FINAL-REGIME-STATE
# ------------------------------------------------------------
# ✔ regime_model の保持
# ✔ 最新regimeキャッシュ（任意）
# ✔ スレッド安全
# ============================================================

from __future__ import annotations

import logging
from threading import Lock
from typing import Optional, Any

logger = logging.getLogger(__name__)


class RegimeState:
    def __init__(self):
        self._lock = Lock()
        self.model: Optional[Any] = None
        self._last_regime: Optional[int] = None

    def set_model(self, model):
        with self._lock:
            self.model = model

    def predict(self, market_state: dict) -> int:
        try:
            m = self.model
            if m is None:
                return 2
            r = int(m.predict(market_state))
            with self._lock:
                self._last_regime = r
            return r
        except Exception:
            logger.exception("RegimeState.predict failed")
            return 2

    def get_last(self) -> Optional[int]:
        try:
            return self._last_regime
        except Exception:
            return None