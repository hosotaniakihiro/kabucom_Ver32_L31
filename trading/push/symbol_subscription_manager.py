# ============================================================
# File   : trading/push/symbol_subscription_manager.py
# Function:
#   - 旧公開入口の互換ラッパー
#   - 新しい subscription_manager.core の公開 API を再 export
# ------------------------------------------------------------
# Notes:
#   - 既存 import を壊さないためにこのファイルは残す
#   - 実装本体は trading.push.subscription_manager 側へ分離
# ============================================================

from __future__ import annotations

from trading.push.subscription_manager.core import (
    force_refresh_subscriptions,
    refresh_subscriptions,
    start_symbol_subscription_manager,
    stop_symbol_subscription_manager,
)
from trading.push.subscription_manager.transport import (
    set_refresh_callable,
    set_ws_sender,
)

__all__ = [
    "refresh_subscriptions",
    "force_refresh_subscriptions",
    "start_symbol_subscription_manager",
    "stop_symbol_subscription_manager",
    "set_refresh_callable",
    "set_ws_sender",
]