# ============================================================
# File   : core/startup/ranking_legacy_save_patch.py
# Version: V1.0-FORCE-RANKING-LEGACY-SAVE
# ------------------------------------------------------------
# Purpose:
#   ranking_raw / ranking_snapshot に加えて、ランキング種別ごとの
#   legacy テーブル（例: 値上がり率_ALL, 売買高上位_TP 等）にも
#   保存されるようにする起動時パッチ。
#
# Notes:
#   trading.ranking.ranking_db_writer は既に legacy table 保存機能を持つ。
#   ただし呼び出し側が save_legacy=False の場合は保存されないため、
#   add_ranking_rows / add_ranking_rows_async の save_legacy を既定で True に補正する。
# ============================================================

from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y", "enable", "enabled"}


def install() -> bool:
    """
    ランキング保存時に legacy/category table 保存を有効化する。

    無効化したい場合だけ環境変数で指定:
      RANKING_LEGACY_SAVE_ENABLED=0
    """
    global _PATCHED
    if _PATCHED:
        return True

    if not _env_bool("RANKING_LEGACY_SAVE_ENABLED", True):
        logger.warning("[RANKING LEGACY SAVE PATCH] skipped by RANKING_LEGACY_SAVE_ENABLED=0")
        return False

    try:
        import trading.ranking.ranking_db_writer as mod
    except Exception:
        logger.exception("[RANKING LEGACY SAVE PATCH] import ranking_db_writer failed")
        return False

    try:
        cls = mod.RankingDBWriter
        original_method = getattr(cls, "add_ranking_rows")

        if getattr(original_method, "_legacy_save_forced", False):
            _PATCHED = True
            return True

        @wraps(original_method)
        def patched_add_ranking_rows(self: Any, *args: Any, **kwargs: Any):
            # 呼び出し側が False を渡しても、ランキング種別ごとのテーブル保存を優先する。
            kwargs["save_legacy"] = True
            return original_method(self, *args, **kwargs)

        patched_add_ranking_rows._legacy_save_forced = True  # type: ignore[attr-defined]
        setattr(cls, "add_ranking_rows", patched_add_ranking_rows)
    except Exception:
        logger.exception("[RANKING LEGACY SAVE PATCH] patch RankingDBWriter.add_ranking_rows failed")
        return False

    try:
        original_func = getattr(mod, "add_ranking_rows_async", None)
        if original_func is not None and not getattr(original_func, "_legacy_save_forced", False):

            @wraps(original_func)
            def patched_add_ranking_rows_async(*args: Any, **kwargs: Any):
                kwargs["save_legacy"] = True
                return original_func(*args, **kwargs)

            patched_add_ranking_rows_async._legacy_save_forced = True  # type: ignore[attr-defined]
            setattr(mod, "add_ranking_rows_async", patched_add_ranking_rows_async)
    except Exception:
        logger.exception("[RANKING LEGACY SAVE PATCH] patch add_ranking_rows_async failed")
        return False

    _PATCHED = True
    logger.warning(
        "[RANKING LEGACY SAVE PATCH] installed: ranking rows are also saved into type/market legacy tables"
    )
    return True


__all__ = ["install"]
