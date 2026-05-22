# ============================================================
# File   : data_collectors/split_mode.py
# Version: DATA-COLLECTORS-SPLIT-MODE-V3-SUMMARY-SAVE-OWNER
# ------------------------------------------------------------
# Purpose:
#   - main.py と main_database.py の役割分離フラグを共通管理する
#
# Policy:
#   - main_database.py / data_collectors_runner.py 側:
#       AUTOSTOCK_DATA_COLLECTORS_PROCESS=1
#       → DB作成 / ranking取得 / PUSH登録 / PUSH受信 / Yahoo補完保存 /
#         定時サマリーDB保存を実行してよい
#
#   - main.py 側:
#       AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=1 なら
#       → DB作成 / ranking取得本体 / PUSH登録 / PUSH受信 / PUSH DB writer を起動しない
#       → Yahoo補完の取得・保存ジョブも起動しない
#       → 定時サマリーDB保存も main_database.py 側へ寄せる
#       → Yahoo補完済みsummary DBは読み込んで利用してよい
#
# Notes:
#   - 既定では分離モードON。
#   - 元のmain.py単独運用に戻したい場合は、起動前に
#       set AUTOSTOCK_EXTERNAL_DATA_COLLECTORS=0
#       set AUTOSTOCK_SUMMARY_SAVE_OWNER=main
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


def _env_text(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v is None:
        return str(default)
    return str(v).strip()


def is_data_collector_process() -> bool:
    return _env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False)


def external_data_collectors_enabled() -> bool:
    return _env_bool("AUTOSTOCK_EXTERNAL_DATA_COLLECTORS", True)


def yahoo_complement_owner() -> str:
    """
    Yahoo補完の取得・DB保存をどちらのプロセスが担当するか。

    values:
      - database : main_database.py 側
      - main     : main.py 側
      - both     : 両方許可 非推奨
      - none     : Yahoo補完ジョブ登録なし
    """
    owner = _env_text("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER", "database").lower()
    if owner not in {"database", "main", "both", "none"}:
        return "database"
    return owner


def summary_save_owner() -> str:
    """
    定時サマリーDB保存をどちらのプロセスが担当するか。

    values:
      - database : main_database.py / summary_database_runner.py 側
      - main     : main.py 側
      - both     : 両方許可 非推奨。DBロック競合が増える
      - none     : summary DB保存なし

    既定は database。
    main.py は計算・表示・AI/entry用、main_database.py はDB保存用に分離する。
    """
    owner = _env_text("AUTOSTOCK_SUMMARY_SAVE_OWNER", "database").lower()
    if owner not in {"database", "main", "both", "none"}:
        return "database"
    return owner


def should_skip_data_collector_work_in_main() -> bool:
    """
    True の場合、main.py側では以下を起動しない。
      - PUSH WebSocket
      - PUSH DB writer
      - PUSH銘柄登録ローテーション
      - ranking DB writer / ranking取得本体
    """
    return external_data_collectors_enabled() and not is_data_collector_process()


def should_run_yahoo_complement_in_this_process() -> bool:
    """
    Yahoo補完の取得・DB保存ジョブをこのプロセスで動かしてよいか。

    main_database.py 側:
      AUTOSTOCK_DATA_COLLECTORS_PROCESS=1 なので database owner なら True

    main.py 側:
      既定では external collectors ON かつ owner=database なので False
    """
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
    """
    summary DB保存をこのプロセスで動かしてよいか。

    重要:
      - main.py と main_database.py が同じ summary DB に同時保存すると、
        interval lock timeout / database is locked / 長時間tick の原因になる。
      - 既定では database owner のため、main.py はDB保存をスキップする。
      - main.py 側は計算・表示・AI判定・エントリーに専念する。
    """
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