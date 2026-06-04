# ============================================================
# File   : trading/push/push_stream/rotation_settings.py
# Version: PRODUCTION-STABLE-REV1.4-PUSH-FAST-LIQ-FAILOPEN
# ------------------------------------------------------------
# PUSH A/Bローテーションの時間設定を集約する。
#
# Default flow:
#   20〜30銘柄登録 -> 45秒維持 -> 次の登録
#
# Notes:
#   - 5秒登録timeoutでは、kabu Station 側の登録HTTP/APIが返る前に
#     rotation A register timeout 5.000s となり、未登録のままPUSH受信0件に
#     なりやすい。
#   - 外部で環境変数を明示指定している場合は、その値を優先する。
#   - REV1.4: PUSH登録前の流動性ガードtimeoutを0.25秒に短縮。
#     ここで2秒待つと、その間にWebSocketがWinError 10054で落ち、
#     rotation registerが ws_not_ready になりやすい。PUSH登録を優先し、
#     低流動性の最終除外はentry側ガードに任せる。
# ============================================================

from __future__ import annotations

import os

from .constants import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    DEFAULT_REGISTER_MAX_SYMBOLS,
)

VERSION = "PRODUCTION-STABLE-REV1.4-PUSH-FAST-LIQ-FAILOPEN"


# 既存 rotation.py 互換のため、import 時点で環境変数デフォルトを注入する。
# os.environ.setdefault のため、ユーザーが明示指定した値は上書きしない。
os.environ.setdefault("PUSH_ROTATION_HOLD_SEC", "45.0")
os.environ.setdefault("PUSH_ROTATION_UNREGISTER_WAIT_SEC", "0.0")
os.environ.setdefault("PUSH_ROTATION_WS_WAIT_LOG_INTERVAL_SEC", "10.0")
os.environ.setdefault("PUSH_ROTATION_REGISTER_TIMEOUT_SEC", "20.0")
os.environ.setdefault("PUSH_ROTATION_LIQ_GUARD_TIMEOUT_SEC", "0.25")
os.environ.setdefault("PUSH_ROTATION_LIQ_GUARD_TIMEOUT_FAIL_OPEN", "1")


# 登録した銘柄を維持する秒数。
ROTATE_HOLD_SEC = float(os.environ["PUSH_ROTATION_HOLD_SEC"])

# REV1.2以降では既定で全解除しないため通常0秒。
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
