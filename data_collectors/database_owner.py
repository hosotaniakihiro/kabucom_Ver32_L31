# -*- coding: utf-8 -*-
"""DB 書き込みオーナーを main_database.py / データ収集プロセスへ寄せる判定。

旧 core/startup/database_owner_runtime_patch.py を本文化したもの。

旧パッチは install 時に
  data_collectors.split_mode          の 11 関数
  scheduler_jobs.summary.runner_core  の _summary_save_enabled / _is_database_process
  core.startup.push.storage_bootstrap の _should_skip_push_storage_start_in_main
を**完全置換**していた (元関数は一度も呼ばない)。巻き戻しも watcher も無い。

本文化では各ホストが呼び出し時に owner_override_enabled() を見て、
真ならここの実装、偽なら素の実装を使う。有効化の導線とスコープ判定は
旧構成と同じ core/startup/runtime_env/defaults_patch.py の
_install_database_owner() 側に残してある
(context in DB_CONTEXTS|TRADING_CONTEXTS|GENERIC_HELPER_CONTEXTS かつ
 DISABLE_DATABASE_OWNER_RUNTIME_PATCH != "1")。
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

VERSION = "V1-DATABASE-OWNER-ONLY"
_TRUE_VALUES = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "disable", "disabled"}

_OVERRIDE_ENABLED = False


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in _TRUE_VALUES:
        return True
    if s in _FALSE_VALUES:
        return False
    return bool(default)


def _argv_context() -> str:
    try:
        text = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in text or "data_collectors_runner" in text:
            return "main_database"
        if "main.py" in text:
            return "main"
        if text:
            return "helper"
    except Exception:
        pass
    return "unknown"


def _is_database_process() -> bool:
    context = _argv_context()
    return (
        context == "main_database"
        or _env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False)
        or _env_bool("AUTOSTOCK_MAIN_DATABASE_PROCESS", False)
        or _env_bool("AUTOSTOCK_SUMMARY_DB_WRITER", False)
    )


def _is_main_process() -> bool:
    return _argv_context() == "main" and not _is_database_process()


def _apply_env_defaults() -> None:
    # DB owner is always main_database/data_collector unless explicitly disabled.
    os.environ.setdefault("AUTOSTOCK_EXTERNAL_DATA_COLLECTORS", "1")
    os.environ.setdefault("AUTOSTOCK_SUMMARY_SAVE_OWNER", "database")
    os.environ.setdefault("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER", "database")

    if _is_database_process():
        os.environ.setdefault("AUTOSTOCK_DATA_COLLECTORS_PROCESS", "1")
        os.environ.setdefault("SUMMARY_DB_WRITER_ROLE", "database")
        os.environ.setdefault("SUMMARY_SKIP_DB_SAVE_IN_MAIN", "0")
        os.environ.setdefault("AUTOSTOCK_MAIN_MEMORY_ONLY", "0")
        os.environ.setdefault("AUTOSTOCK_SKIP_DATA_COLLECTOR_WORK_IN_MAIN", "0")
    elif _is_main_process():
        # main.py is explicitly memory/entry only.  Do not use setdefault here;
        # this must override stale shell variables from older runs.
        os.environ["AUTOSTOCK_MAIN_MEMORY_ONLY"] = "1"
        os.environ["AUTOSTOCK_SKIP_DATA_COLLECTOR_WORK_IN_MAIN"] = "1"
        os.environ["SUMMARY_MAIN_ENTRY_ONLY"] = "1"
        os.environ["SUMMARY_SKIP_DB_SAVE_IN_MAIN"] = "1"
        os.environ["SUMMARY_DB_WRITER_ROLE"] = "entry_only"
        os.environ["AUTOSTOCK_SUMMARY_SAVE_OWNER"] = "database"
        os.environ["AUTOSTOCK_YAHOO_COMPLEMENT_OWNER"] = "database"
        os.environ.setdefault("AUTOSTOCK_ENTRY_ONLY_PROCESS", "1")


def owner_override_enabled() -> bool:
    """旧 install() が走ったかどうか。各ホストの分岐条件。"""
    return bool(_OVERRIDE_ENABLED)


def enable_owner_override() -> bool:
    """旧 install()。差し替えの代わりにフラグを立て、env 既定値だけ適用する。"""
    global _OVERRIDE_ENABLED
    if _OVERRIDE_ENABLED:
        return True
    try:
        _apply_env_defaults()
        _OVERRIDE_ENABLED = True
        logger.warning(
            "[DATABASE OWNER] enabled version=%s context=%s db_process=%s main_process=%s summary_owner=%s yahoo_owner=%s main_memory_only=%s",
            VERSION,
            _argv_context(),
            _is_database_process(),
            _is_main_process(),
            os.environ.get("AUTOSTOCK_SUMMARY_SAVE_OWNER"),
            os.environ.get("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER"),
            os.environ.get("AUTOSTOCK_MAIN_MEMORY_ONLY"),
        )
        return True
    except Exception:
        logger.exception("[DATABASE OWNER] enable failed")
        return False


# ------------------------------------------------------------
# split_mode の 11 関数 (旧 _patch_split_mode の中身をそのまま)
# ------------------------------------------------------------


def is_data_collector_process() -> bool:
    return _is_database_process()


def external_data_collectors_enabled() -> bool:
    return True


def main_memory_only_enabled() -> bool:
    return _is_main_process() or _env_bool("AUTOSTOCK_MAIN_MEMORY_ONLY", False)


def yahoo_complement_owner() -> str:
    return "database"


def summary_save_owner() -> str:
    return "database"


def should_skip_data_collector_work_in_main() -> bool:
    return _is_main_process()


def should_run_yahoo_complement_in_this_process() -> bool:
    return _is_database_process()


def should_run_summary_save_in_this_process() -> bool:
    return _is_database_process()


def should_skip_yahoo_complement_in_main() -> bool:
    return not _is_database_process()


def should_skip_summary_save_in_this_process() -> bool:
    return not _is_database_process()


def mark_as_data_collector_process() -> None:
    os.environ["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
    os.environ["AUTOSTOCK_EXTERNAL_DATA_COLLECTORS"] = "1"
    os.environ["AUTOSTOCK_YAHOO_COMPLEMENT_OWNER"] = "database"
    os.environ["AUTOSTOCK_SUMMARY_SAVE_OWNER"] = "database"
    os.environ["SUMMARY_DB_WRITER_ROLE"] = "database"


# ------------------------------------------------------------
# runner_core / storage_bootstrap のゲート (旧 _patch_summary_runner_core /
# _patch_push_storage_bootstrap の中身をそのまま)
# ------------------------------------------------------------


def summary_save_enabled() -> bool:
    mode = str(os.environ.get("AUTOSTOCK_SUMMARY_SAVE_MODE", "")).strip().lower()
    if mode in {"disabled", "disable", "calculate_only", "calc_only", "no_save", "skip", "off"}:
        return False
    # Even if a stale env says enabled, main.py must remain non-writer.
    if _is_main_process() or os.environ.get("SUMMARY_SKIP_DB_SAVE_IN_MAIN", "").strip().lower() in _TRUE_VALUES:
        return False
    return _is_database_process()


def is_database_process_gate() -> bool:
    return _is_database_process()


def should_skip_push_storage_start_in_main() -> bool:
    return _is_main_process()


__all__ = [
    "VERSION",
    "enable_owner_override",
    "owner_override_enabled",
    "is_data_collector_process",
    "external_data_collectors_enabled",
    "main_memory_only_enabled",
    "yahoo_complement_owner",
    "summary_save_owner",
    "should_skip_data_collector_work_in_main",
    "should_run_yahoo_complement_in_this_process",
    "should_run_summary_save_in_this_process",
    "should_skip_yahoo_complement_in_main",
    "should_skip_summary_save_in_this_process",
    "mark_as_data_collector_process",
    "summary_save_enabled",
    "is_database_process_gate",
    "should_skip_push_storage_start_in_main",
]
