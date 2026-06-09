# ============================================================
# File   : core/startup/main_skip_ranking_summary_bootstrap_patch.py
# Version: V1-MAIN-SKIP-RANKING-SUMMARY-BOOTSTRAP
# ------------------------------------------------------------
# Purpose:
#   main.py 起動時に、NAS上の ranking_snapshot / ranking_summary / summary DB を
#   同期的に直読み・UPSERTする ranking summary bootstrap を実行しない。
#
# Reason:
#   2026-06-09 のログで PUSH stack / PUSH summary fallback skip 後、
#     [RANKING SUMMARY BOOTSTRAP LOADER] loaded ranking snapshot rows=586
#   の直後に Python例外ではなく Windows 0xC0000006 でプロセス終了した。
#
# Policy:
#   - main.py は起動継続を最優先する。
#   - ranking summary のDB読み込み・保存・global cache更新は main_database.py 側へ寄せる。
#   - main.py側で従来動作へ戻したい場合だけ
#       AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_BOOTSTRAP=0
#     を明示する。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_BOOTSTRAP_RANKING_SUMMARY = None


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
        return True
    if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    return bool(default)


def _is_main_py_process() -> bool:
    try:
        return Path(sys.argv[0]).name.lower() == "main.py"
    except Exception:
        return False


def _should_skip() -> bool:
    if not _is_main_py_process():
        return False
    return _env_bool("AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_BOOTSTRAP", True)


def _empty_result(intervals: Iterable[int] = (1, 3, 5), db_path: str = ""):
    try:
        from trading.ranking.summary.bootstrap_config import RankingSummaryBootstrapResult
        ints = tuple(int(i) for i in intervals)
        return RankingSummaryBootstrapResult(
            ok=True,
            intervals={i: 0 for i in ints},
            db_path=db_path or "",
            snapshot_rows=0,
            message="skipped in main.py by AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_BOOTSTRAP=1",
        )
    except Exception:
        # startup_orchestrator は result の属性/dict get を安全に扱うため dict fallback でも成立する。
        ints = tuple(int(i) for i in intervals)
        return {
            "ok": True,
            "intervals": {i: 0 for i in ints},
            "db_path": db_path or "",
            "snapshot_rows": 0,
            "message": "skipped in main.py by AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_BOOTSTRAP=1",
        }


def _patched_bootstrap_ranking_summary_on_startup(*args, **kwargs):
    if _should_skip():
        intervals = kwargs.get("intervals", (1, 3, 5))
        db_path = str(kwargs.get("ranking_summary_db_path") or "")
        logger.warning(
            "[MAIN SKIP RANKING SUMMARY BOOTSTRAP] skipped bootstrap_ranking_summary_on_startup "
            "in main.py intervals=%s to avoid NAS SQLite ranking snapshot/summary read-write 0xC0000006. "
            "main_database.py handles ranking summary bootstrap. "
            "Set AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_BOOTSTRAP=0 to restore legacy behavior.",
            intervals,
        )
        return _empty_result(intervals=intervals, db_path=db_path)

    if callable(_ORIGINAL_BOOTSTRAP_RANKING_SUMMARY):
        return _ORIGINAL_BOOTSTRAP_RANKING_SUMMARY(*args, **kwargs)
    return _empty_result(kwargs.get("intervals", (1, 3, 5)), "")


def install() -> bool:
    global _INSTALLED
    global _ORIGINAL_BOOTSTRAP_RANKING_SUMMARY

    if _INSTALLED:
        return True

    try:
        os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_BOOTSTRAP", "1")

        import trading.ranking.summary.bootstrap as bootstrap_mod

        current = getattr(bootstrap_mod, "bootstrap_ranking_summary_on_startup", None)
        if getattr(current, "__name__", "") != "_patched_bootstrap_ranking_summary_on_startup":
            _ORIGINAL_BOOTSTRAP_RANKING_SUMMARY = current
            bootstrap_mod.bootstrap_ranking_summary_on_startup = _patched_bootstrap_ranking_summary_on_startup

        _INSTALLED = True
        logger.warning(
            "[MAIN SKIP RANKING SUMMARY BOOTSTRAP] installed enabled=%s main_py=%s",
            _should_skip(),
            _is_main_py_process(),
        )
        return True
    except Exception:
        logger.exception("[MAIN SKIP RANKING SUMMARY BOOTSTRAP] install failed")
        return False


__all__ = ["install"]
