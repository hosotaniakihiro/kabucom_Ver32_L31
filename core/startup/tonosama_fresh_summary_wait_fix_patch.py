# ============================================================
# File   : core/startup/tonosama_fresh_summary_wait_fix_patch.py
# Version: v1-FIX-FRESH-SUMMARY-WAIT-LOOKUP
# ------------------------------------------------------------
# Purpose:
#   trading.entry_exit.tasks Ver2.3 added a fresh PUSH summary wait
#   before TONOSAMA, but it imports a non-existing module-level
#   get_push_merged_summary from core.global_context.context.
#
#   As a result the wait check returns latest=None rows=0 and TONOSAMA
#   skips every cycle even though GlobalContext has fresh merged summary.
#
#   This patch replaces tasks._latest_push_summary_age_sec with a robust
#   implementation that uses global_data/global_context methods and falls
#   back to get_push_summary / get_merged_summary.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_INSTALLING = False


def _get_gc() -> Any:
    try:
        import core.global_context.context as ctx
        return getattr(ctx, "global_data", None) or getattr(ctx, "global_context", None) or getattr(ctx, "GC", None)
    except Exception:
        return None


def _best_push_summary_df(tf: int = 1):
    try:
        import pandas as pd
        gc = _get_gc()
        if gc is not None:
            for name, args in (
                ("get_push_merged_summary", (tf,)),
                ("get_merged_summary", (tf, "push")),
                ("get_push_summary", (tf,)),
            ):
                fn = getattr(gc, name, None)
                if callable(fn):
                    try:
                        df = fn(*args)
                    except TypeError:
                        if name == "get_merged_summary":
                            df = fn(tf=tf, source="push")
                        else:
                            df = fn(tf)
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        return df

        # Last-resort legacy attributes used by old GlobalContext code.
        for attr in ("push_summary_1m", "push_summary_1min", "summary_1m", "merged_summary_1m"):
            if gc is not None and hasattr(gc, attr):
                df = getattr(gc, attr)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df
    except Exception:
        logger.debug("[TONOSAMA FRESH SUMMARY WAIT FIX] best df lookup failed", exc_info=True)
    try:
        import pandas as pd
        return pd.DataFrame()
    except Exception:
        return None


def _patched_latest_push_summary_age_sec() -> tuple[float | None, dt.datetime | None, int]:
    try:
        import pandas as pd
        df = _best_push_summary_df(1)
        rows = int(len(df)) if df is not None else 0
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            logger.warning("[TONOSAMA FRESH SUMMARY WAIT FIX] no push summary df rows=%s", rows)
            return None, None, rows

        time_cols = [c for c in ("datetime", "dt", "end_time", "time") if c in df.columns]
        if not time_cols:
            logger.warning("[TONOSAMA FRESH SUMMARY WAIT FIX] no time col rows=%s cols=%s", rows, list(df.columns)[:20])
            return None, None, rows

        s = pd.Series(dtype="datetime64[ns]")
        for col in time_cols:
            ss = pd.to_datetime(df[col], errors="coerce").dropna()
            if not ss.empty:
                s = ss
                break
        if s.empty:
            logger.warning("[TONOSAMA FRESH SUMMARY WAIT FIX] time parse empty rows=%s time_cols=%s", rows, time_cols)
            return None, None, rows

        latest = s.max().to_pydatetime().replace(tzinfo=None)
        age = (dt.datetime.now() - latest).total_seconds()
        logger.info("[TONOSAMA FRESH SUMMARY WAIT FIX] latest push summary latest=%s age=%.1fs rows=%s cols=%s", latest, age, rows, len(df.columns))
        return float(age), latest, rows
    except Exception:
        logger.exception("[TONOSAMA FRESH SUMMARY WAIT FIX] patched latest age failed")
        return None, None, 0


def _apply() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.entry_exit.tasks as tasks
    except Exception:
        logger.debug("[TONOSAMA FRESH SUMMARY WAIT FIX] tasks not ready", exc_info=True)
        return False

    try:
        cur = getattr(tasks, "_latest_push_summary_age_sec", None)
        if getattr(cur, "_tonosama_fresh_summary_wait_fix_v1", False):
            _INSTALLED = True
            return True
        _patched_latest_push_summary_age_sec._tonosama_fresh_summary_wait_fix_v1 = True  # type: ignore[attr-defined]
        _patched_latest_push_summary_age_sec._original = cur  # type: ignore[attr-defined]
        setattr(tasks, "_latest_push_summary_age_sec", _patched_latest_push_summary_age_sec)
        _INSTALLED = True
        logger.warning("[TONOSAMA FRESH SUMMARY WAIT FIX] installed v1 patched=trading.entry_exit.tasks._latest_push_summary_age_sec")
        return True
    except Exception:
        logger.exception("[TONOSAMA FRESH SUMMARY WAIT FIX] apply failed")
        return False


def install(retry: bool = True) -> bool:
    global _INSTALLING
    if _apply():
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _retry_loop() -> None:
            global _INSTALLING
            try:
                for _ in range(60):
                    if _apply():
                        return
                    time.sleep(0.2)
                logger.warning("[TONOSAMA FRESH SUMMARY WAIT FIX] retry exhausted")
            finally:
                _INSTALLING = False

        import threading
        threading.Thread(target=_retry_loop, name="tonosama-fresh-summary-wait-fix", daemon=True).start()
    return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA FRESH SUMMARY WAIT FIX] auto install failed")


__all__ = ["install"]
