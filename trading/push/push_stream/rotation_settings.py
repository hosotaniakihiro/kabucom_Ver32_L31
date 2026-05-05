# ============================================================
# File   : trading/push/push_stream/rotation_settings.py
# Version: PRODUCTION-STABLE-REV1.1-PUSH-ROTATION-SETTINGS-ENV-DEFAULTS
# ------------------------------------------------------------
# PUSH A/Bローテーションの時間設定を集約する。
#
# Default flow:
#   50銘柄登録 -> 4.8秒維持 -> 全解除 -> 0.2秒待機 -> 次の50銘柄登録
#
# Notes:
#   - 既存 rotation.py が os.environ を直接読んでいるため、
#     package import 時点で setdefault して既存コードを壊さず新デフォルトを反映する。
#   - 外部で環境変数を明示指定している場合は、その値を優先する。
# ============================================================

from __future__ import annotations

import os

from .constants import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    DEFAULT_REGISTER_MAX_SYMBOLS,
)

VERSION = "PRODUCTION-STABLE-REV1.1-PUSH-ROTATION-SETTINGS-ENV-DEFAULTS"


# 既存 rotation.py 互換のため、import 時点で環境変数デフォルトを注入する。
# os.environ.setdefault のため、ユーザーが明示指定した値は上書きしない。
os.environ.setdefault("PUSH_ROTATION_HOLD_SEC", "4.8")
os.environ.setdefault("PUSH_ROTATION_UNREGISTER_WAIT_SEC", "0.2")
os.environ.setdefault("PUSH_ROTATION_WS_WAIT_LOG_INTERVAL_SEC", "4.9")
os.environ.setdefault("PUSH_ROTATION_REGISTER_TIMEOUT_SEC", "3.0")


# 50銘柄を登録したまま維持する秒数。
ROTATE_HOLD_SEC = float(os.environ["PUSH_ROTATION_HOLD_SEC"])

# 全解除後、次の50銘柄を登録するまで待つ秒数。
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
