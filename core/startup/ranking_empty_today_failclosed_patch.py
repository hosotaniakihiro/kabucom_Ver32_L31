# ============================================================
# File   : core/startup/ranking_empty_today_failclosed_patch.py
# Version: V1-RANKING-TODAY-EMPTY-FAIL-CLOSED
# ------------------------------------------------------------
# Purpose:
#   During market session, if rankingYYYYMMDD.db exists but has no usable
#   ranking tables / rows, do not return that path as a usable ranking DB.
#
# Background log:
#   [ATS RANKING] preferred today db exists but empty/unusable -> use today db anyway
#   [RANKING SUMMARY RUNNER] ranking snapshot empty trade_date=YYYY-MM-DD
#
# Desired behavior:
#   - ranking summary: empty DB -> skip, do not announce / resample as valid
#   - ranking entry: empty DB -> fail closed, do not create pending
#   - fallback to old DB remains blocked during market session
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_GET_USABLE = None
_ORIG_GET_INTERNAL = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _today_path(mod, now: Optional[dt.datetime] = None) -> str:
    try:
        fn = getattr(mod, "get_today_ranking_db_path", None)
        if callable(fn):
            return str(fn(now=now))
    except Exception:
        pass
    try:
        fn = getattr(mod, "_today_ranking_db_path", None)
        if callable(fn):
            return str(fn(now))
    except Exception:
        pass
    return ""


def _in_session(mod, now: Optional[dt.datetime] = None) -> bool:
    try:
        fn = getattr(mod, "_is_market_session_like", None)
        if callable(fn):
            return bool(fn(now))
    except Exception:
        pass
    now = (now or dt.datetime.now()).replace(microsecond=0)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt.time(9, 0) <= t <= dt.time(11, 30) or dt.time(12, 30) <= t <= dt.time(15, 30)


def _is_usable(mod, path: str) -> bool:
    try:
        fn = getattr(mod, "_db_has_usable_ranking_tables", None)
        if callable(fn):
            return bool(fn(path))
    except Exception:
        logger.debug("[RANKING EMPTY TODAY FAILCLOSED] usable check failed path=%s", path, exc_info=True)
    return False


def _maybe_fail_closed(mod, path: Optional[str], *, now: Optional[dt.datetime], caller: str) -> Optional[str]:
    if not _env_bool("RANKING_TODAY_EMPTY_FAIL_CLOSED", True):
        return path
    if not path:
        return path

    now_dt = (now or dt.datetime.now()).replace(microsecond=0)
    today = _today_path(mod, now_dt)
    try:
        same = Path(str(path)).resolve() == Path(today).resolve()
    except Exception:
        same = str(path) == str(today)

    if not same:
        return path

    if not _in_session(mod, now_dt):
        return path

    if _is_usable(mod, str(path)):
        return path

    logger.warning(
        "[RANKING EMPTY TODAY FAILCLOSED] return None caller=%s path=%s reason=today_db_empty_unusable in_session=True",
        caller,
        path,
    )
    try:
        clear = getattr(mod, "invalidate_ranking_db_path_cache", None)
        if callable(clear):
            clear()
    except Exception:
        pass
    return None


def install() -> bool:
    global _INSTALLED, _ORIG_GET_USABLE, _ORIG_GET_INTERNAL
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("RANKING_TODAY_EMPTY_FAIL_CLOSED", "1")
        os.environ.setdefault("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", "1")
        os.environ.setdefault("RANKING_ENTRY_REQUIRE_TODAY", "1")
        os.environ.setdefault("RANKING_ENTRY_CLEAR_PENDING_ON_STALE", "1")

        import ats.ats_ranking.db_path as db_path

        cur_get = getattr(db_path, "get_usable_ranking_db_path", None)
        cur_internal = getattr(db_path, "_get_ranking_db_path", None)

        if callable(cur_get) and not getattr(cur_get, "_ranking_today_empty_failclosed_v1", False):
            _ORIG_GET_USABLE = cur_get

            def wrapped_get_usable_ranking_db_path(*args, **kwargs):
                now = kwargs.get("now")
                path = _ORIG_GET_USABLE(*args, **kwargs)
                return _maybe_fail_closed(db_path, path, now=now, caller="get_usable_ranking_db_path")

            wrapped_get_usable_ranking_db_path._ranking_today_empty_failclosed_v1 = True  # type: ignore[attr-defined]
            wrapped_get_usable_ranking_db_path._original = _ORIG_GET_USABLE  # type: ignore[attr-defined]
            db_path.get_usable_ranking_db_path = wrapped_get_usable_ranking_db_path

        if callable(cur_internal) and not getattr(cur_internal, "_ranking_today_empty_failclosed_v1", False):
            _ORIG_GET_INTERNAL = cur_internal

            def wrapped_get_ranking_db_path(*args, **kwargs):
                now = kwargs.get("now")
                path = _ORIG_GET_INTERNAL(*args, **kwargs)
                return _maybe_fail_closed(db_path, path, now=now, caller="_get_ranking_db_path")

            wrapped_get_ranking_db_path._ranking_today_empty_failclosed_v1 = True  # type: ignore[attr-defined]
            wrapped_get_ranking_db_path._original = _ORIG_GET_INTERNAL  # type: ignore[attr-defined]
            db_path._get_ranking_db_path = wrapped_get_ranking_db_path

        _INSTALLED = True
        logger.warning(
            "[RANKING EMPTY TODAY FAILCLOSED] installed enabled=%s",
            os.getenv("RANKING_TODAY_EMPTY_FAIL_CLOSED"),
        )
        return True
    except Exception:
        logger.exception("[RANKING EMPTY TODAY FAILCLOSED] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING EMPTY TODAY FAILCLOSED] auto install failed")

__all__ = ["install"]
