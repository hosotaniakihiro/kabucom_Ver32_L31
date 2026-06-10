# ============================================================
# File   : core/startup/summary_seed_restore_main_fast_patch.py
# Version: V1-MAIN-FAST-NO-PREV-SCAN
# ------------------------------------------------------------
# 目的:
#   main.py 起動中/昼休み中に summary_db_seed_restore_patch が
#   前日 summary DB を COUNT して数分間 I/O を使い続ける問題を抑制する。
#
# 方針:
#   - main.py では前日DBを push履歴に注入しない運用なので、前日候補探索も省略する
#   - main.py の seed rows は軽量化し、main_database.py 側の正式保存を優先する
# ============================================================
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIG_RESOLVE_PREV = None
_ORIG_ENV_INT = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _is_main_py_context() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in argv:
            return False
        if any(x in argv for x in (
            "summary_database_runner.py",
            "yahoo_complement_runner.py",
            "ranking_collector_runner.py",
            "push_receiver_runner.py",
            "data_collectors_runner.py",
        )):
            return False
        if any(os.getenv(k) == "1" for k in (
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
        )):
            return False
        return "main.py" in argv
    except Exception:
        return False


def install() -> bool:
    global _PATCHED, _ORIG_RESOLVE_PREV, _ORIG_ENV_INT
    if _PATCHED:
        return True
    try:
        import core.startup.summary_db_seed_restore_patch as sr

        _ORIG_RESOLVE_PREV = getattr(sr, "_resolve_previous_seed_db_path", None)
        _ORIG_ENV_INT = getattr(sr, "_env_int", None)

        def _patched_env_int(name: str, default: int) -> int:
            # main.py では seed restore を軽量化する。main_database.py / force 系は従来通り。
            try:
                if _is_main_py_context() and _env_bool("SUMMARY_DB_SEED_RESTORE_MAIN_FAST_DEFAULTS", True):
                    if name == "SUMMARY_DB_SEED_RESTORE_BARS_PER_SYMBOL":
                        raw = os.getenv(name)
                        return int(float(raw)) if raw not in (None, "") else int(os.getenv("SUMMARY_DB_SEED_RESTORE_MAIN_BARS_PER_SYMBOL", "30"))
                    if name == "SUMMARY_DB_SEED_RESTORE_MAX_ROWS_PER_TF":
                        raw = os.getenv(name)
                        return int(float(raw)) if raw not in (None, "") else int(os.getenv("SUMMARY_DB_SEED_RESTORE_MAIN_MAX_ROWS_PER_TF", "9000"))
                    if name == "SUMMARY_DB_SEED_RESTORE_PREV_LOOKBACK_DAYS":
                        raw = os.getenv(name)
                        return int(float(raw)) if raw not in (None, "") else int(os.getenv("SUMMARY_DB_SEED_RESTORE_MAIN_PREV_LOOKBACK_DAYS", "1"))
            except Exception:
                pass
            if callable(_ORIG_ENV_INT):
                return int(_ORIG_ENV_INT(name, default))
            try:
                raw = os.getenv(name)
                return int(float(raw)) if raw not in (None, "") else int(default)
            except Exception:
                return int(default)

        def _patched_resolve_previous_seed_db_path(current_path: Path) -> Optional[Path]:
            try:
                if _is_main_py_context() and _env_bool("SUMMARY_DB_SEED_RESTORE_SKIP_PREV_SCAN_IN_MAIN", True):
                    if not _env_bool("SUMMARY_DB_SEED_RESTORE_ALLOW_PREV_AS_PUSH_HISTORY", False):
                        logger.warning(
                            "[SUMMARY SEED MAIN FAST] skip previous summary DB scan in main.py current=%s allow_prev_as_push_history=0",
                            current_path,
                        )
                        return None
            except Exception:
                pass
            if callable(_ORIG_RESOLVE_PREV):
                return _ORIG_RESOLVE_PREV(current_path)
            return None

        sr._env_int = _patched_env_int
        sr._resolve_previous_seed_db_path = _patched_resolve_previous_seed_db_path
        _PATCHED = True
        logger.warning(
            "[SUMMARY SEED MAIN FAST] installed v1 main_fast_defaults=%s skip_prev_scan=%s",
            _env_bool("SUMMARY_DB_SEED_RESTORE_MAIN_FAST_DEFAULTS", True),
            _env_bool("SUMMARY_DB_SEED_RESTORE_SKIP_PREV_SCAN_IN_MAIN", True),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY SEED MAIN FAST] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY SEED MAIN FAST] auto install failed")


__all__ = ["install"]
