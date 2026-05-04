# ============================================================
# trading/positions/position_sync.py
# ------------------------------------------------------------
# ✔ 再起動・不整合復旧のため Position API を使用
# ✔ 常時ポーリングは禁止
# ✔ 条件付き・低頻度・API Gate 経由
# ============================================================

import time
import logging

from core.api_gate import api_gate
from kabu_api.positions import get_positions
from trading.state.position_state import position_state

logger = logging.getLogger(__name__)


class PositionSyncManager:
    """
    Position API 再同期コントローラ
    """

    def __init__(self):
        self.last_sync = 0.0
        self.interval_sec = 60     # ★ 最大でも1分に1回
        self.force = True          # 起動直後は必ず同期

    def maybe_sync(self):
        """
        条件付きで Position API を叩く
        """
        now = time.time()
        if not self.force and (now - self.last_sync) < self.interval_sec:
            return

        def _sync():
            positions = get_positions()
            if not positions:
                logger.warning("[PositionSync] empty positions")
                return

            # 既存の PositionState に反映
            # ※ rebuild_from_api は次で実装
            position_state.rebuild_from_api(positions)
            logger.info("[PositionSync] synced from Position API")

        self.force = False
        self.last_sync = now

        api_gate.call(
            key="positions",
            min_interval=self.interval_sec,
            func=_sync,
        )

    def on_ws_reconnect(self):
        """
        WebSocket 再接続時に強制同期
        """
        logger.info("[PositionSync] WS reconnect → force sync")
        self.force = True
