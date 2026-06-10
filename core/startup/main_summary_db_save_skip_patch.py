# ============================================================
# File   : core/startup/main_summary_db_save_skip_patch.py
# Version: V1-MAIN-SUMMARY-DB-WRITE-SKIP
# ------------------------------------------------------------
# 目的:
#   main.py は entry/exit/表示側であり、summary DB の正式保存は
#   main_database.py / DB owner 側へ寄せる。
#   main.py から save_summary_safe / save_merged_summary / upsert 系が走ると、
#   stock_summary_1min の database is locked を誘発するため no-op 化する。
# ============================================================
from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _is_main_py_context() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in argv:
            return False
        if any(x in argv for x in (
            "db_prepare_runner.py",
            "ranking_collector_runner.py",
            "push_receiver_runner.py",
            "yahoo_complement_runner.py",
            "summary_database_runner.py",
            "data_collectors_runner.py",
        )):
            return False
        if any(os.getenv(k) == "1" for k in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
        )):
            return False
        return "main.py" in argv
    except Exception:
        return False


def _skip_enabled() -> bool:
    if not _is_main_py_context():
        return False
    if _env_bool("AUTOSTOCK_MAIN_ALLOW_SUMMARY_DB_SAVE", False):
        return False
    return _env_bool("AUTOSTOCK_MAIN_SKIP_SUMMARY_DB_SAVE", True)


def _rows(df: Any) -> int:
    try:
        return int(len(df))
    except Exception:
        return 0


def _patch_safe_io() -> bool:
    try:
        import scheduler_jobs.summary.safe_io as safe_io  # type: ignore

        old = getattr(safe_io, "save_summary_safe", None)
        if not callable(old):
            return False
        if getattr(old, "_main_summary_db_save_skip_v1", False):
            return True

        def _patched_save_summary_safe(df, interval: int, source: str) -> bool:
            if _skip_enabled():
                logger.warning(
                    "[MAIN SUMMARY DB SAVE SKIP] save_summary_safe skipped in main.py source=%s interval=%s rows=%s. main_database.py owns summary DB writes.",
                    source,
                    interval,
                    _rows(df),
                )
                return False
            return old(df, interval, source)

        _patched_save_summary_safe._main_summary_db_save_skip_v1 = True  # type: ignore[attr-defined]
        _patched_save_summary_safe._original = old  # type: ignore[attr-defined]
        safe_io.save_summary_safe = _patched_save_summary_safe
        return True
    except Exception:
        logger.exception("[MAIN SUMMARY DB SAVE SKIP] safe_io patch failed")
        return False


def _patch_cache_writer() -> bool:
    try:
        import scheduler_jobs.summary.cache_writer as cache_writer  # type: ignore

        old = getattr(cache_writer, "save_merged_summary", None)
        if not callable(old):
            return False
        if getattr(old, "_main_summary_db_save_skip_v1", False):
            return True

        def _patched_save_merged_summary(df, interval: int, *args, **kwargs):
            if _skip_enabled():
                source = kwargs.get("source", None)
                logger.warning(
                    "[MAIN SUMMARY DB SAVE SKIP] save_merged_summary skipped in main.py source=%s interval=%s rows=%s. main_database.py owns summary DB writes.",
                    source,
                    interval,
                    _rows(df),
                )
                return False
            return old(df, interval, *args, **kwargs)

        _patched_save_merged_summary._main_summary_db_save_skip_v1 = True  # type: ignore[attr-defined]
        _patched_save_merged_summary._original = old  # type: ignore[attr-defined]
        cache_writer.save_merged_summary = _patched_save_merged_summary
        return True
    except Exception:
        logger.exception("[MAIN SUMMARY DB SAVE SKIP] cache_writer patch failed")
        return False


def _patch_recovery_persistence() -> bool:
    ok_any = False
    try:
        import trading.summary.recovery.persistence as persistence  # type: ignore
        for name in ("upsert_summary_df", "save_summary_bulk"):
            old = getattr(persistence, name, None)
            if not callable(old) or getattr(old, "_main_summary_db_save_skip_v1", False):
                continue

            def _make(fn, nm):
                def _patched(*args, **kwargs):
                    if _skip_enabled():
                        df = args[0] if args else kwargs.get("df")
                        interval = kwargs.get("interval", kwargs.get("tf", "?"))
                        logger.warning(
                            "[MAIN SUMMARY DB SAVE SKIP] recovery.%s skipped in main.py interval=%s rows=%s. main_database.py owns summary DB writes.",
                            nm,
                            interval,
                            _rows(df),
                        )
                        return False
                    return fn(*args, **kwargs)
                _patched._main_summary_db_save_skip_v1 = True  # type: ignore[attr-defined]
                _patched._original = fn  # type: ignore[attr-defined]
                return _patched

            setattr(persistence, name, _make(old, name))
            ok_any = True
    except Exception:
        logger.debug("[MAIN SUMMARY DB SAVE SKIP] recovery persistence patch skipped", exc_info=True)
    return ok_any


def install() -> bool:
    global _INSTALLED
    os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_SUMMARY_DB_SAVE", "1")
    ok = bool(_patch_safe_io())
    ok = bool(_patch_cache_writer()) or ok
    ok = bool(_patch_recovery_persistence()) or ok
    _INSTALLED = True
    logger.warning(
        "[MAIN SUMMARY DB SAVE SKIP] installed v1 ok=%s main_py=%s skip_enabled=%s allow_env=%s",
        ok,
        _is_main_py_context(),
        _skip_enabled(),
        os.getenv("AUTOSTOCK_MAIN_ALLOW_SUMMARY_DB_SAVE"),
    )
    return ok


try:
    install()
except Exception:
    logger.exception("[MAIN SUMMARY DB SAVE SKIP] auto install failed")

__all__ = ["install"]
