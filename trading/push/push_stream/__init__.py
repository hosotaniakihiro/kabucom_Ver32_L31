# ============================================================
# File   : trading/push/push_stream/__init__.py
# Version: Ver1.4-PUSH-STREAM-PACKAGE-COMPAT-SPLIT-MODE
# ------------------------------------------------------------
# ✔ 旧 trading.push.push_stream 公開API互換
# ✔ 分割後モジュールの再エクスポート
# ✔ transport / rotation_core / runner / dataframe の公開窓口
# ✔ rotation_settings を先に読み込み、
#   PUSH_ROTATION_HOLD_SEC=4.8 / PUSH_ROTATION_UNREGISTER_WAIT_SEC=0.2
#   のデフォルトを注入する
# ✔ main_database.py 分離運用時、main.py側からのPUSH受信起動をno-op化
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

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
    start_push_stream as _runner_start_push_stream,
    stop_push_stream,
    get_status,
)


def _should_skip_push_stream_start_in_main() -> bool:
    try:
        from data_collectors.split_mode import should_skip_data_collector_work_in_main
        return bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


def start_push_stream(*args, **kwargs):
    """
    PUSH受信本体の公開入口。

    main_database.py 分離運用時:
      - main_database.py / data_collectors_runner.py 側では通常起動
      - main.py 側から呼ばれた場合は二重起動防止のため no-op
    """
    if _should_skip_push_stream_start_in_main():
        logger.warning(
            "[push_stream] start skipped in main process because "
            "AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1; main_database.py handles PUSH."
        )
        return None

    return _runner_start_push_stream(*args, **kwargs)


def start(*args, **kwargs):
    return start_push_stream(*args, **kwargs)


def run_background(*args, **kwargs):
    return start_push_stream(*args, **kwargs)


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
    "start",
    "run_background",
]
