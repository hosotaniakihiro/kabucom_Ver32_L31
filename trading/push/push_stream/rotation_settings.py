# ============================================================
# File   : trading/push/push_stream/rotation_settings.py
# Version: PRODUCTION-STABLE-REV1-PUSH-ROTATION-SETTINGS
# ------------------------------------------------------------
# PUSH A/Bローテーションの時間設定を集約する。
#
# Default flow:
#   50銘柄登録 -> 4.8秒維持 -> 全解除 -> 0.2秒待機 -> 次の50銘柄登録
# ============================================================

from __future__ import annotations

import os

from .constants import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    DEFAULT_REGISTER_MAX_SYMBOLS,
)

VERSION = "PRODUCTION-STABLE-REV1-PUSH-ROTATION-SETTINGS"


# 50銘柄を登録したまま維持する秒数。
ROTATE_HOLD_SEC = float(
    os.environ.get("PUSH_ROTATION_HOLD_SEC", "4.8")
)

# 全解除後、次の50銘柄を登録するまで待つ秒数。
UNREGISTER_TO_REGISTER_WAIT_SEC = float(
    os.environ.get("PUSH_ROTATION_UNREGISTER_WAIT_SEC", "0.2")
)

# WebSocket準備待ちログの間隔。
WS_WAIT_LOG_INTERVAL_SEC = float(
    os.environ.get("PUSH_ROTATION_WS_WAIT_LOG_INTERVAL_SEC", "4.9")
)

# 登録API呼び出しの最大待機秒数。
REGISTER_TIMEOUT_SEC = float(
    os.environ.get("PUSH_ROTATION_REGISTER_TIMEOUT_SEC", "3.0")
)


__all__ = [
    "VERSION",
    "DEFAULT_REGISTER_CHUNK_SIZE",
    "DEFAULT_REGISTER_MAX_SYMBOLS",
    "ROTATE_HOLD_SEC",
    "UNREGISTER_TO_REGISTER_WAIT_SEC",
    "WS_WAIT_LOG_INTERVAL_SEC",
    "REGISTER_TIMEOUT_SEC",
]
