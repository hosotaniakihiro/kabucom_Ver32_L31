# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-FRESH-DISPATCH-PENDING-RESCUE"
_INSTALLED = False

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(raw)
    except Exception:
        return float(default)


def _as_dt(v: Any) -> dt.datetime | None:
    try:
        if v is None or str(v).strip() == "":
            return None
        if hasattr(v, "to_pydatetime"):
            v = v.to_pydatetime()
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)
        s = str(v).strip()
        if s.lower() in {"nat", "nan", "none", "null"}:
            return None
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
        try:
            import pandas as pd  # type: ignore
            p = pd.to_datetime(v, errors="coerce")
            if str(p) == "NaT":
                return None
            if hasattr(p, "to_pydatetime"):
                p = p.to_pydatetime()
            if isinstance(p, dt.datetime):
                return p.replace(tzinfo=None)
        except Exception:
            pass
    except Exception:
        pass
    return None


def _fresh_runtime_age(entry: dict[str, Any]) -> tuple[float | None, str]:
    for key in ("pending_refreshed_at", "created_at", "queued_at", "approved_at", "built_at", "updated_at"):
        ts = _as_dt(entry.get(key))
        if ts is None:
            continue
        try:
            return max(0.0, (dt.datetime.now() - ts).total_seconds()), key
        except Exception:
            continue
    return None, ""


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import core.startup.summary_entry_pending_existing_fix_patch as pef
    except Exception:
        logger.exception("[SUMMARY ENTRY STALE RESCUE] import pending fix failed")
        return False

    old = getattr(pef, "_stale_summary_reason", None)
    if getattr(old, "_summary_entry_stale_rescue_v1", False):
        _INSTALLED = True
        return True

    def _stale_summary_reason_rescued(entry: dict[str, Any]) -> str | None:
        try:
            if not _env_bool("SUMMARY_ENTRY_STALE_RESCUE_ENABLED", True):
                return old(entry) if callable(old) else None

            # SUMMARY AI direct dispatch 直後に作られた候補は、summary足のdatetimeが古くても、
            # created_at / pending_refreshed_at が新しければ stale_skipped で捨てない。
            is_summary = False
            try:
                fn = getattr(pef, "_is_summary_ai_entry", None)
                is_summary = bool(fn(entry)) if callable(fn) else False
            except Exception:
                is_summary = False

            if is_summary:
                runtime_age, runtime_key = _fresh_runtime_age(entry)
                fresh_limit = _env_float("SUMMARY_ENTRY_FRESH_DISPATCH_MAX_AGE_SEC", 45.0)
                if runtime_age is not None and runtime_age <= fresh_limit:
                    logger.warning(
                        "[SUMMARY ENTRY STALE RESCUE] allow fresh runtime candidate symbol=%s identity=%s runtime_key=%s runtime_age=%.1fs limit=%.1fs bar_datetime=%s score=%s side=%s",
                        entry.get("symbol"),
                        getattr(pef, "_identity", lambda x: "")(entry),
                        runtime_key,
                        runtime_age,
                        fresh_limit,
                        entry.get("datetime"),
                        entry.get("score", entry.get("final_score")),
                        entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"),
                    )
                    return None

            return old(entry) if callable(old) else None
        except Exception:
            logger.debug("[SUMMARY ENTRY STALE RESCUE] rescued stale check failed", exc_info=True)
            return old(entry) if callable(old) else None

    _stale_summary_reason_rescued._summary_entry_stale_rescue_v1 = True  # type: ignore[attr-defined]
    _stale_summary_reason_rescued._original = old  # type: ignore[attr-defined]
    pef._stale_summary_reason = _stale_summary_reason_rescued
    _INSTALLED = True
    logger.warning(
        "[SUMMARY ENTRY STALE RESCUE] installed=True fresh_limit=%.1fs stale_skip_enabled=%s version=%s",
        _env_float("SUMMARY_ENTRY_FRESH_DISPATCH_MAX_AGE_SEC", 45.0),
        os.getenv("SUMMARY_SKIP_STALE_PENDING_ENABLED", "1"),
        VERSION,
    )
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY ENTRY STALE RESCUE] auto install failed")


__all__ = ["VERSION", "install"]
