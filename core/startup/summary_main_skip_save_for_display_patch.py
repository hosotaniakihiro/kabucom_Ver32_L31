# ============================================================
# File   : core/startup/summary_main_skip_save_for_display_patch.py
# Version: V1-MAIN-1M-SKIP-BLOCKING-SAVE-BEFORE-DISPLAY
# ------------------------------------------------------------
# 目的:
#   main.py(entry_only) 側で1分足PUSHサマリーは計算できているのに、
#   表示/Discordまで進まない問題を防ぐ。
#
# 背景:
#   runner_core.job_summary() の順番は以下。
#     1. 計算
#     2. _save_summary_if_owner()
#     3. AI entry
#     4. display/Discord
#
#   main.py は DB保存ownerではなく、DB保存は main_database.py 側が担当する。
#   しかし main.py でも save_summary_safe() がcache保存・履歴取得などで重くなり、
#   1分足の表示へ到達する前に数十秒〜数分止まることがある。
#
# 方針:
#   - main.py / entry_only / 非database process のときだけ有効
#   - PUSH 1分足だけ _save_summary_if_owner() をスキップ
#   - 3分/5分、database runner側は従来通り保存
#   - 計算済みDFは summary_controller 側でglobal_dataへ反映済みなので表示・AIに使える
#
# ENV:
#   SUMMARY_MAIN_SKIP_1M_SAVE_BEFORE_DISPLAY=1  既定ON
#   SUMMARY_MAIN_SKIP_SAVE_BEFORE_DISPLAY_INTERVALS=1
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_SAVE = None


def _env_flag(name: str) -> str:
    try:
        return str(os.getenv(name, "")).strip().lower()
    except Exception:
        return ""


def _env_bool(name: str, default: bool = True) -> bool:
    raw = _env_flag(name)
    if raw in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
        return True
    if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return bool(default)


def _is_database_process() -> bool:
    return any(
        _env_bool(name, False)
        for name in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
        )
    )


def _is_main_entry_only() -> bool:
    role = _env_flag("SUMMARY_DB_WRITER_ROLE")
    return (
        _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False)
        or _env_bool("SUMMARY_SKIP_DB_SAVE_IN_MAIN", False)
        or role == "entry_only"
    )


def _skip_intervals() -> set[int]:
    raw = os.getenv("SUMMARY_MAIN_SKIP_SAVE_BEFORE_DISPLAY_INTERVALS", "1")
    out: set[int] = set()
    for x in str(raw).replace(";", ",").split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.add(int(float(x)))
        except Exception:
            pass
    return out or {1}


def _should_skip(interval: int, source: str) -> bool:
    if not _env_bool("SUMMARY_MAIN_SKIP_1M_SAVE_BEFORE_DISPLAY", True):
        return False
    if str(source).lower() not in {"push", "summary"}:
        return False
    if int(interval) not in _skip_intervals():
        return False
    if _is_database_process():
        return False
    if not _is_main_entry_only():
        return False
    return True


def install() -> bool:
    global _PATCHED, _ORIGINAL_SAVE
    if _PATCHED:
        return True

    try:
        import scheduler_jobs.summary.runner_core as rc

        cur = getattr(rc, "_save_summary_if_owner", None)
        if not callable(cur):
            logger.warning("[SUMMARY MAIN SKIP SAVE DISPLAY PATCH] target missing")
            return False
        if getattr(cur, "_summary_main_skip_save_display_patch", False):
            _PATCHED = True
            return True

        _ORIGINAL_SAVE = cur

        def _patched_save_summary_if_owner(df, interval: int, *, source: str):
            try:
                iv = int(interval)
            except Exception:
                iv = interval

            if _should_skip(int(iv), str(source)):
                try:
                    rows = len(df) if hasattr(df, "__len__") else 0
                except Exception:
                    rows = 0
                logger.warning(
                    "[SUMMARY MAIN SKIP SAVE DISPLAY PATCH] skip blocking save before display interval=%s source=%s rows=%s role=%s main_entry_only=%s env_skip=%s reason=main_process_display_first",
                    iv,
                    source,
                    rows,
                    os.getenv("SUMMARY_DB_WRITER_ROLE", ""),
                    os.getenv("SUMMARY_MAIN_ENTRY_ONLY", ""),
                    os.getenv("SUMMARY_SKIP_DB_SAVE_IN_MAIN", ""),
                )
                return None

            return _ORIGINAL_SAVE(df, int(iv), source=source)

        _patched_save_summary_if_owner._summary_main_skip_save_display_patch = True  # type: ignore[attr-defined]
        _patched_save_summary_if_owner._original = cur  # type: ignore[attr-defined]
        rc._save_summary_if_owner = _patched_save_summary_if_owner
        _PATCHED = True
        logger.warning(
            "[SUMMARY MAIN SKIP SAVE DISPLAY PATCH] installed enabled=%s intervals=%s",
            _env_bool("SUMMARY_MAIN_SKIP_1M_SAVE_BEFORE_DISPLAY", True),
            sorted(_skip_intervals()),
        )
        return True

    except Exception:
        logger.exception("[SUMMARY MAIN SKIP SAVE DISPLAY PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN SKIP SAVE DISPLAY PATCH] auto install failed")


__all__ = ["install"]
