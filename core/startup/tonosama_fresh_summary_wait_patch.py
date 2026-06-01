# ============================================================
# File   : core/startup/tonosama_fresh_summary_wait_patch.py
# Version: V1-TONOSAMA-FRESH-SUMMARY-WAIT-FAILOPEN
# ------------------------------------------------------------
# 目的:
#   trading.entry_exit.tasks の Tonosama 起動前 fresh summary wait が
#   get_push_merged_summary(1) だけを見て rows=0 / latest=None となり、
#   実データがあるのに Tonosama 本体を起動せず skip する問題を防ぐ。
#
# 症状:
#   [TONOSAMA ENTRY SCHEDULE] fresh push summary wait expired
#   latest=None age=None rows=0 ... -> skip this cycle
#
# 方針:
#   - push merged だけでなく summary_history / summary_loader も見る。
#   - それでも latest=None の場合は、Tonosama本体の stale guard に任せるため
#     fail-open して本体を起動する。
#   - import順に備えて retry thread で後追いpatchする。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_INSTALLING = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _latest_from_df(df: Any, *, source_name: str) -> tuple[float | None, dt.datetime | None, int, str]:
    try:
        import pandas as pd
        if df is None or not isinstance(df, pd.DataFrame):
            return None, None, 0, source_name
        rows = int(len(df))
        if df.empty:
            return None, None, rows, source_name
        col = None
        for c in ("datetime", "end_time", "start_time", "time", "snapshot_time"):
            if c in df.columns:
                col = c
                break
        if not col:
            return None, None, rows, source_name
        s = pd.to_datetime(df[col], errors="coerce").dropna()
        if s.empty:
            return None, None, rows, source_name
        latest = s.max().to_pydatetime().replace(tzinfo=None)
        age = (dt.datetime.now() - latest).total_seconds()
        return float(age), latest, rows, f"{source_name}:{col}"
    except Exception:
        logger.debug("[TONOSAMA FRESH WAIT PATCH] latest_from_df failed source=%s", source_name, exc_info=True)
        return None, None, 0, source_name


def _patched_latest_push_summary_age_sec() -> tuple[float | None, dt.datetime | None, int, str]:
    candidates: list[tuple[str, Any]] = []

    try:
        import core.global_context.context as ctx
        providers = [
            ("get_push_merged_summary", lambda: ctx.get_push_merged_summary(1)),
            ("get_merged_summary_push", lambda: ctx.get_merged_summary(1, source="push")),
            ("get_summary_history_push", lambda: ctx.get_summary_history(1, source="push")),
        ]
        for name, fn in providers:
            try:
                candidates.append((name, fn()))
            except Exception:
                logger.debug("[TONOSAMA FRESH WAIT PATCH] provider failed %s", name, exc_info=True)
    except Exception:
        logger.debug("[TONOSAMA FRESH WAIT PATCH] global_context import failed", exc_info=True)

    try:
        from trading.entry.tonosama.summary_loader import load_merged_summary
        candidates.append(("tonosama.summary_loader.load_merged_summary", load_merged_summary(1)))
    except Exception:
        logger.debug("[TONOSAMA FRESH WAIT PATCH] summary_loader fallback failed", exc_info=True)

    best: tuple[float | None, dt.datetime | None, int, str] = (None, None, 0, "none")
    for name, df in candidates:
        age, latest, rows, src = _latest_from_df(df, source_name=name)
        if latest is None:
            if rows > best[2]:
                best = (age, latest, rows, src)
            continue
        if best[1] is None or latest > best[1]:
            best = (age, latest, rows, src)
    return best


def _patched_wait_fresh_push_summary_before_tonosama() -> bool:
    try:
        if not _env_bool("TONOSAMA_WAIT_FRESH_PUSH_SUMMARY", True):
            return True
        max_age = max(30.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_MAX_AGE_SEC", 180.0))
        wait_sec = max(0.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_WAIT_SEC", 15.0))
        poll = max(0.25, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_POLL_SEC", 1.0))
        fail_open_empty = _env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", True)
        deadline = time.perf_counter() + wait_sec
        last_age = None
        last_dt = None
        last_rows = 0
        last_src = "none"

        while True:
            age, latest, rows, src = _patched_latest_push_summary_age_sec()
            last_age, last_dt, last_rows, last_src = age, latest, rows, src
            if age is not None and age <= max_age:
                logger.info(
                    "[TONOSAMA ENTRY SCHEDULE] fresh push summary ok latest=%s age=%.1fs rows=%s max_age=%.1fs source=%s patched=1",
                    latest, age, rows, max_age, src,
                )
                return True
            if latest is None and fail_open_empty:
                logger.warning(
                    "[TONOSAMA ENTRY SCHEDULE] fresh push summary unavailable latest=None rows=%s source=%s -> fail-open to Tonosama body patched=1",
                    rows, src,
                )
                return True
            if time.perf_counter() >= deadline:
                if last_dt is None and fail_open_empty:
                    logger.warning(
                        "[TONOSAMA ENTRY SCHEDULE] fresh push summary wait expired latest=None rows=%s source=%s -> fail-open to Tonosama body patched=1",
                        last_rows, last_src,
                    )
                    return True
                logger.warning(
                    "[TONOSAMA ENTRY SCHEDULE] fresh push summary wait expired latest=%s age=%s rows=%s max_age=%.1fs wait_sec=%.1fs source=%s -> skip this cycle patched=1",
                    last_dt, None if last_age is None else round(last_age, 1), last_rows, max_age, wait_sec, last_src,
                )
                return False
            time.sleep(poll)
    except Exception:
        logger.exception("[TONOSAMA FRESH WAIT PATCH] wait failed -> fail-open")
        return True


def _apply_patch() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, "_wait_fresh_push_summary_before_tonosama", None)
        if getattr(cur, "_tonosama_fresh_wait_patch_v1", False):
            _INSTALLED = True
            return True
        setattr(tasks, "_latest_push_summary_age_sec", _patched_latest_push_summary_age_sec)
        _patched_wait_fresh_push_summary_before_tonosama._tonosama_fresh_wait_patch_v1 = True  # type: ignore[attr-defined]
        setattr(tasks, "_wait_fresh_push_summary_before_tonosama", _patched_wait_fresh_push_summary_before_tonosama)
        os.environ.setdefault("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", "1")
        _INSTALLED = True
        logger.warning("[TONOSAMA FRESH WAIT PATCH] installed v1 fail_open_empty=%s", os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY"))
        return True
    except Exception:
        logger.debug("[TONOSAMA FRESH WAIT PATCH] target not ready", exc_info=True)
        return False


def install(retry: bool = True) -> bool:
    global _INSTALLING
    if _apply_patch():
        return True
    if retry and not _INSTALLING:
        _INSTALLING = True

        def _retry_loop() -> None:
            global _INSTALLING
            try:
                for _ in range(60):
                    if _apply_patch():
                        return
                    time.sleep(0.25)
                logger.warning("[TONOSAMA FRESH WAIT PATCH] retry exhausted")
            finally:
                _INSTALLING = False

        threading.Thread(target=_retry_loop, name="tonosama-fresh-summary-wait-patch", daemon=True).start()
    return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA FRESH WAIT PATCH] auto install failed")


__all__ = ["install"]
