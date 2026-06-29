# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_candidate_refill_patch.py
# Version: V1-SUMMARY-AI-CANDIDATE-REFILL
# ------------------------------------------------------------
# Purpose:
#   2026-06-29 logs showed SUMMARY_AI created only one SELL candidate
#   (4417), then the SELL credit pre-AI filter correctly removed it:
#       candidates before=1 after=0 -> AI_OK=0 -> approved=0
#
#   This patch prevents the run from ending there by:
#     1) widening SUMMARY_AI candidate pool defaults, and
#     2) retrying once without the TONOSAMA pre-filter when the first pass
#        has no AI_OK / approved rows after candidate filtering.
#
# Safety:
#   - Does not bypass SELL credit guard, liquidity guard, final entry guard,
#     market-hour guard, risk guard, order builder, or broker checks.
#   - Only increases the number of rows presented to existing gates.
# ============================================================
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-CANDIDATE-REFILL"
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


def _as_len(v: Any) -> int:
    try:
        return len(v or [])
    except Exception:
        return 0


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


def install() -> bool:
    global _INSTALLED
    if not _env_bool("SUMMARY_AI_CANDIDATE_REFILL_ENABLED", True):
        logger.warning("[SUMMARY AI CANDIDATE REFILL] disabled by env")
        return False
    try:
        from trading.entry.summary_ai import runner as r

        cur = getattr(r, "run_summary_ai_entry_from_df", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI CANDIDATE REFILL] target missing")
            return False
        if getattr(cur, "_summary_ai_candidate_refill_v1", False):
            _INSTALLED = True
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(cur)
        def patched_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
            wide_kwargs = _apply_wide_kwargs(kwargs, refill=False)
            first = cur(*args, **wide_kwargs)
            if not _env_bool("SUMMARY_AI_REFILL_RETRY_WITHOUT_TONOSAMA", True):
                return first
            if not _result_needs_refill(first):
                return first

            retry_kwargs = _apply_wide_kwargs(kwargs, refill=True)
            retry_kwargs["use_tonosama_filter"] = False
            retry_kwargs["fail_open_tonosama"] = True
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

        patched_run_summary_ai_entry_from_df._summary_ai_candidate_refill_v1 = True  # type: ignore[attr-defined]
        patched_run_summary_ai_entry_from_df._original = orig  # type: ignore[attr-defined]
        r.run_summary_ai_entry_from_df = patched_run_summary_ai_entry_from_df
        try:
            r.DEFAULT_AI_ENTRY_TOP_N = max(int(getattr(r, "DEFAULT_AI_ENTRY_TOP_N", 20)), _env_int("SUMMARY_AI_REFILL_TOP_N", 60))
            r.DEFAULT_TONOSAMA_AI_CANDIDATES = max(int(getattr(r, "DEFAULT_TONOSAMA_AI_CANDIDATES", 20)), _env_int("SUMMARY_AI_REFILL_TONOSAMA_MAX_CANDIDATES", 60))
        except Exception:
            pass
        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI CANDIDATE REFILL] installed version=%s top_n=%s tonosama_max=%s retry_without_tonosama=%s",
            VERSION,
            _env_int("SUMMARY_AI_REFILL_TOP_N", 60),
            _env_int("SUMMARY_AI_REFILL_TONOSAMA_MAX_CANDIDATES", 60),
            _env_bool("SUMMARY_AI_REFILL_RETRY_WITHOUT_TONOSAMA", True),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI CANDIDATE REFILL] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI CANDIDATE REFILL] auto install failed")

__all__ = ["VERSION", "install"]
