# ============================================================
# File   : trading/push/push_stream/__init__.py
# Version: Ver1.9-PUSH-ROTATION-LIQ-KEEP100-MAIN-WS-GUARD-NO-RECURSION
# ------------------------------------------------------------
# ✔ 旧 trading.push.push_stream 公開API互換
# ✔ 分割後モジュールの再エクスポート
# ✔ transport / rotation_core / runner / dataframe の公開窓口
# ✔ rotation_settings を先に読み込み、
#   PUSH_ROTATION_HOLD_SEC=4.8 / PUSH_ROTATION_UNREGISTER_WAIT_SEC=0.2
#   のデフォルトを注入する
# ✔ main_database.py 分離運用時、main.py側からのPUSH受信起動をno-op化
# ✔ PUSH rotation stability patch を自動適用
# ✔ rotation用 liquidity guard で100→50へ崩れる問題を補正
# ✔ runner module を直接importされた場合も main.py 側の重複WS起動を止める
# ✔ runner.start_push_stream を package start_push_stream に差し替えない
#   - package start_push_stream -> _runner_start_push_stream -> wrapper -> package start_push_stream
#     の再帰を防ぐ
# ✔ runner guard は動的 _runner_start_push_stream ではなく、固定退避した本物関数を呼ぶ
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# rotation系が import 時に os.environ を読む前に、必ず先に読み込む。
from . import rotation_settings as rotation_settings
from . import rotation_symbols as rotation_symbols
from . import rotation_register as rotation_register
from . import rotation_logging as rotation_logging

# PUSHローテーション安定化パッチ。
# - rotation_* で unregister_all を強制しない
# - fixed=0ならA/Bを50/50へ補正
# - liquidity guardで100->30へ崩れた場合はfail-open
try:
    from . import rotation_stability_patch as rotation_stability_patch
    rotation_stability_patch.install()
except Exception:
    logger.exception("[push_stream] rotation_stability_patch install failed")

# PUSHローテーション専用の流動性ガード補正。
# stability patch の後に入れることで、100銘柄候補が50銘柄へ削られて
# Bグループが消えるケースを最終的に防ぐ。
try:
    from . import rotation_liquidity_keep100_patch as rotation_liquidity_keep100_patch
    rotation_liquidity_keep100_patch.install()
except Exception:
    logger.exception("[push_stream] rotation_liquidity_keep100_patch install failed")

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
from . import runner as _runner_mod

# 重要:
# runner module 側の本物の start_push_stream をここで退避する。
# package公開入口は _runner_start_push_stream 経由で呼ぶため、
# push_summary_realtime_patch などがここを差し替えることは許可する。
# 一方、runner module 直呼び用guardは、差し替え後の _runner_start_push_stream ではなく
# _TRUE_RUNNER_START_PUSH_STREAM を呼ぶ。これにより guard -> patch wrapper -> guard の再帰を防ぐ。
_runner_start_push_stream = _runner_mod.start_push_stream
_TRUE_RUNNER_START_PUSH_STREAM = _runner_start_push_stream
stop_push_stream = _runner_mod.stop_push_stream
get_status = _runner_mod.get_status

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _should_skip_push_stream_start_in_main() -> bool:
    """
    main_database.py 分離運用では PUSH WebSocket 接続も main_database.py 側に一本化する。

    理由:
      kabu Station PUSH WS を main.py と main_database.py の両方で張ると、
      DISCONNECTED/CONNECTED を数秒ごとに繰り返し、main.py側 summary が stale になる。

    非常用で main.py 側のWSを明示的に使いたい場合のみ:
      AUTOSTOCK_MAIN_PUSH_WS_ENABLED=1
    """
    if _env_bool("AUTOSTOCK_MAIN_PUSH_WS_ENABLED", False):
        return False
    try:
        from data_collectors.split_mode import (
            is_data_collector_process,
            should_skip_data_collector_work_in_main,
            external_data_collectors_enabled,
        )
        if bool(is_data_collector_process()):
            return False
        return bool(external_data_collectors_enabled()) and bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


def _mark_main_ws_skipped() -> None:
    try:
        from .runtime import _safe_set_runtime
        _safe_set_runtime("push_stream_running", False)
        _safe_set_runtime("push_writer_running", False)
        _safe_set_runtime("push_stream_memory_only", True)
        _safe_set_runtime("push_stream_ws_skipped_in_main", True)
    except Exception:
        pass


def start_push_stream(*args, **kwargs):
    """
    PUSH受信本体の公開入口。

    main_database.py 分離運用時:
      - main_database.py / data_collectors_runner.py 側では通常起動
      - main.py 側から呼ばれた場合は二重WebSocket接続防止のため no-op
    """
    if _should_skip_push_stream_start_in_main():
        _mark_main_ws_skipped()
        logger.warning(
            "[push_stream] WS start skipped in main process; "
            "main_database.py handles PUSH WebSocket/registration/storage. "
            "Set AUTOSTOCK_MAIN_PUSH_WS_ENABLED=1 only for emergency standalone mode."
        )
        return None

    return _runner_start_push_stream(*args, **kwargs)


def start(*args, **kwargs):
    return start_push_stream(*args, **kwargs)


def run_background(*args, **kwargs):
    return start_push_stream(*args, **kwargs)


# 直接 `from trading.push.push_stream import runner` された後に
# runner.start_push_stream を呼ばれる経路も同じguardへ寄せる。
#
# ただし、runner.start_push_stream に package の start_push_stream そのものを入れると、
# push_summary_realtime_patch などが runner.start_push_stream を original として保存した場合に
# package start_push_stream -> _runner_start_push_stream -> patch wrapper -> original -> package start_push_stream
# の再帰になる。
#
# そのため runner 側には「固定退避した本物 runner 関数」を呼ぶ専用guardだけを入れる。
def _runner_start_push_stream_guarded(*args, **kwargs):
    if _should_skip_push_stream_start_in_main():
        _mark_main_ws_skipped()
        logger.warning(
            "[push_stream] runner WS start skipped in main process; "
            "main_database.py handles PUSH WebSocket/registration/storage."
        )
        return None
    return _TRUE_RUNNER_START_PUSH_STREAM(*args, **kwargs)


try:
    _runner_start_push_stream_guarded.__wrapped__ = _TRUE_RUNNER_START_PUSH_STREAM  # type: ignore[attr-defined]
    _runner_start_push_stream_guarded._push_stream_runner_true_original = _TRUE_RUNNER_START_PUSH_STREAM  # type: ignore[attr-defined]
    _runner_start_push_stream_guarded._push_stream_runner_guarded_no_recursion = True  # type: ignore[attr-defined]
    _runner_mod.start_push_stream = _runner_start_push_stream_guarded
    _runner_mod.start = _runner_start_push_stream_guarded
    _runner_mod.run_background = _runner_start_push_stream_guarded
except Exception:
    logger.debug("[push_stream] runner guard patch failed", exc_info=True)


__all__ = [
    "rotation_settings",
    "rotation_symbols",
    "rotation_register",
    "rotation_logging",
    "rotation_stability_patch",
    "rotation_liquidity_keep100_patch",
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
