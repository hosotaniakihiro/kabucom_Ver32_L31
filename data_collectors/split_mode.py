# ============================================================
# File   : data_collectors/split_mode.py
# Version: DATA-COLLECTORS-SPLIT-MODE-V5-EXPLICIT-MAIN-MEMORY-ONLY
# ------------------------------------------------------------
# Purpose:
#   - main.py と main_database.py の役割分離フラグを共通管理する
#
# Important:
#   2026-06-01 のログで main.py が
#     memory_only=True / writer_ready=False / total_flushed=0
#   のままになっていた。
#   原因は AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1 が残っているだけで
#   main.py 側の PUSH writer / summary save を止めていたこと。
#
# Policy V5:
#   - main_database.py / data_collectors_runner.py 側:
#       AUTOSTOCK_DATA_COLLECTORS_PROCESS=1
#       → DB作成 / ranking取得 / PUSH登録 / PUSH受信 / Yahoo補完保存 /
#         定時サマリーDB保存を実行してよい
#
#   - main.py 側:
#       既定は単独起動扱い。
#       AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1 が残っていても、
#       AUTOSTOCK_MAIN_MEMORY_ONLY=1 または
#       AUTOSTOCK_SKIP_DATA_COLLECTOR_WORK_IN_MAIN=1 を明示しない限り、
#       PUSH DB writer / flush worker / summary save を止めない。
#
#   - main.py を本当にエントリー専用・メモリ専用にしたい場合だけ:
#       set AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1
#       set AUTOSTOCK_MAIN_MEMORY_ONLY=1
#       set AUTOSTOCK_SUMMARY_SAVE_OWNER=database
# ============================================================

from __future__ import annotations

import os


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)

    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
        return True
    if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return bool(default)


def _env_text(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v is None:
        return str(default)
    return str(v).strip()


def is_data_collector_process() -> bool:
    return _env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False)


def external_data_collectors_enabled() -> bool:
    """
    外部データ収集プロセス分離モードの親フラグ。

    注意:
      この値だけでは main.py を memory-only にしない。
      V5では、誤って環境変数が残っただけでDB保存が止まる事故を防ぐため、
      main.py側のスキップは main_memory_only_enabled() も必須にした。
    """
    return _env_bool("AUTOSTOCK_EXTERNAL_DATA_COLLECTORS", False)


def main_memory_only_enabled() -> bool:
    """
    main.py を本当にエントリー専用・メモリ専用にする明示フラグ。

    これが False の場合、AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1 が残っていても、
    main.py単独起動として PUSH DB writer / flush worker を起動する。
    """
    return (
        _env_bool("AUTOSTOCK_MAIN_MEMORY_ONLY", False)
        or _env_bool("AUTOSTOCK_SKIP_DATA_COLLECTOR_WORK_IN_MAIN", False)
        or _env_bool("AUTOSTOCK_ENTRY_ONLY_PROCESS", False)
    )


def _database_owner_default_enabled() -> bool:
    """owner系の既定値を database に寄せてよい状態か。"""
    if is_data_collector_process():
        return True
    return external_data_collectors_enabled() and main_memory_only_enabled()


def yahoo_complement_owner() -> str:
    """
    Yahoo補完の取得・DB保存をどちらのプロセスが担当するか。

    values:
      - database : main_database.py 側
      - main     : main.py 側
      - both     : 両方許可 非推奨
      - none     : Yahoo補完ジョブ登録なし
    """
    default_owner = "database" if _database_owner_default_enabled() else "main"
    owner = _env_text("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER", default_owner).lower()
    if owner not in {"database", "main", "both", "none"}:
        return default_owner
    return owner


def summary_save_owner() -> str:
    """
    定時サマリーDB保存をどちらのプロセスが担当するか。

    values:
      - database : main_database.py / summary_database_runner.py 側
      - main     : main.py 側
      - both     : 両方許可 非推奨。DBロック競合が増える
      - none     : summary DB保存なし

    V5:
      - main.py単独起動の事故防止を優先し、memory-only明示が無ければ既定 main
      - 分離運用時だけ database
    """
    default_owner = "database" if _database_owner_default_enabled() else "main"
    owner = _env_text("AUTOSTOCK_SUMMARY_SAVE_OWNER", default_owner).lower()
    if owner not in {"database", "main", "both", "none"}:
        return default_owner
    return owner


def should_skip_data_collector_work_in_main() -> bool:
    """
    True の場合、main.py側では以下を起動しない。
      - PUSH WebSocket
      - PUSH DB writer
      - PUSH銘柄登録ローテーション
      - ranking DB writer / ranking取得本体

    V5:
      AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1 だけではスキップしない。
      AUTOSTOCK_MAIN_MEMORY_ONLY=1 等の明示がある場合だけスキップする。
    """
    if is_data_collector_process():
        return False
    return external_data_collectors_enabled() and main_memory_only_enabled()


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
    os.environ.setdefault("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER", "database")
    os.environ.setdefault("AUTOSTOCK_SUMMARY_SAVE_OWNER", "database")


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
]
