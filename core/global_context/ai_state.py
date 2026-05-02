# ============================================================
# File   : core/global_context/ai_state.py
# Version: V35-FINAL-AI-STATE-COMPLETE
# ------------------------------------------------------------
# ✔ meta_exit_controller / models 保持
# ✔ exit_ctx 保持（symbol -> ExitContext）
# ✔ feature_cache 保持
# ✔ force_exit / force_exit_all 統合
# ✔ snapshot安全化（copy返却）
# ✔ スレッド安全
# ✔ 例外安全
# ✔ clear完全対応
# ============================================================

from __future__ import annotations

import logging
from threading import Lock
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class AIState:
    def __init__(self):

        self._lock = Lock()

        # ----------------------------------------------------
        # Controllers / Models
        # ----------------------------------------------------
        self.meta_exit_controller = None
        self.collapse_model = None
        self.inago_model = None

        # ----------------------------------------------------
        # EXIT Context
        # ----------------------------------------------------
        self._exit_ctx: Dict[str, Any] = {}

        # ----------------------------------------------------
        # Feature Cache
        # ----------------------------------------------------
        self.feature_cache = None

        # ----------------------------------------------------
        # FORCE EXIT FLAGS
        # ----------------------------------------------------
        self._force_exit: bool = False
        self._force_exit_all: bool = False

    # ========================================================
    # Controllers
    # ========================================================

    def set_meta_exit_controller(self, controller):
        with self._lock:
            self.meta_exit_controller = controller

    def get_meta_exit_controller(self):
        return self.meta_exit_controller

    def set_collapse_model(self, model):
        with self._lock:
            self.collapse_model = model

    def get_collapse_model(self):
        return self.collapse_model

    def set_inago_model(self, model):
        with self._lock:
            self.inago_model = model

    def get_inago_model(self):
        return self.inago_model

    # ========================================================
    # EXIT CTX
    # ========================================================

    def get_exit_ctx(self, symbol: str) -> Optional[Any]:
        try:
            return self._exit_ctx.get(str(symbol))
        except Exception:
            return None

    def set_exit_ctx(self, symbol: str, ctx: Any):
        try:
            with self._lock:
                self._exit_ctx[str(symbol)] = ctx
        except Exception:
            logger.exception("AIState.set_exit_ctx failed")

    def remove_exit_ctx(self, symbol: str):
        try:
            with self._lock:
                self._exit_ctx.pop(str(symbol), None)
        except Exception:
            logger.exception("AIState.remove_exit_ctx failed")

    def clear_exit_ctx(self):
        try:
            with self._lock:
                self._exit_ctx.clear()
        except Exception:
            logger.exception("AIState.clear_exit_ctx failed")

    def exit_ctx_snapshot(self) -> Dict[str, Any]:
        try:
            # 安全コピーを返す
            with self._lock:
                return dict(self._exit_ctx)
        except Exception:
            return {}

    def set_exit_ctx_dict(self, d: dict):
        # 旧互換：丸ごと代入ケース用
        try:
            if not isinstance(d, dict):
                return
            with self._lock:
                self._exit_ctx = dict(d)
        except Exception:
            logger.exception("AIState.set_exit_ctx_dict failed")

    # ========================================================
    # FORCE EXIT
    # ========================================================

    def set_force_exit(self, value: bool):
        try:
            with self._lock:
                self._force_exit = bool(value)
        except Exception:
            logger.exception("AIState.set_force_exit failed")

    def get_force_exit(self) -> bool:
        try:
            return self._force_exit
        except Exception:
            return False

    def set_force_exit_all(self, value: bool):
        try:
            with self._lock:
                self._force_exit_all = bool(value)
        except Exception:
            logger.exception("AIState.set_force_exit_all failed")

    def get_force_exit_all(self) -> bool:
        try:
            return self._force_exit_all
        except Exception:
            return False

    def clear_force_flags(self):
        try:
            with self._lock:
                self._force_exit = False
                self._force_exit_all = False
        except Exception:
            logger.exception("AIState.clear_force_flags failed")

    # ========================================================
    # Feature Cache
    # ========================================================

    def set_feature_cache(self, cache):
        with self._lock:
            self.feature_cache = cache

    def get_feature_cache(self):
        return self.feature_cache

    # ========================================================
    # CLEAR (全リセット)
    # ========================================================

    def clear(self):
        try:
            with self._lock:
                self._exit_ctx.clear()
                self.meta_exit_controller = None
                self.collapse_model = None
                self.inago_model = None
                self.feature_cache = None
                self._force_exit = False
                self._force_exit_all = False
        except Exception:
            logger.exception("AIState.clear failed")