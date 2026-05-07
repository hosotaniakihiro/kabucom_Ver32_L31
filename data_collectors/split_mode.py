# ============================================================
# File   : data_collectors/split_mode.py
# Version: DATA-COLLECTORS-SPLIT-MODE-V1
# ------------------------------------------------------------
# Purpose:
#   - main.py と main_database.py の役割分離フラグを共通管理する
#
# Policy:
#   - main_database.py / data_collectors_runner.py 側:
#       AUTOSTOCK_DATA_COLLECTORS_PROCESS=1
#       → DB作成 / ranking取得 / PUSH登録 / PUSH受信を実行してよい
#
#   - main.py 側:
#       AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1 なら
#       → DB作成 / ranking取得本体 / PUSH登録 / PUSH受信 / PUSH DB writer を起動しない
#
# Notes:
#   - 既定では分離モードON。
#   - 元のmain.py単独運用に戻したい場合は、起動前に
#       set AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=0
#     を指定する。
# ============================================================

from __future__ import annotations

import os


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)

    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def is_data_collector_process() -> bool:
    return _env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False)


def external_data_collectors_enabled() -> bool:
    return _env_bool("AUTOSTOCK_EXTERNAL_DATA_COLLECTORS", True)


def should_skip_data_collector_work_in_main() -> bool:
    """
    True の場合、main.py側では以下を起動しない。
      - PUSH WebSocket
      - PUSH DB writer
      - PUSH銘柄登録ローテーション
      - ranking DB writer / ranking取得本体
    """
    return external_data_collectors_enabled() and not is_data_collector_process()


def mark_as_data_collector_process() -> None:
    os.environ["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
    os.environ.setdefault("AUTOSTOCK_EXTERNAL_DATA_COLLECTORS", "1")


__all__ = [
    "external_data_collectors_enabled",
    "is_data_collector_process",
    "mark_as_data_collector_process",
    "should_skip_data_collector_work_in_main",
]
