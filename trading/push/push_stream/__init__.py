# ============================================================
# File   : trading/push/push_stream/__init__.py
# Version: Ver1.2-PUSH-STREAM-PACKAGE-COMPAT-ROTATION-SETTINGS
# ------------------------------------------------------------
# ✔ 旧 trading.push.push_stream 公開API互換
# ✔ 分割後モジュールの再エクスポート
# ✔ transport / rotation / runner / dataframe の公開窓口
# ✔ rotation.py import 前に rotation_settings を読み込み、
#   PUSH_ROTATION_HOLD_SEC=4.8 / PUSH_ROTATION_UNREGISTER_WAIT_SEC=0.2
#   のデフォルトを注入する
# ============================================================

# rotation.py が import 時に os.environ を読むため、必ず先に読み込む。
from . import rotation_settings as rotation_settings

from .transport import (
    set_refresh_callable,
    refresh_subscriptions,
    get_ws_sender,
    wait_until_connected,
    is_connected,
)
from .dataframe import (
    get_push_dataframe,
    clear_push_dataframe,
)
from .rotation import (
    register_symbols,
    enable_rotation,
)
from .runner import (
    start_push_stream,
    stop_push_stream,
    get_status,
)

__all__ = [
    "rotation_settings",
    "set_refresh_callable",
    "refresh_subscriptions",
    "get_ws_sender",
    "wait_until_connected",
    "is_connected",
    "get_push_dataframe",
    "clear_push_dataframe",
    "register_symbols",
    "enable_rotation",
    "start_push_stream",
    "stop_push_stream",
    "get_status",
]
