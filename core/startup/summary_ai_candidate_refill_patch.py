# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_candidate_refill_patch.py
# Version: V2-SUMMARY-AI-CANDIDATE-REFILL-SAFETY-GUARDS
# ------------------------------------------------------------
# Purpose:
#   2026-06-29 logs showed SUMMARY_AI could run while PUSH was only
#   memory-resident:
#       writer_ready=False / memory_only=True / total_flushed=0
#
#   This patch keeps the existing candidate refill behaviour, but adds
#   fail-safe guards that are installed from this already-loaded startup
#   module:
#     1) block SUMMARY/SUMMARY_AI entry while PUSH writer is not ready,
#     2) suppress stale DB fallback rows during market session,
#     3) drop Tonosama history-missing fail-open rows.
#
# Safety:
#   - Does not bypass SELL credit guard, liquidity guard, final entry guard,
#     market-hour guard, risk guard, order builder, or broker checks.
#   - Only widens candidate pools after fresh/writer checks pass.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V2-SUMMARY-AI-CANDIDATE-REFILL-SAFETY-GUARDS"
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _as_len(v: Any) -> int:
    try:
        if v is None:
            return 0
        return len(v)
    except Exception:
        return 0


def _to_datetime(v: Any) -> dt.datetime | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    try:
        import pandas as pd

        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None
        try:
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.tz_convert(None)
        except Exception:
            pass
        if hasattr(ts, "to_pydatetime"):
            return ts.to_pydatetime().replace(tzinfo=None)
    except Exception:
        pass
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00").replace("+00:00", "")).replace(tzinfo=None)
    except Exception:
        return None


def _df_latest_age_sec(df: Any) -> tuple[bool, float | None, dt.datetime | None, int]:
    try:
        if df is None or getattr(df, "empty", True):
            return False, None, None, 0
        cols = getattr(df, "columns", [])
        dt_col = None
        for c in ("datetime", "end_time", "last_update", "updated_at", "inserted_at"):
            if c in cols:
                dt_col = c
                break
        if not dt_col:
            return False, None, None, len(df)
        latest = _to_datetime(df[dt_col].max())
        if latest is None:
            return False, None, None, len(df)
        age = (dt.datetime.now().replace(tzinfo=None) - latest).total_seconds()
        return True, age, latest, len(df)
    except Exception:
        return False, None, None, 0


def _get_push_1m_context() -> Any:
    try:
        import core.global_context.context as ctx
    except Exception:
        return None
    calls = (
        ("get_push_merged_summary", {"tf": 1}),
        ("get_push_merged_summary", {"interval": 1}),
        ("get_merged_summary", {"tf": 1, "source": "push"}),
        ("get_merged_summary", {"interval": 1, "source": "push"}),
        ("get_summary_history", {"tf": 1, "source": "push"}),
        ("get_summary_history", {"interval": 1, "source": "push"}),
    )
    for name, kwargs in calls:
        fn = getattr(ctx, name, None)
        if not callable(fn):
            continue
        try:
            return fn(**kwargs)
        except TypeError:
            continue
        except Exception:
            continue
    return None


def _push_writer_state() -> tuple[bool, str]:
    if not _env_bool("SUMMARY_AI_REQUIRE_PUSH_WRITER_READY", True):
        return True, "writer_check_disabled"

    saw_memory_only = False
    saw_writer_false = False
    saw_writer_true = False
    details: list[str] = []
    for mod_name in (
        "trading.push.push_stream.monitor",
        "trading.push.push_stream",
        "trading.push.push_db_writer",
        "trading.push.db_writer",
        "global_data",
    ):
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except Exception:
            continue
        for attr in ("memory_only", "PUSH_MEMORY_ONLY"):
            try:
                if hasattr(mod, attr):
                    val = getattr(mod, attr)
                    details.append(f"{mod_name}.{attr}={val}")
                    if val is True:
                        saw_memory_only = True
            except Exception:
                pass
        for attr in ("writer_ready", "push_writer_ready", "PUSH_WRITER_READY"):
            try:
                if hasattr(mod, attr):
                    val = getattr(mod, attr)
                    details.append(f"{mod_name}.{attr}={val}")
                    if val is True:
                        saw_writer_true = True
                    elif val is False:
                        saw_writer_false = True
            except Exception:
                pass

    if saw_memory_only:
        return False, "memory_only_true " + " ".join(details[-6:])
    if saw_writer_false:
        return False, "writer_ready_false " + " ".join(details[-6:])
    if saw_writer_true:
        return True, "writer_ready_true"
    return False, "writer_state_unknown"


def _summary_ai_entry_unsafe_reason() -> str | None:
    if _env_bool("SUMMARY_AI_REQUIRE_FRESH_PUSH_1M", True):
        df = _get_push_1m_context()
        ok, age, latest, rows = _df_latest_age_sec(df)
        if not ok:
            return f"no_fresh_push_1m rows={rows}"
        max_age = _env_float("SUMMARY_AI_MAX_PUSH_1M_AGE_SEC", 120.0)
        if age is None or age > max_age:
            return f"stale_push_1m rows={rows} latest={latest} age={age} max={max_age}"

    ok, reason = _push_writer_state()
    if not ok:
        return reason
    return None


def _result_needs_refill(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    try:
        execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
        if bool(execution.get("executed")):
            return False
        if _as_len(result.get("approved_rows")) > 0:
            return False
        if _as_len(result.get("ai_ok")) > 0:
            return False
        candidates_n = _as_len(result.get("candidates"))
        ai_results_n = _as_len(result.get("ai_results"))
        skip = str(execution.get("skip_reason") or result.get("skip_reason") or "").lower()
        if candidates_n == 0 or ai_results_n == 0:
            return True
        if any(x in skip for x in ("no_candidates", "no_ai_ok", "no approved", "approved")):
            return True
    except Exception:
        return False
    return False


def _blocked_result(reason: str) -> dict[str, Any]:
    return {
        "candidates": [],
        "ai_results": [],
        "ai_ok": [],
        "approved_rows": [],
        "execution": {"executed": False, "skip_reason": f"blocked_by_summary_ai_safety_guard:{reason}"},
        "skip_reason": f"blocked_by_summary_ai_safety_guard:{reason}",
        "safety_blocked": True,
    }


def _apply_wide_kwargs(kwargs: dict[str, Any], *, refill: bool = False) -> dict[str, Any]:
    out = dict(kwargs)
    top_n = max(1, _env_int("SUMMARY_AI_REFILL_TOP_N", 60))
    tonosama_n = max(1, _env_int("SUMMARY_AI_REFILL_TONOSAMA_MAX_CANDIDATES", top_n))
    if refill:
        top_n = max(top_n, _env_int("SUMMARY_AI_REFILL_RETRY_TOP_N", 80))
        tonosama_n = max(tonosama_n, _env_int("SUMMARY_AI_REFILL_RETRY_TONOSAMA_MAX_CANDIDATES", top_n))

    for key in ("top_n", "max_candidates", "candidate_limit"):
        try:
            cur = int(float(out.get(key))) if key in out and out.get(key) is not None else 0
        except Exception:
            cur = 0
        if cur <= 0 or cur < top_n:
            out[key] = top_n

    try:
        cur_t = int(float(out.get("tonosama_max_candidates"))) if out.get("tonosama_max_candidates") is not None else 0
    except Exception:
        cur_t = 0
    if cur_t <= 0 or cur_t < tonosama_n:
        out["tonosama_max_candidates"] = tonosama_n

    return out


def _summarize_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
    return {
        "candidates": _as_len(result.get("candidates")),
        "ai_results": _as_len(result.get("ai_results")),
        "ai_ok": _as_len(result.get("ai_ok")),
        "approved": _as_len(result.get("approved_rows")),
        "executed": bool(execution.get("executed")),
        "skip": execution.get("skip_reason") or result.get("skip_reason"),
    }


def _install_summary_ai_guard() -> bool:
    try:
        from trading.entry.summary_ai import runner as r

        cur = getattr(r, "run_summary_ai_entry_from_df", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI SAFETY GUARD] target missing")
            return False
        if getattr(cur, "_summary_ai_candidate_refill_v2", False):
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(cur)
        def patched_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
            source = str(kwargs.get("source") or "SUMMARY").upper()
            if "SUMMARY" in source and "RANKING" not in source:
                reason = _summary_ai_entry_unsafe_reason()
                if reason:
                    logger.warning(
                        "[SUMMARY AI SAFETY GUARD] blocked source=%s interval=%s reason=%s",
                        source,
                        kwargs.get("interval"),
                        reason,
                    )
                    return _blocked_result(reason)

            wide_kwargs = _apply_wide_kwargs(kwargs, refill=False)
            first = cur(*args, **wide_kwargs)
            if not _env_bool("SUMMARY_AI_REFILL_RETRY_WITHOUT_TONOSAMA", True):
                return first
            if not _result_needs_refill(first):
                return first

            retry_kwargs = _apply_wide_kwargs(kwargs, refill=True)
            retry_kwargs["use_tonosama_filter"] = False
            retry_kwargs["fail_open_tonosama"] = False
            logger.warning(
                "[SUMMARY AI CANDIDATE REFILL] first pass empty -> retry without TONOSAMA first=%s retry_top_n=%s source=%s interval=%s",
                _summarize_result(first),
                retry_kwargs.get("top_n"),
                retry_kwargs.get("source"),
                retry_kwargs.get("interval"),
            )
            second = cur(*args, **retry_kwargs)
            if isinstance(second, dict):
                second = dict(second)
                second["candidate_refill_used"] = True
                second["candidate_refill_first"] = _summarize_result(first)
                second["candidate_refill_retry"] = _summarize_result(second)
            return second

        patched_run_summary_ai_entry_from_df._summary_ai_candidate_refill_v2 = True  # type: ignore[attr-defined]
        patched_run_summary_ai_entry_from_df._original = orig  # type: ignore[attr-defined]
        r.run_summary_ai_entry_from_df = patched_run_summary_ai_entry_from_df
        try:
            r.DEFAULT_AI_ENTRY_TOP_N = max(int(getattr(r, "DEFAULT_AI_ENTRY_TOP_N", 20)), _env_int("SUMMARY_AI_REFILL_TOP_N", 60))
            r.DEFAULT_TONOSAMA_AI_CANDIDATES = max(int(getattr(r, "DEFAULT_TONOSAMA_AI_CANDIDATES", 20)), _env_int("SUMMARY_AI_REFILL_TONOSAMA_MAX_CANDIDATES", 60))
        except Exception:
            pass
        logger.warning(
            "[SUMMARY AI SAFETY GUARD] installed version=%s top_n=%s tonosama_max=%s writer_required=%s fresh_required=%s",
            VERSION,
            _env_int("SUMMARY_AI_REFILL_TOP_N", 60),
            _env_int("SUMMARY_AI_REFILL_TONOSAMA_MAX_CANDIDATES", 60),
            _env_bool("SUMMARY_AI_REQUIRE_PUSH_WRITER_READY", True),
            _env_bool("SUMMARY_AI_REQUIRE_FRESH_PUSH_1M", True),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI SAFETY GUARD] install failed")
        return False


def _install_stale_fallback_guard() -> bool:
    try:
        import pandas as pd
        import scheduler_jobs.summary.fallback_loader as fl

        cur = getattr(fl, "select_best_candidate", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_stale_fallback_guard_v1", False):
            return True

        @wraps(cur)
        def patched_select_best_candidate(*args: Any, **kwargs: Any):
            out = cur(*args, **kwargs)
            try:
                if out is None or getattr(out, "empty", True):
                    return out
                interval = int(kwargs.get("interval") or 1)
                for_ranking = bool(kwargs.get("for_ranking", False))
                if for_ranking:
                    return out
                ok, age, latest, rows = _df_latest_age_sec(out)
                max_age = _env_float("SUMMARY_STALE_DB_FALLBACK_MAX_AGE_SEC", 420.0)
                if ok and age is not None and age > max_age:
                    logger.warning(
                        "[SUMMARY FALLBACK STALE GUARD] dropped stale fallback interval=%s rows=%s latest=%s age=%.1f max=%.1f",
                        interval,
                        rows,
                        latest,
                        age,
                        max_age,
                    )
                    return pd.DataFrame()
            except Exception:
                logger.exception("[SUMMARY FALLBACK STALE GUARD] check failed")
            return out

        patched_select_best_candidate._summary_stale_fallback_guard_v1 = True  # type: ignore[attr-defined]
        patched_select_best_candidate._original = cur  # type: ignore[attr-defined]
        fl.select_best_candidate = patched_select_best_candidate
        logger.warning("[SUMMARY FALLBACK STALE GUARD] installed max_age=%s", _env_float("SUMMARY_STALE_DB_FALLBACK_MAX_AGE_SEC", 420.0))
        return True
    except Exception:
        logger.exception("[SUMMARY FALLBACK STALE GUARD] install failed")
        return False


def _install_tonosama_failopen_guard() -> bool:
    try:
        import pandas as pd
        import trading.entry.tonosama.volume_surge as vs

        cur = getattr(vs, "build_scalping_feature_df", None)
        if not callable(cur):
            return False
        if getattr(cur, "_tonosama_failopen_drop_guard_v1", False):
            return True

        @wraps(cur)
        def patched_build_scalping_feature_df(*args: Any, **kwargs: Any):
            out = cur(*args, **kwargs)
            try:
                if not isinstance(out, pd.DataFrame) or out.empty:
                    return out
                before = len(out)
                x = out
                if _env_bool("TONOSAMA_DISABLE_HISTORY_FAILOPEN", True):
                    for col in ("_volume_surge_history_missing", "_volume_surge_failopen"):
                        if col in x.columns:
                            x = x[~x[col].fillna(False).astype(bool)].copy()
                if "technical_ready" in x.columns and _env_bool("TONOSAMA_REQUIRE_TECHNICAL_READY", True):
                    x = x[x["technical_ready"].fillna(False).astype(bool)].copy()
                after = len(x)
                if after != before:
                    logger.warning("[TONOSAMA FAILOPEN GUARD] dropped unsafe rows before=%s after=%s", before, after)
                return x
            except Exception:
                logger.exception("[TONOSAMA FAILOPEN GUARD] failed")
                return out

        patched_build_scalping_feature_df._tonosama_failopen_drop_guard_v1 = True  # type: ignore[attr-defined]
        patched_build_scalping_feature_df._original = cur  # type: ignore[attr-defined]
        vs.build_scalping_feature_df = patched_build_scalping_feature_df
        logger.warning("[TONOSAMA FAILOPEN GUARD] installed")
        return True
    except Exception:
        logger.exception("[TONOSAMA FAILOPEN GUARD] install failed")
        return False


def install() -> bool:
    global _INSTALLED
    if not _env_bool("SUMMARY_AI_CANDIDATE_REFILL_ENABLED", True):
        logger.warning("[SUMMARY AI SAFETY GUARD] disabled by env")
        return False
    os.environ.setdefault("SUMMARY_AI_REQUIRE_PUSH_WRITER_READY", "1")
    os.environ.setdefault("SUMMARY_AI_REQUIRE_FRESH_PUSH_1M", "1")
    os.environ.setdefault("SUMMARY_AI_MAX_PUSH_1M_AGE_SEC", "120")
    os.environ.setdefault("SUMMARY_STALE_DB_FALLBACK_MAX_AGE_SEC", "420")
    os.environ.setdefault("TONOSAMA_DISABLE_HISTORY_FAILOPEN", "1")
    os.environ.setdefault("TONOSAMA_REQUIRE_TECHNICAL_READY", "1")
    ok_ai = _install_summary_ai_guard()
    ok_fb = _install_stale_fallback_guard()
    ok_tono = _install_tonosama_failopen_guard()
    _INSTALLED = bool(ok_ai or ok_fb or ok_tono)
    logger.warning("[SUMMARY AI SAFETY GUARD] install done ai=%s fallback=%s tonosama=%s", ok_ai, ok_fb, ok_tono)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI SAFETY GUARD] auto install failed")

__all__ = ["VERSION", "install"]
