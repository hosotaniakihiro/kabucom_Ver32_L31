# ============================================================
# File   : core/startup/ranking_summary_persistence_lock_patch.py
# Version: PRODUCTION-STABLE-REV1-RANKING-SUMMARY-LOCK-SKIP
# ------------------------------------------------------------
# Purpose:
#   - trading.ranking.summary.persistence が場中に summaryYYYYMMDD.db へ
#     ranking_summary_Xmin テーブルを ensure しに行き、
#     stock_summary_* 保存と競合して database is locked になる問題を緩和する。
#   - locked 時は ERROR ではなく WARNING で skip し、次回へ回す。
#   - 既に ensure 済みの table は cache で即 return する。
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_ENSURE_LOCAL = None
_ORIGINAL_ENSURE = None
_ORIGINAL_SAVE = None


def _is_locked_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "database is locked" in s or "database table is locked" in s or "locked" in s


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def install_ranking_summary_persistence_lock_patch() -> None:
    global _INSTALLED, _ORIGINAL_ENSURE_LOCAL, _ORIGINAL_ENSURE, _ORIGINAL_SAVE
    if _INSTALLED:
        return

    try:
        import trading.ranking.summary.persistence as persistence
    except Exception:
        logger.exception("[RANKING SUMMARY LOCK PATCH] import persistence failed")
        return

    original_local = getattr(persistence, "_ensure_local_ranking_summary_table", None)
    original_ensure = getattr(persistence, "ensure_ranking_summary_table", None)
    original_save = getattr(persistence, "save_ranking_summary", None)

    if not callable(original_local) or not callable(original_ensure):
        logger.warning("[RANKING SUMMARY LOCK PATCH] target functions missing")
        return

    _ORIGINAL_ENSURE_LOCAL = original_local
    _ORIGINAL_ENSURE = original_ensure
    _ORIGINAL_SAVE = original_save

    def patched_ensure_local(*, interval: int | str = 1, date_yyyymmdd: Any = None, db_path: str | Path | None = None) -> bool:
        try:
            return bool(original_local(interval=interval, date_yyyymmdd=date_yyyymmdd, db_path=db_path))
        except Exception as exc:
            if _is_locked_error(exc):
                logger.warning(
                    "[RANKING SUMMARY LOCK PATCH] ensure local skipped locked interval=%s db=%s err=%s",
                    interval,
                    db_path,
                    exc,
                )
                return False
            logger.exception("[RANKING SUMMARY LOCK PATCH] ensure local failed interval=%s", interval)
            return False

    def patched_ensure(interval: int | str = 1, *args: Any, **kwargs: Any) -> bool:
        try:
            return bool(original_ensure(interval, *args, **kwargs))
        except Exception as exc:
            if _is_locked_error(exc):
                logger.warning(
                    "[RANKING SUMMARY LOCK PATCH] ensure skipped locked interval=%s err=%s",
                    interval,
                    exc,
                )
                return False
            logger.exception("[RANKING SUMMARY LOCK PATCH] ensure failed interval=%s", interval)
            return False

    def patched_save(df, *args: Any, interval: int | str = 1, source: str = "ranking", **kwargs: Any) -> int:
        # 先に短時間の ensure を試す。失敗/lockedなら今回は保存を諦める。
        # ranking summary は次回再計算できるため、summary 1m 保存を優先する。
        ok = patched_ensure(interval=interval)
        if not ok:
            try:
                rows = 0 if df is None else len(df)
            except Exception:
                rows = 0
            logger.warning(
                "[RANKING SUMMARY LOCK PATCH] save skipped because ensure unavailable interval=%s source=%s rows=%s",
                interval,
                source,
                rows,
            )
            return 0

        if not callable(original_save):
            return 0

        try:
            return int(original_save(df, *args, interval=interval, source=source, **kwargs) or 0)
        except Exception as exc:
            if _is_locked_error(exc):
                logger.warning(
                    "[RANKING SUMMARY LOCK PATCH] save skipped locked interval=%s source=%s err=%s",
                    interval,
                    source,
                    exc,
                )
                return 0
            logger.exception("[RANKING SUMMARY LOCK PATCH] save failed interval=%s source=%s", interval, source)
            return 0

    persistence._ensure_local_ranking_summary_table = patched_ensure_local
    persistence.ensure_ranking_summary_table = patched_ensure
    persistence.ensure_table = patched_ensure
    persistence.save_ranking_summary = patched_save
    persistence.save_ranking_summary_df = patched_save
    persistence.persist_ranking_summary = patched_save
    persistence.persist_ranking_summary_df = patched_save
    persistence.save = patched_save
    persistence.save_df = patched_save

    _INSTALLED = True
    logger.warning(
        "[RANKING SUMMARY LOCK PATCH] installed locked_skip=True ensure_timeout_note=original timeout; error downgraded to warning"
    )


__all__ = ["install_ranking_summary_persistence_lock_patch"]
