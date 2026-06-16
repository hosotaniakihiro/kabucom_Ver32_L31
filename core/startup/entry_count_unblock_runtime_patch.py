# -*- coding: utf-8 -*-
"""
Runtime patch to prevent valid intraday entry candidates from being dropped too aggressively.

Fixes production symptoms seen in 2026-06-16 logs:
- SUMMARY_AI candidates dropped by SHORT_MTF_SLOPE_MISSING when only 1m slope is ready.
- TONOSAMA skipped because push summary is stale while raw PUSH is still receiving ticks.
- Entry pipeline blocked for a long time by board retry around PUSH A/B rotation boundary.
- API key mismatch recovery refreshes a token but downstream modules keep using the stale token.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-ENTRY-COUNT-UNBLOCK"
_INSTALLED = False
_RETRYING = False


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
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _publish_token(token: str) -> None:
    token = str(token or "").strip()
    if not token:
        return
    for key in ("KABU_API_KEY", "KABUSAPI_API_KEY", "KABUSAPI_TOKEN", "KABU_TOKEN", "X_API_KEY"):
        os.environ[key] = token
    for module_name, attr_name in (("global_state", "global_data"), ("core.global_context.context", "global_data")):
        try:
            mod = __import__(module_name, fromlist=[attr_name])
            gd = getattr(mod, attr_name, None)
            if gd is None:
                continue
            for name in ("kabu_api_key", "kabusapi_api_key", "api_key", "token", "kabu_token", "kabusapi_token"):
                try:
                    setattr(gd, name, token)
                except Exception:
                    pass
            headers = getattr(gd, "headers", None)
            if isinstance(headers, dict):
                headers["X-API-KEY"] = token
        except Exception:
            continue


def _patch_token_manager() -> bool:
    try:
        import token_manager  # type: ignore
    except Exception:
        return False
    if getattr(token_manager, "_ENTRY_COUNT_UNBLOCK_TOKEN_PATCHED", False):
        return True
    old_refresh = getattr(token_manager, "refresh_token", None)
    old_get = getattr(token_manager, "get_valid_token", None)
    if not callable(old_refresh) or not callable(old_get):
        return False

    def refresh_token_patched(*args, **kwargs):
        token = old_refresh(*args, **kwargs)
        if token:
            _publish_token(str(token))
        return token

    def get_valid_token_patched(*args, **kwargs):
        token = None
        for env_name in ("KABUSAPI_TOKEN", "KABU_TOKEN", "KABU_API_KEY", "KABUSAPI_API_KEY", "X_API_KEY"):
            v = os.getenv(env_name)
            if v and len(str(v).strip()) >= 16:
                token = str(v).strip()
                break
        if token:
            try:
                token_manager.API_TOKEN = token
            except Exception:
                pass
            return token
        return old_get(*args, **kwargs)

    try:
        token_manager.refresh_token = refresh_token_patched  # type: ignore[attr-defined]
        token_manager.get_valid_token = get_valid_token_patched  # type: ignore[attr-defined]
        token_manager._ENTRY_COUNT_UNBLOCK_TOKEN_PATCHED = True  # type: ignore[attr-defined]
        logger.warning("[ENTRY COUNT UNBLOCK] token_manager patched publish refreshed token")
        return True
    except Exception:
        logger.exception("[ENTRY COUNT UNBLOCK] token_manager patch failed")
        return False


def _apply_entry_defaults() -> None:
    if not _env_bool("ENTRY_COUNT_UNBLOCK_RELAX_SHORT_MTF", True):
        return
    # Logs showed SUMMARY_AI candidates were rejected because 3m/5m slopes were missing.
    # Use 1 available/aligned short slope as the default, while still blocking opposite slopes when present.
    os.environ["ENTRY_SHORT_MTF_REQUIRED"] = "1"
    os.environ["ENTRY_SHORT_MTF_MIN_AVAILABLE"] = os.getenv("ENTRY_SHORT_MTF_MIN_AVAILABLE_FORCE", "1")
    os.environ["ENTRY_SHORT_MTF_MIN_ALIGNED"] = os.getenv("ENTRY_SHORT_MTF_MIN_ALIGNED_FORCE", "1")
    os.environ.setdefault("ENTRY_SHORT_MTF_ZERO_NEUTRAL", "1")
    os.environ.setdefault("ENTRY_SHORT_MTF_DB_BACKFILL", "1")
    os.environ.setdefault("ENTRY_DAILY_MTF_OPTIONAL", "1")
    os.environ["ENTRY_ORDER_REQUIRE_MTF_DATA"] = "0"
    os.environ["ENTRY_ORDER_REQUIRE_5S_DATA"] = "0"

    # A/B PUSH rotation boundary can make board retry wait 5.6s per candidate. Keep a short retry only.
    os.environ["ENTRY_ORDER_BOARD_RETRY_SEC"] = os.getenv("ENTRY_ORDER_BOARD_RETRY_SEC_FORCE", "1.2")
    os.environ["ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC"] = os.getenv("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC_FORCE", "0.20")
    os.environ.setdefault("ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", "0")


def _patch_tonosama_wait_once() -> bool:
    try:
        import trading.entry_exit.tasks as tasks  # type: ignore
    except Exception:
        return False
    if getattr(tasks, "_ENTRY_COUNT_UNBLOCK_TONOSAMA_STALE_PATCHED", False):
        return True
    old_wait = getattr(tasks, "_wait_fresh_push_summary_before_tonosama", None)
    old_latest = getattr(tasks, "_latest_push_summary_age_sec", None)
    if not callable(old_wait) or not callable(old_latest):
        return False

    def _patched_wait_fresh_push_summary_before_tonosama() -> bool:
        try:
            age, latest, rows = old_latest()
        except Exception:
            age, latest, rows = None, None, 0
        max_age = max(30.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_MAX_AGE_SEC", 180.0))
        stale_fail_open = _env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE", True)
        stale_max_age = max(max_age, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_STALE_FAIL_OPEN_MAX_AGE_SEC", 1800.0))
        min_rows = max(1, _env_int("TONOSAMA_WAIT_PUSH_SUMMARY_STALE_FAIL_OPEN_MIN_ROWS", 20))
        if age is not None and age <= max_age:
            return True
        if latest is not None and age is not None and stale_fail_open and age <= stale_max_age and int(rows or 0) >= min_rows:
            logger.warning(
                "[TONOSAMA ENTRY SCHEDULE] stale push summary fail-open latest=%s age=%.1fs rows=%s max_age=%.1fs stale_max_age=%.1fs patched=entry_count_unblock",
                latest,
                float(age),
                rows,
                max_age,
                stale_max_age,
            )
            return True
        return bool(old_wait())

    try:
        _patched_wait_fresh_push_summary_before_tonosama._entry_count_unblock_v1 = True  # type: ignore[attr-defined]
        _patched_wait_fresh_push_summary_before_tonosama._original = old_wait  # type: ignore[attr-defined]
        setattr(tasks, "_wait_fresh_push_summary_before_tonosama", _patched_wait_fresh_push_summary_before_tonosama)
        setattr(tasks, "_ENTRY_COUNT_UNBLOCK_TONOSAMA_STALE_PATCHED", True)
        logger.warning("[ENTRY COUNT UNBLOCK] TONOSAMA stale wait patched")
        return True
    except Exception:
        logger.exception("[ENTRY COUNT UNBLOCK] TONOSAMA stale wait patch failed")
        return False


def _retry_patch_tonosama_wait() -> None:
    global _RETRYING
    try:
        for _ in range(120):
            if _patch_tonosama_wait_once():
                return
            time.sleep(0.25)
        logger.warning("[ENTRY COUNT UNBLOCK] TONOSAMA stale wait retry exhausted")
    finally:
        _RETRYING = False


def _start_retry_thread() -> None:
    global _RETRYING
    if _RETRYING:
        return
    _RETRYING = True
    threading.Thread(target=_retry_patch_tonosama_wait, name="entry-count-unblock-tonosama", daemon=True).start()


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("ENTRY_COUNT_UNBLOCK_PATCH_ENABLED", True):
        logger.warning("[ENTRY COUNT UNBLOCK] disabled by env")
        return False
    _apply_entry_defaults()
    token_ok = _patch_token_manager()
    tonosama_ok = _patch_tonosama_wait_once()
    if not tonosama_ok:
        _start_retry_thread()
    os.environ["TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE"] = "1"
    os.environ.setdefault("TONOSAMA_WAIT_PUSH_SUMMARY_STALE_FAIL_OPEN_MAX_AGE_SEC", "1800")
    _INSTALLED = True
    logger.warning(
        "[ENTRY COUNT UNBLOCK] installed version=%s token_patch=%s tonosama_patch=%s short_mtf_min_available=%s short_mtf_min_aligned=%s board_retry_sec=%s stale_fail_open=%s stale_max_age=%s",
        VERSION,
        token_ok,
        tonosama_ok,
        os.environ.get("ENTRY_SHORT_MTF_MIN_AVAILABLE"),
        os.environ.get("ENTRY_SHORT_MTF_MIN_ALIGNED"),
        os.environ.get("ENTRY_ORDER_BOARD_RETRY_SEC"),
        os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE"),
        os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_STALE_FAIL_OPEN_MAX_AGE_SEC"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY COUNT UNBLOCK] auto install failed")


__all__ = ["VERSION", "install"]
