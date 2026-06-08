# ============================================================
# File   : core/startup/summary_ai_more_candidates_patch.py
# Version: Ver1.2-SUMMARY-AI-FINAL-GATE-RELAX
# ------------------------------------------------------------
# 【目的】
#   AIに「もっとエントリーできるか」を確認させるため、
#   SUMMARY_AI runner へ渡す候補数を起動時に拡張する。
#
# Ver1.2:
#   - SUMMARY_AI / TONOSAMA の upstream 承認済み候補が、entry_controller の
#     BUY score>=5.0 旧閾値だけで最終落ちする問題を防ぐため、
#     entry_controller_final_gate_relax_patch を同時 install。
# ============================================================

from __future__ import annotations

import functools
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _install_controller_enrich_patch() -> bool:
    try:
        from core.startup.summary_controller_enrich_runtime_patch import install as install_enrich
        ok = bool(install_enrich())
        logger.warning("[SUMMARY AI MORE CANDIDATES PATCH] summary_controller_enrich_runtime_patch installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES PATCH] summary_controller_enrich_runtime_patch install failed")
        return False


def _install_final_gate_relax_patch() -> bool:
    try:
        from core.startup.entry_controller_final_gate_relax_patch import install as install_relax
        ok = bool(install_relax())
        logger.warning("[SUMMARY AI MORE CANDIDATES PATCH] entry_controller_final_gate_relax_patch installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES PATCH] entry_controller_final_gate_relax_patch install failed")
        return False


def install() -> bool:
    global _INSTALLED

    # summary_controller 側の補強は、このpatchが既に入っていても再install確認する。
    _install_controller_enrich_patch()
    _install_final_gate_relax_patch()

    if _INSTALLED:
        logger.warning("[SUMMARY AI MORE CANDIDATES PATCH] already installed")
        return True

    if not _env_bool("SUMMARY_AI_MORE_CANDIDATES_ENABLED", True):
        logger.warning("[SUMMARY AI MORE CANDIDATES PATCH] disabled by env")
        return False

    try:
        import trading.entry.summary_ai.runner as runner
        import trading.entry.summary_ai.candidates as candidates

        top_n = max(20, _env_int("SUMMARY_AI_ENTRY_TOP_N", 40))
        tonosama_max = max(20, _env_int("SUMMARY_AI_TONOSAMA_MAX_CANDIDATES", top_n))
        bypass_slope = _env_bool("SUMMARY_AI_ENTRY_BYPASS_SLOPE_FILTER", False)

        try:
            runner.DEFAULT_AI_ENTRY_TOP_N = top_n
            runner.DEFAULT_TONOSAMA_AI_CANDIDATES = tonosama_max
        except Exception:
            pass

        try:
            candidates.DEFAULT_TOP_N = top_n
        except Exception:
            pass

        original = getattr(runner, "run_summary_ai_entry_from_df", None)
        if not callable(original):
            logger.error("[SUMMARY AI MORE CANDIDATES PATCH] runner.run_summary_ai_entry_from_df not callable")
            return False

        if getattr(original, "_summary_ai_more_candidates_v12", False):
            _INSTALLED = True
            return True

        @functools.wraps(original)
        def _wrapped_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
            explicit_top_n = any(k in kwargs for k in ("top_n", "max_candidates", "candidate_limit"))
            if not explicit_top_n:
                kwargs["top_n"] = top_n

            if "tonosama_max_candidates" not in kwargs:
                kwargs["tonosama_max_candidates"] = tonosama_max

            if bypass_slope and "use_pre_slope_filter" not in kwargs:
                kwargs["use_pre_slope_filter"] = False

            logger.warning(
                "[SUMMARY AI MORE CANDIDATES PATCH] run source=%s interval=%s top_n=%s tonosama_max=%s bypass_slope=%s explicit_top_n=%s",
                kwargs.get("source", "SUMMARY"),
                kwargs.get("interval", 1),
                kwargs.get("top_n"),
                kwargs.get("tonosama_max_candidates"),
                bypass_slope,
                explicit_top_n,
            )
            return original(*args, **kwargs)

        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v1 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v12 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._original = original  # type: ignore[attr-defined]
        runner.run_summary_ai_entry_from_df = _wrapped_run_summary_ai_entry_from_df

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI MORE CANDIDATES PATCH] installed top_n=%s tonosama_max=%s bypass_slope=%s final_gate_relax=True",
            top_n,
            tonosama_max,
            bypass_slope,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES PATCH] install failed")
        return False


__all__ = ["install"]
