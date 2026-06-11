# ============================================================
# File   : trading/push/push_stream/rotation_settings.py
# Version: PRODUCTION-STABLE-REV1.7-PUSH-STRICT-AB-ROTATION
# ------------------------------------------------------------
# PUSH A/Bローテーションの時間設定を集約する。
#
# User design / default flow:
#   A register -> hold 4.8s -> unregister/all -> wait 0.2s -> B register
#   B register -> hold 4.8s -> unregister/all -> wait 0.2s -> A register
#
# Notes:
#   - register/unregister は kabu Station の REST API。
#   - rotation では必ず unregister_all を使う。
#   - 外部で環境変数を明示指定している場合は、その値を優先する。
# ============================================================

from __future__ import annotations

import os

from .constants import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    DEFAULT_REGISTER_MAX_SYMBOLS,
)

VERSION = "PRODUCTION-STABLE-REV1.7-PUSH-STRICT-AB-ROTATION"


# 既存 rotation.py 互換のため、import 時点で環境変数デフォルトを注入する。
# os.environ.setdefault のため、ユーザーが明示指定した値は上書きしない。
os.environ.setdefault("PUSH_ROTATION_HOLD_SEC", "4.8")
os.environ.setdefault("PUSH_ROTATION_UNREGISTER_WAIT_SEC", "0.2")
os.environ.setdefault("PUSH_ROTATION_WAIT_AFTER_CLEAR_SEC", "0.2")
os.environ.setdefault("PUSH_ROTATION_CLEAR_FIRST", "1")
os.environ.setdefault("PUSH_ROTATION_UNREGISTER_FIRST", "1")
os.environ.setdefault("PUSH_ROTATION_REGISTER_FORCE", "1")
os.environ.setdefault("PUSH_ROTATION_REGISTER_REQUIRE_WS", "0")
os.environ.setdefault("PUSH_ROTATION_WS_STABLE_GRACE_SEC", "0.0")
os.environ.setdefault("PUSH_ROTATION_WS_STABLE_MAX_WAIT_SEC", "1.0")
os.environ.setdefault("PUSH_ROTATION_WS_WAIT_LOG_INTERVAL_SEC", "10.0")
os.environ.setdefault("PUSH_ROTATION_REGISTER_TIMEOUT_SEC", "18.0")
os.environ.setdefault("PUSH_ROTATION_LIQ_GUARD_TIMEOUT_SEC", "0.25")
os.environ.setdefault("PUSH_ROTATION_LIQ_GUARD_TIMEOUT_FAIL_OPEN", "1")

# subscription_manager.register_ops 用。
# rotation側は PUSH_ROTATION_UNREGISTER_WAIT_SEC=0.2 を優先する。
# startup等のrotation以外は安全側の既定値を維持する。
os.environ.setdefault("KABU_REGISTER_UNREGISTER_WAIT_SEC", "1.5")
os.environ.setdefault("KABU_REGISTER_COUNT_ERROR_RETRY_WAIT_SEC", "2.0")


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
