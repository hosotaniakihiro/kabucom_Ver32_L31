# ============================================================
# File   : core/startup/main_skip_push_db_restore_patch.py
# Version: V4-MAIN-SKIP-PUSH-DB-RESTORE-MEMORY-SUMMARY-FALLBACK
# ------------------------------------------------------------
# Purpose:
#   main.py 起動時に、NAS上の pushYYYYMMDD.db を同期的に直読み復元しない。
#   さらに main.py 側の PUSH storage writer / PUSH symbol bridge / PUSH stream
#   起動を既定でスキップし、0xC0000006 の発生面を最小化する。
#
#   V4:
#   - PUSH stack を止めた状態で PUSH summary runner が空を返した場合、
#     main.py では DB/cache fallback を読まない方針は維持する。
#   - ただし空 DataFrame 固定だと後場再起動直後に summary/AI が空になるため、
#     GlobalContext のメモリ上 summary_history_cache / merged_summary / push_summary_cache
#     からだけ復元する memory-only fallback を追加する。
#
# Policy:
#   - main.py は起動継続を最優先する。
#   - NAS DB作成 / PUSH保存 / PUSH登録 / PUSH受信 / PUSH DB fallback は main_database.py 側に寄せる。
#   - main.py はメモリ上に既にある summary だけを利用する。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_BOOTSTRAP_PUSH = None
_ORIGINAL_START_PUSH_STACK = None
_ORIGINAL_START_PUSH_STORAGE_SAFE = None
_ORIGINAL_START_PUSH_STREAM_EARLY_SAFE = None
_ORIGINAL_START_PUSH_STREAM_FALLBACK_SAFE = None
_ORIGINAL_FALLBACK_PUSH_SUMMARY_DF = None
_MEMORY_FALLBACK_LOGGED = set()


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


def _should_skip_db_restore() -> bool:
    if not _is_main_py_process():
        return False
    return _env_bool("AUTOSTOCK_MAIN_SKIP_PUSH_DB_RESTORE", True)


def _should_skip_push_stack() -> bool:
    if not _is_main_py_process():
        return False
    return _env_bool("AUTOSTOCK_MAIN_SKIP_PUSH_STACK", True)


def _should_skip_push_summary_fallback() -> bool:
    if not _is_main_py_process():
        return False
    return _env_bool("AUTOSTOCK_MAIN_SKIP_PUSH_SUMMARY_FALLBACK", True)


def _set_empty_push_df(reason: str, push_dir=None) -> None:
    try:
        from global_state import global_data
    except Exception:
        return

    empty = pd.DataFrame()
    try:
        global_data.push_df = empty
    except Exception:
        pass
    try:
        global_data.set_push_df(empty)
    except Exception:
        pass
    try:
        global_data.push_bootstrap_skipped = True
        global_data.push_bootstrap_skip_reason = reason
        global_data.push_bootstrap_skipped_by_patch = True
        global_data.push_bootstrap_push_dir = str(push_dir or "")
        global_data.push_bootstrap_rows = 0
        global_data.push_bootstrap_raw_rows = 0
    except Exception:
        pass


def _mark_push_stack_skipped(reason: str) -> None:
    try:
        from global_state import global_data
    except Exception:
        return

    try:
        global_data.push_writer_running = False
        global_data.push_storage_running = False
        global_data.push_storage_skipped_external = True
        global_data.push_stream_running = False
        global_data.push_stream_memory_only = False
        global_data.push_stream_skipped_external = True
        global_data.push_symbol_bridge_installed = False
        global_data.push_symbol_bridge_count = 0
        global_data.push_symbol_bridge_symbols = []
        global_data.push_collection_skipped_external = True
        global_data.push_collection_memory_merge_mode = False
        global_data.main_push_stack_skipped = True
        global_data.main_push_stack_skip_reason = reason
    except Exception:
        pass


def _patched_bootstrap_push(push_dir):
    if _should_skip_db_restore():
        _set_empty_push_df("main_skip_push_db_restore", push_dir=push_dir)
        logger.warning(
            "[MAIN SKIP PUSH DB RESTORE] skipped core.startup.push_bootstrap.bootstrap_push "
            "in main.py to avoid NAS SQLite direct-read 0xC0000006 push_dir=%s. "
            "Set AUTOSTOCK_MAIN_SKIP_PUSH_DB_RESTORE=0 to restore legacy behavior.",
            push_dir,
        )
        return None

    if callable(_ORIGINAL_BOOTSTRAP_PUSH):
        return _ORIGINAL_BOOTSTRAP_PUSH(push_dir)
    return None


def _patched_start_push_stack_before_scheduler():
    if _should_skip_push_stack():
        _set_empty_push_df("main_skip_push_stack", push_dir=None)
        _mark_push_stack_skipped("main_skip_push_stack")
        logger.warning(
            "[MAIN SKIP PUSH STACK] skipped full PUSH startup stack in main.py "
            "to avoid NAS SQLite writer/reader 0xC0000006. "
            "main_database.py handles PUSH storage/register/receive. "
            "Set AUTOSTOCK_MAIN_SKIP_PUSH_STACK=0 to restore legacy behavior."
        )
        return []

    if callable(_ORIGINAL_START_PUSH_STACK):
        return _ORIGINAL_START_PUSH_STACK()
    return []


def _patched_start_push_storage_safe():
    if _should_skip_push_stack():
        _mark_push_stack_skipped("main_skip_push_storage")
        logger.warning(
            "[MAIN SKIP PUSH STACK] skipped push storage writer in main.py. "
            "main_database.py handles push DB writer."
        )
        return None
    if callable(_ORIGINAL_START_PUSH_STORAGE_SAFE):
        return _ORIGINAL_START_PUSH_STORAGE_SAFE()
    return None


def _patched_start_push_stream_early_safe():
    if _should_skip_push_stack():
        _mark_push_stack_skipped("main_skip_push_stream_early")
        logger.warning(
            "[MAIN SKIP PUSH STACK] skipped early push stream in main.py. "
            "main_database.py handles PUSH receive/register."
        )
        return False
    if callable(_ORIGINAL_START_PUSH_STREAM_EARLY_SAFE):
        return _ORIGINAL_START_PUSH_STREAM_EARLY_SAFE()
    return False


def _patched_start_push_stream_fallback_safe():
    if _should_skip_push_stack():
        _mark_push_stack_skipped("main_skip_push_stream_fallback")
        logger.warning(
            "[MAIN SKIP PUSH STACK] skipped fallback push stream in main.py. "
            "main_database.py handles PUSH receive/register."
        )
        return False
    if callable(_ORIGINAL_START_PUSH_STREAM_FALLBACK_SAFE):
        return _ORIGINAL_START_PUSH_STREAM_FALLBACK_SAFE()
    return False


def _normalize_interval(interval):
    try:
        if str(interval).lower() in {"1", "1m", "1min"}:
            return 1
        if str(interval).lower() in {"3", "3m", "3min"}:
            return 3
        if str(interval).lower() in {"5", "5m", "5min"}:
            return 5
        return int(interval)
    except Exception:
        return interval


def _memory_push_summary_fallback(interval, now=None) -> pd.DataFrame:
    """Return only in-process memory summary; never read NAS DB/cache here."""
    if not _env_bool("AUTOSTOCK_MAIN_PUSH_SUMMARY_MEMORY_FALLBACK", True):
        return pd.DataFrame()
    tf = _normalize_interval(interval)
    candidates: list[tuple[str, pd.DataFrame]] = []

    # 1) global_context summary history (full rows, best for indicator/display rebuild)
    try:
        from core.global_context.context import global_context
        if hasattr(global_context, "get_summary_history"):
            df = global_context.get_summary_history(tf, source="push")
            if isinstance(df, pd.DataFrame) and not df.empty:
                candidates.append(("summary_history", df))
        if hasattr(global_context, "get_merged_summary"):
            df = global_context.get_merged_summary(tf, source="push")
            if isinstance(df, pd.DataFrame) and not df.empty:
                candidates.append(("merged_summary", df))
        cache = getattr(global_context, "push_summary_cache", None)
        if isinstance(cache, dict):
            df = cache.get(tf)
            if isinstance(df, pd.DataFrame) and not df.empty:
                candidates.append(("push_summary_cache", df))
    except Exception:
        logger.debug("[MAIN SKIP PUSH SUMMARY FALLBACK] memory global_context fallback failed", exc_info=True)

    # 2) global_state/global_data compatibility
    try:
        from global_state import global_data
        for attr in (f"merged_summary_{tf}", f"summary_{tf}", f"push_summary_{tf}"):
            df = getattr(global_data, attr, None)
            if isinstance(df, pd.DataFrame) and not df.empty:
                candidates.append((f"global_data.{attr}", df))
        getter = getattr(global_data, "get_push_summary", None)
        if callable(getter):
            df = getter(tf)
            if isinstance(df, pd.DataFrame) and not df.empty:
                candidates.append(("global_data.get_push_summary", df))
    except Exception:
        logger.debug("[MAIN SKIP PUSH SUMMARY FALLBACK] memory global_data fallback failed", exc_info=True)

    for source, df in candidates:
        try:
            out = df.copy()
            if "datetime" in out.columns:
                out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
                # Keep only today rows when possible; avoid old seed leaking into PM session.
                today = pd.Timestamp.now().normalize()
                mask_today = out["datetime"].dt.normalize().eq(today)
                if mask_today.any():
                    out = out.loc[mask_today].copy()
            if out.empty:
                continue
            key = (tf, source)
            if key not in _MEMORY_FALLBACK_LOGGED:
                latest = None
                if "datetime" in out.columns:
                    latest = pd.to_datetime(out["datetime"], errors="coerce").max()
                logger.warning(
                    "[MAIN SKIP PUSH SUMMARY FALLBACK] memory fallback interval=%s source=%s rows=%s latest=%s now=%s",
                    tf,
                    source,
                    len(out),
                    latest,
                    now,
                )
                _MEMORY_FALLBACK_LOGGED.add(key)
            return out.reset_index(drop=True)
        except Exception:
            logger.debug("[MAIN SKIP PUSH SUMMARY FALLBACK] candidate failed source=%s", source, exc_info=True)

    return pd.DataFrame()


def _patched_fallback_push_summary_df(interval, now=None, *args, **kwargs):
    if _should_skip_push_summary_fallback():
        df_mem = _memory_push_summary_fallback(interval, now=now)
        if isinstance(df_mem, pd.DataFrame) and not df_mem.empty:
            return df_mem
        logger.warning(
            "[MAIN SKIP PUSH SUMMARY FALLBACK] skipped DB/cache fallback_push_summary_df interval=%s now=%s "
            "in main.py to avoid NAS SQLite/cache fallback 0xC0000006; no memory summary available. "
            "main_database.py handles PUSH summary DB/cache. "
            "Set AUTOSTOCK_MAIN_SKIP_PUSH_SUMMARY_FALLBACK=0 to restore legacy DB fallback.",
            interval,
            now,
        )
        return pd.DataFrame()

    if callable(_ORIGINAL_FALLBACK_PUSH_SUMMARY_DF):
        return _ORIGINAL_FALLBACK_PUSH_SUMMARY_DF(interval, now=now, *args, **kwargs)
    return pd.DataFrame()


def _install_push_summary_fallback_skip() -> None:
    global _ORIGINAL_FALLBACK_PUSH_SUMMARY_DF

    try:
        import scheduler_jobs.summary.fallback_loader as fallback_loader_mod

        current = getattr(fallback_loader_mod, "fallback_push_summary_df", None)
        if getattr(current, "__name__", "") != "_patched_fallback_push_summary_df":
            _ORIGINAL_FALLBACK_PUSH_SUMMARY_DF = current
            fallback_loader_mod.fallback_push_summary_df = _patched_fallback_push_summary_df

        # runner_core.py は `from .fallback_loader import fallback_push_summary_df` で
        # 関数参照を保持するため、既にimport済みならこちらも差し替える。
        try:
            import scheduler_jobs.summary.runner_core as runner_core_mod
            runner_core_mod.fallback_push_summary_df = _patched_fallback_push_summary_df
        except Exception:
            logger.debug("[MAIN SKIP PUSH SUMMARY FALLBACK] runner_core patch skipped", exc_info=True)

        logger.warning(
            "[MAIN SKIP PUSH SUMMARY FALLBACK] installed enabled=%s memory=%s main_py=%s",
            _should_skip_push_summary_fallback(),
            _env_bool("AUTOSTOCK_MAIN_PUSH_SUMMARY_MEMORY_FALLBACK", True),
            _is_main_py_process(),
        )
    except Exception:
        logger.exception("[MAIN SKIP PUSH SUMMARY FALLBACK] install failed")


def install() -> bool:
    global _INSTALLED
    global _ORIGINAL_BOOTSTRAP_PUSH
    global _ORIGINAL_START_PUSH_STACK
    global _ORIGINAL_START_PUSH_STORAGE_SAFE
    global _ORIGINAL_START_PUSH_STREAM_EARLY_SAFE
    global _ORIGINAL_START_PUSH_STREAM_FALLBACK_SAFE

    try:
        os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_PUSH_DB_RESTORE", "1")
        os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_PUSH_STACK", "1")
        os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_PUSH_SUMMARY_FALLBACK", "1")
        os.environ.setdefault("AUTOSTOCK_MAIN_PUSH_SUMMARY_MEMORY_FALLBACK", "1")
        os.environ.setdefault("PUSH_STREAM_DB_WRITE", "0")
        os.environ.setdefault("PUSH_STREAM_ORDER_BOOK_WRITE", "0")

        import core.startup.push_bootstrap as push_bootstrap_mod

        current = getattr(push_bootstrap_mod, "bootstrap_push", None)
        if getattr(current, "__name__", "") != "_patched_bootstrap_push":
            _ORIGINAL_BOOTSTRAP_PUSH = current
            push_bootstrap_mod.bootstrap_push = _patched_bootstrap_push

        try:
            import core.startup.push_startup as push_startup_mod

            # push_startup.py は `from core.startup.push_bootstrap import bootstrap_push` で
            # 関数参照を保持しているため、こちらも差し替える。
            push_startup_mod.bootstrap_push = _patched_bootstrap_push

            current_stack = getattr(push_startup_mod, "start_push_stack_before_scheduler", None)
            if getattr(current_stack, "__name__", "") != "_patched_start_push_stack_before_scheduler":
                _ORIGINAL_START_PUSH_STACK = current_stack
                push_startup_mod.start_push_stack_before_scheduler = _patched_start_push_stack_before_scheduler

            current_storage = getattr(push_startup_mod, "start_push_storage_safe", None)
            if getattr(current_storage, "__name__", "") != "_patched_start_push_storage_safe":
                _ORIGINAL_START_PUSH_STORAGE_SAFE = current_storage
                push_startup_mod.start_push_storage_safe = _patched_start_push_storage_safe

            current_early = getattr(push_startup_mod, "start_push_stream_early_safe", None)
            if getattr(current_early, "__name__", "") != "_patched_start_push_stream_early_safe":
                _ORIGINAL_START_PUSH_STREAM_EARLY_SAFE = current_early
                push_startup_mod.start_push_stream_early_safe = _patched_start_push_stream_early_safe

            current_fallback = getattr(push_startup_mod, "start_push_stream_fallback_safe", None)
            if getattr(current_fallback, "__name__", "") != "_patched_start_push_stream_fallback_safe":
                _ORIGINAL_START_PUSH_STREAM_FALLBACK_SAFE = current_fallback
                push_startup_mod.start_push_stream_fallback_safe = _patched_start_push_stream_fallback_safe

            # startup_orchestrator.py は import 時点で関数参照を保持している可能性があるため、
            # 既に読み込まれている場合はこちらも差し替える。
            try:
                import core.startup.startup_orchestrator as orchestrator_mod
                orchestrator_mod.start_push_stack_before_scheduler = _patched_start_push_stack_before_scheduler
                orchestrator_mod.start_push_stream_fallback_safe = _patched_start_push_stream_fallback_safe
            except Exception:
                logger.debug("[MAIN SKIP PUSH STACK] orchestrator patch skipped", exc_info=True)

        except Exception:
            logger.debug("[MAIN SKIP PUSH DB RESTORE] push_startup patch skipped", exc_info=True)

        _install_push_summary_fallback_skip()

        _INSTALLED = True
        logger.warning(
            "[MAIN SKIP PUSH DB RESTORE] installed enabled=%s main_py=%s push_stack_skip=%s push_summary_fallback_skip=%s memory_fallback=%s",
            _should_skip_db_restore(),
            _is_main_py_process(),
            _should_skip_push_stack(),
            _should_skip_push_summary_fallback(),
            _env_bool("AUTOSTOCK_MAIN_PUSH_SUMMARY_MEMORY_FALLBACK", True),
        )
        return True
    except Exception:
        logger.exception("[MAIN SKIP PUSH DB RESTORE] install failed")
        return False


__all__ = ["install"]