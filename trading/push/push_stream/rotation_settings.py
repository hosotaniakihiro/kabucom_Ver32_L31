# ============================================================
# File   : trading/push/push_stream/rotation_settings.py
# Version: PRODUCTION-STABLE-REV1.2-PUSH-ROTATION-LOW-PRESSURE
# ------------------------------------------------------------
# PUSH A/Bローテーションの時間設定を集約する。
#
# Default flow:
#   30銘柄登録 -> 30秒維持 -> 次の30銘柄登録
#
# Notes:
#   - 50銘柄を4.8秒ごとに回すと、板/約定PUSHが急増して
#     kabu Station側から WinError 10054 で切断される環境がある。
#   - 外部で環境変数を明示指定している場合は、その値を優先する。
# ============================================================

from __future__ import annotations

import os

from .constants import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    DEFAULT_REGISTER_MAX_SYMBOLS,
)

VERSION = "PRODUCTION-STABLE-REV1.2-PUSH-ROTATION-LOW-PRESSURE"


# 既存 rotation.py 互換のため、import 時点で環境変数デフォルトを注入する。
# os.environ.setdefault のため、ユーザーが明示指定した値は上書きしない。
os.environ.setdefault("PUSH_ROTATION_HOLD_SEC", "30.0")
os.environ.setdefault("PUSH_ROTATION_UNREGISTER_WAIT_SEC", "0.0")
os.environ.setdefault("PUSH_ROTATION_WS_WAIT_LOG_INTERVAL_SEC", "10.0")
os.environ.setdefault("PUSH_ROTATION_REGISTER_TIMEOUT_SEC", "5.0")


# 30銘柄を登録したまま維持する秒数。
ROTATE_HOLD_SEC = float(os.environ["PUSH_ROTATION_HOLD_SEC"])

# REV1.2では既定で全解除しないため通常0秒。
UNREGISTER_TO_REGISTER_WAIT_SEC = float(os.environ["PUSH_ROTATION_UNREGISTER_WAIT_SEC"])

# WebSocket準備待ちログの間隔。
WS_WAIT_LOG_INTERVAL_SEC = float(os.environ["PUSH_ROTATION_WS_WAIT_LOG_INTERVAL_SEC"])

# 登録API呼び出しの最大待機秒数。
REGISTER_TIMEOUT_SEC = float(os.environ["PUSH_ROTATION_REGISTER_TIMEOUT_SEC"])


__all__ = [
    "VERSION",
    "DEFAULT_REGISTER_CHUNK_SIZE",
    "DEFAULT_REGISTER_MAX_SYMBOLS",
    "ROTATE_HOLD_SEC",
    "UNREGISTER_TO_REGISTER_WAIT_SEC",
    "WS_WAIT_LOG_INTERVAL_SEC",
    "REGISTER_TIMEOUT_SEC",
]
