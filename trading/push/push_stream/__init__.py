# ============================================================
# File   : trading/push/push_stream/__init__.py
# Version: Ver1.3-PUSH-STREAM-PACKAGE-COMPAT-SPLIT-ROTATION
# ------------------------------------------------------------
# ✔ 旧 trading.push.push_stream 公開API互換
# ✔ 分割後モジュールの再エクスポート
# ✔ transport / rotation_core / runner / dataframe の公開窓口
# ✔ rotation_settings を先に読み込み、
#   PUSH_ROTATION_HOLD_SEC=4.8 / PUSH_ROTATION_UNREGISTER_WAIT_SEC=0.2
#   のデフォルトを注入する
# ============================================================

# rotation系が import 時に os.environ を読む前に、必ず先に読み込む。
from . import rotation_settings as rotation_settings
from . import rotation_symbols as rotation_symbols
from . import rotation_register as rotation_register
from . import rotation_logging as rotation_logging

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
from .rotation_register import register_symbols
from .rotation_core import enable_rotation
from .runner import (
    start_push_stream,
    stop_push_stream,
    get_status,
)

__all__ = [
    "rotation_settings",
    "rotation_symbols",
    "rotation_register",
    "rotation_logging",
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
