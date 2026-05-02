# ============================================================
# File   : trading/summary/recovery/persistence_pkg/upsert_runner.py
# Ver    : PRODUCTION-STABLE-REV9.0-UPSERT-RUNNER
# ------------------------------------------------------------
# 【概要】
#   upsert_summary_df runner
# ============================================================

from __future__ import annotations

import logging
from typing import Callable, Optional

import pandas as pd

from .db_normalizer import finalize_for_upsert
from .backend_resolver import (
    execute_upsert_backend,
    execute_upsert_backend_name,
    save_summary_bulk_backend,
    save_summary_bulk_backend_name,
)

logger = logging.getLogger(__name__)


def call_backend_with_fallbacks(fn: Callable, backend_name: str, df: pd.DataFrame, interval: int) -> bool:
    attempts = [
        ("df, interval", lambda: fn(df, int(interval))),
        ("interval, df", lambda: fn(int(interval), df)),
        ("df kw interval", lambda: fn(df=df, interval=int(interval))),
        ("interval kw df", lambda: fn(interval=int(interval), df=df)),
    ]

    last_exc: Exception | None = None
    for label, caller in attempts:
        try:
            caller()
            logger.info(
                "[summary.recovery.persistence] backend call ok backend=%s signature=%s interval=%s rows=%s",
                backend_name,
                label,
                interval,
                len(df),
            )
            return True
        except TypeError as e:
            last_exc = e
            logger.debug(
                "[summary.recovery.persistence] backend signature mismatch backend=%s signature=%s",
                backend_name,
                label,
                exc_info=True,
            )
            continue
        except Exception as e:
            last_exc = e
            logger.exception(
                "[summary.recovery.persistence] backend call failed backend=%s signature=%s interval=%s rows=%s",
                backend_name,
                label,
                interval,
                len(df),
            )
            continue

    if last_exc is not None:
        logger.warning(
            "[summary.recovery.persistence] backend exhausted backend=%s interval=%s rows=%s last_exc=%r",
            backend_name,
            interval,
            len(df),
            last_exc,
        )
    return False


def upsert_summary_df(df: pd.DataFrame, interval: int) -> None:
    out = finalize_for_upsert(df, interval=interval)
    if out.empty:
        logger.info("[summary.recovery.persistence] upsert skipped empty interval=%s", interval)
        return

    try:
        backends: list[tuple[Optional[Callable], Optional[str]]] = [
            (execute_upsert_backend, execute_upsert_backend_name),
            (save_summary_bulk_backend, save_summary_bulk_backend_name),
        ]

        for fn, name in backends:
            if not callable(fn) or not name:
                continue
            if call_backend_with_fallbacks(fn, name, out, int(interval)):
                logger.info(
                    "[summary.recovery.persistence] upsert done backend=%s interval=%s rows=%s",
                    name,
                    interval,
                    len(out),
                )
                return

        logger.warning(
            "[summary.recovery.persistence] upsert backend not found interval=%s rows=%s execute_upsert=%s save_summary_bulk=%s",
            interval,
            len(out),
            execute_upsert_backend_name,
            save_summary_bulk_backend_name,
        )
    except Exception:
        logger.exception("[summary.recovery.persistence] upsert failed interval=%s", interval)


__all__ = [
    "call_backend_with_fallbacks",
    "upsert_summary_df",
]