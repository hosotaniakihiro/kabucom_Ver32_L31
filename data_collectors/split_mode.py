# ============================================================
# File   : data_collectors/split_mode.py
# Version: DATA-COLLECTORS-SPLIT-MODE-V6-DATABASE-OWNER-DEFAULT
# ------------------------------------------------------------
# Purpose:
#   - main.py と main_database.py の役割分離フラグを共通管理する
#
# Policy V6:
#   - main_database.py / data_collectors_runner.py 側:
#       AUTOSTOCK_DATA_COLLECTORS_PROCESS=1
#       → DB作成 / ranking取得 / PUSH登録 / PUSH受信 / Yahoo補完保存 /
#         定時サマリーDB保存を実行してよい
#
#   - main.py 側:
#       エントリー判定・表示・exit・ATS 等のリアルタイム処理を担当する。
#       DB永続保存は原則 main_database.py に集約する。
#       PUSH DB writer / ranking DB writer / Yahoo補完保存 / summary DB保存は
#       main.py 側では起動しない。
#
#   - main.py単独で全保存したい非常用運用だけ:
#       set AUTOSTOCK_FORCE_MAIN_DB_WRITES=1
#       または owner を明示的に main にする。
# ============================================================

from __future__ import annotations

import os
import sys


_TRUE_VALUES = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "disable", "disabled"}


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)

    s = str(v).strip().lower()
    if s in _TRUE_VALUES:
        return True
    if s in _FALSE_VALUES:
        return False
    return bool(default)


def _env_text(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v is None:
        return str(default)
    return str(v).strip()


def _argv_context() -> str:
    try:
        text = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in text or "data_collectors_runner" in text:
            return "main_database"
        if "main.py" in text:
            return "main"
    except Exception:
        pass
    return "unknown"


def is_data_collector_process() -> bool:
    return (
        _env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False)
        or _env_bool("AUTOSTOCK_MAIN_DATABASE_PROCESS", False)
        or _env_bool("AUTOSTOCK_SUMMARY_DB_WRITER", False)
        or _argv_context() == "main_database"
    )


def force_main_db_writes_enabled() -> bool:
    """非常用。main.py単独でDB保存も行う場合だけON。"""
    return _env_bool("AUTOSTOCK_FORCE_MAIN_DB_WRITES", False)


def external_data_collectors_enabled() -> bool:
    """
    外部データ収集プロセス分離モードの親フラグ。

    V6では、DB保存を main_database.py に集約するため既定ON。
    main.py単独保存に戻す場合は AUTOSTOCK_FORCE_MAIN_DB_WRITES=1 を使う。
    """
    return _env_bool("AUTOSTOCK_EXTERNAL_DATA_COLLECTORS", True)


def main_memory_only_enabled() -> bool:
    """
    main.py をエントリー専用・メモリ専用として扱うか。
    V6既定では main.py はDB writerを持たない。
    """
    if is_data_collector_process():
        return False
    if force_main_db_writes_enabled():
        return False
    return (
        _argv_context() == "main"
        or _env_bool("AUTOSTOCK_MAIN_MEMORY_ONLY", True)
        or _env_bool("AUTOSTOCK_SKIP_DATA_COLLECTOR_WORK_IN_MAIN", True)
        or _env_bool("AUTOSTOCK_ENTRY_ONLY_PROCESS", False)
    )


def _database_owner_default_enabled() -> bool:
    """owner系の既定値を database に寄せる。"""
    if force_main_db_writes_enabled():
        return False
    return True


def yahoo_complement_owner() -> str:
    """
    Yahoo補完の取得・DB保存をどちらのプロセスが担当するか。

    values:
      - database : main_database.py 側
      - main     : main.py 側（非常用）
      - both     : 両方許可 非推奨
      - none     : Yahoo補完ジョブ登録なし
    """
    default_owner = "database" if _database_owner_default_enabled() else "main"
    owner = _env_text("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER", default_owner).lower()
    if force_main_db_writes_enabled() and owner == "database":
        owner = "main"
    if owner not in {"database", "main", "both", "none"}:
        return default_owner
    return owner


def summary_save_owner() -> str:
    """
    定時サマリーDB保存をどちらのプロセスが担当するか。

    values:
      - database : main_database.py / summary_database_runner.py 側
      - main     : main.py 側（非常用）
      - both     : 両方許可 非推奨。DBロック競合が増える
      - none     : summary DB保存なし
    """
    default_owner = "database" if _database_owner_default_enabled() else "main"
    owner = _env_text("AUTOSTOCK_SUMMARY_SAVE_OWNER", default_owner).lower()
    if force_main_db_writes_enabled() and owner == "database":
        owner = "main"
    if owner not in {"database", "main", "both", "none"}:
        return default_owner
    return owner


def should_skip_data_collector_work_in_main() -> bool:
    """
    True の場合、main.py側では以下を起動しない。
      - PUSH DB writer
      - PUSH銘柄登録ローテーション
      - ranking DB writer / ranking取得本体
      - Yahoo補完保存
      - summary DB保存

    PUSH WebSocket自体はエントリー判定で必要なため、呼び出し側で別途制御する。
    """
    if is_data_collector_process():
        return False
    if force_main_db_writes_enabled():
        return False
    return True


def should_run_yahoo_complement_in_this_process() -> bool:
    """Yahoo補完の取得・DB保存ジョブをこのプロセスで動かしてよいか。"""
    owner = yahoo_complement_owner()

    if owner == "none":
        return False
    if owner == "both":
        return True
    if owner == "database":
        return is_data_collector_process()
    if owner == "main":
        return not is_data_collector_process()
    return False


def should_run_summary_save_in_this_process() -> bool:
    """summary DB保存をこのプロセスで動かしてよいか。"""
    owner = summary_save_owner()

    if owner == "none":
        return False
    if owner == "both":
        return True
    if owner == "database":
        return is_data_collector_process()
    if owner == "main":
        return not is_data_collector_process()
    return False


def should_skip_yahoo_complement_in_main() -> bool:
    return not is_data_collector_process() and not should_run_yahoo_complement_in_this_process()


def should_skip_summary_save_in_this_process() -> bool:
    return not should_run_summary_save_in_this_process()


def mark_as_data_collector_process() -> None:
    os.environ["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
    os.environ.setdefault("AUTOSTOCK_EXTERNAL_DATA_COLLECTORS", "1")
    os.environ["AUTOSTOCK_YAHOO_COMPLEMENT_OWNER"] = "database"
    os.environ["AUTOSTOCK_SUMMARY_SAVE_OWNER"] = "database"
    os.environ["SUMMARY_DB_WRITER_ROLE"] = "database"
    os.environ.setdefault("AUTOSTOCK_MAIN_MEMORY_ONLY", "0")
    os.environ.setdefault("AUTOSTOCK_SKIP_DATA_COLLECTOR_WORK_IN_MAIN", "0")


__all__ = [
    "external_data_collectors_enabled",
    "main_memory_only_enabled",
    "is_data_collector_process",
    "mark_as_data_collector_process",
    "should_skip_data_collector_work_in_main",
    "yahoo_complement_owner",
    "summary_save_owner",
    "should_run_yahoo_complement_in_this_process",
    "should_run_summary_save_in_this_process",
    "should_skip_yahoo_complement_in_main",
    "should_skip_summary_save_in_this_process",
    "force_main_db_writes_enabled",
]
