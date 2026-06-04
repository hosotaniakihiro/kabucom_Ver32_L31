# ============================================================
# File   : trading/push/push_stream/rotation_settings.py
# Version: PRODUCTION-STABLE-REV1.5-PUSH-50-SYMBOL-AB
# ------------------------------------------------------------
# PUSH A/Bローテーションの時間設定を集約する。
#
# Default flow:
#   A 50銘柄 register -> 4.8秒維持 -> unregister/all -> 0.2秒待機
#   B 50銘柄 register -> 4.8秒維持 -> unregister/all -> 0.2秒待機
#
# Notes:
#   - register/unregister は kabu Station の REST API なので、受信holdは4.8秒でも
#     登録API timeout は十分長く取る。
#   - 外部で環境変数を明示指定している場合は、その値を優先する。
# ============================================================

from __future__ import annotations

import os

from .constants import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    DEFAULT_REGISTER_MAX_SYMBOLS,
)

VERSION = "PRODUCTION-STABLE-REV1.5-PUSH-50-SYMBOL-AB"


# 既存 rotation.py 互換のため、import 時点で環境変数デフォルトを注入する。
# os.environ.setdefault のため、ユーザーが明示指定した値は上書きしない。
os.environ.setdefault("PUSH_ROTATION_HOLD_SEC", "4.8")
os.environ.setdefault("PUSH_ROTATION_UNREGISTER_WAIT_SEC", "0.2")
os.environ.setdefault("PUSH_ROTATION_WS_WAIT_LOG_INTERVAL_SEC", "10.0")
os.environ.setdefault("PUSH_ROTATION_REGISTER_TIMEOUT_SEC", "60.0")
os.environ.setdefault("PUSH_ROTATION_LIQ_GUARD_TIMEOUT_SEC", "0.25")
os.environ.setdefault("PUSH_ROTATION_LIQ_GUARD_TIMEOUT_FAIL_OPEN", "1")


# 登録した銘柄を維持する秒数。
ROTATE_HOLD_SEC = float(os.environ["PUSH_ROTATION_HOLD_SEC"])

# A/B切替時の unregister/all 後の待機秒数。
UNREGISTER_TO_REGISTER_WAIT_SEC = float(os.environ["PUSH_ROTATION_UNREGISTER_WAIT_SEC"])

# WebSocket準備待ちログの間隔。
WS_WAIT_LOG_INTERVAL_SEC = float(os.environ["PUSH_ROTATION_WS_WAIT_LOG_INTERVAL_SEC"])

# 登録API呼び出しの最大待機秒数。hold時間とは別。
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
