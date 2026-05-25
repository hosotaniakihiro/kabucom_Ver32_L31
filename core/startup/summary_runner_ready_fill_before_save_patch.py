# ============================================================
# File   : core/startup/summary_runner_ready_fill_before_save_patch.py
# Version: V1-SUMMARY-READY-FILL-BEFORE-SAVE-AI
# ------------------------------------------------------------
# 目的:
#   runner_core の job_summary で、PUSH由来サマリーが
#   save/display/AI に渡る直前まで technical_ready=False のまま残る問題を補正する。
#
# 背景:
#   push_incremental_5min などの結果は rsi/macd/slope が入っていても、
#   technical_ready だけ False のまま出ることがある。
#   そのため SUMMARY AI 側が technical_not_ready と判定しやすい。
#
# 方針:
#   - runner_core._save_summary_if_owner をラップする。
#   - save の直前に add_short_history_indicators() を適用し、dfをin-place更新する。
#   - 同じdfオブジェクトが後続の AI / display に渡るため、AI前にも補正済みになる。
#   - DB保存可否やmain_entry_only制御は既存処理に任せる。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False


def _ready_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "technical_ready" not in df.columns:
            return 0
        return int(pd.Series(df["technical_ready"]).fillna(False).astype(bool).sum())
    except Exception:
        return 0


def _fill_ready_inplace(df: pd.DataFrame, *, interval: int, source: str) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    before_ready = _ready_count(df)
    before_cols = set(df.columns)

    try:
        from trading.summary.pipeline.indicator_short_history_patch import add_short_history_indicators
        filled = add_short_history_indicators(df, interval=interval)
    except Exception:
        logger.exception("[SUMMARY RUNNER READY FILL] add_short_history_indicators failed interval=%s source=%s", interval, source)
        return df

    if not isinstance(filled, pd.DataFrame) or filled.empty:
        return df

    try:
        # 元dfオブジェクトを保持したまま中身を差し替える。
        df.drop(df.index, inplace=True)
        for c in list(df.columns):
            if c not in filled.columns:
                try:
                    df.drop(columns=[c], inplace=True)
                except Exception:
                    pass
        for c in filled.columns:
            df[c] = filled[c].values
        df.index = filled.index
    except Exception:
        logger.exception("[SUMMARY RUNNER READY FILL] inplace replace failed interval=%s source=%s", interval, source)
        return filled

    after_ready = _ready_count(df)
    logger.warning(
        "[SUMMARY RUNNER READY FILL] applied interval=%s source=%s rows=%s technical_ready %s->%s added_cols=%s",
        interval,
        source,
        len(df),
        before_ready,
        after_ready,
        sorted([c for c in df.columns if c not in before_cols])[:20],
    )
    return df


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import scheduler_jobs.summary.runner_core as runner_core
    except Exception:
        logger.exception("[SUMMARY RUNNER READY FILL] runner_core import failed")
        return False

    try:
        old_save = getattr(runner_core, "_save_summary_if_owner", None)
        if not callable(old_save):
            logger.warning("[SUMMARY RUNNER READY FILL] _save_summary_if_owner not callable")
            return False
        if getattr(old_save, "_ready_fill_patch", False):
            _PATCHED = True
            return True

        def _save_summary_if_owner_patched(df: pd.DataFrame, interval: int, *, source: str) -> None:
            try:
                _fill_ready_inplace(df, interval=int(interval), source=str(source))
            except Exception:
                logger.exception("[SUMMARY RUNNER READY FILL] pre-save fill failed interval=%s source=%s", interval, source)
            return old_save(df, interval, source=source)

        _save_summary_if_owner_patched._ready_fill_patch = True  # type: ignore[attr-defined]
        _save_summary_if_owner_patched._original = old_save  # type: ignore[attr-defined]
        runner_core._save_summary_if_owner = _save_summary_if_owner_patched

        _PATCHED = True
        logger.warning("[SUMMARY RUNNER READY FILL] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY RUNNER READY FILL] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY RUNNER READY FILL] auto install failed")


__all__ = ["install"]
