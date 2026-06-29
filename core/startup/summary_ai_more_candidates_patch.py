# ============================================================
# File   : core/startup/summary_ai_more_candidates_patch.py
# Version: Ver1.4-SUMMARY-AI-SELL-SHORT-OK-PREFILTER
# ------------------------------------------------------------
# 【目的】
#   AIに「もっとエントリーできるか」を確認させるため、
#   SUMMARY_AI runner へ渡す候補数を起動時に拡張する。
#
# Ver1.4:
#   - A案: SUMMARY_AI の SELL 候補を AI gate 前に short_ok=1 だけへ絞る。
#     short_ok=0 / sell_target=0 の銘柄は SELL_TOP_READY に出さない。
#
# Ver1.3:
#   - SUMMARY_AI / TONOSAMA final gate relax を同時 install。
#   - queued_async のまま実注文dispatchが薄いケースを救済する
#     summary_ai_async_direct_dispatch_patch も同時 install。
#     direct patch は watcher 付きなので、後段の async patch に上書きされても再wrapする。
# ============================================================

from __future__ import annotations

import functools
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_SELL_SHORT_OK_FILTER_INSTALLED = False


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


def _safe_symbol_list(df: Any, n: int = 20) -> list[str]:
    try:
        if df is not None and not df.empty and "symbol" in df.columns:
            return list(df["symbol"].astype(str).head(n))
    except Exception:
        pass
    return []


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


def _install_direct_dispatch_patch() -> bool:
    try:
        from core.startup.summary_ai_async_direct_dispatch_patch import install as install_direct
        ok = bool(install_direct())
        logger.warning("[SUMMARY AI MORE CANDIDATES PATCH] summary_ai_async_direct_dispatch_patch installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES PATCH] summary_ai_async_direct_dispatch_patch install failed")
        return False


def _install_sell_short_ok_filter_patch() -> bool:
    """
    A案:
      SUMMARY_AI SELL候補を作る段階で short_ok=1 の銘柄だけ残す。
      これにより 5139 のような short_ok=0 銘柄が SELL_TOP_READY -> AI gate 前段まで流れない。
    """
    global _SELL_SHORT_OK_FILTER_INSTALLED
    if _SELL_SHORT_OK_FILTER_INSTALLED:
        return True
    if not _env_bool("SUMMARY_AI_SELL_SHORT_OK_PREFILTER", True):
        logger.warning("[SUMMARY AI SELL SHORT_OK PREFILTER] disabled by env")
        return False
    try:
        import pandas as pd
        import trading.entry.summary_ai.candidates as candidates
        from AI.sell_credit_guard import can_sell_symbol

        current = getattr(candidates, "_sell_candidates_from_prepared", None)
        if not callable(current):
            logger.warning("[SUMMARY AI SELL SHORT_OK PREFILTER] target not callable")
            return False
        if getattr(current, "_summary_ai_sell_short_ok_prefilter_v1", False):
            _SELL_SHORT_OK_FILTER_INSTALLED = True
            return True

        original = current

        @functools.wraps(original)
        def _wrapped_sell_candidates_from_prepared(*args: Any, **kwargs: Any):
            out = original(*args, **kwargs)
            try:
                if not isinstance(out, pd.DataFrame) or out.empty:
                    return out
                before = len(out)
                keep_idx: list[int] = []
                skipped: list[dict[str, Any]] = []
                for idx, row in out.iterrows():
                    try:
                        row_dict = row.to_dict() if hasattr(row, "to_dict") else dict(row)
                        symbol = str(row_dict.get("symbol") or "").strip()
                        if can_sell_symbol(row_dict, default=False):
                            keep_idx.append(idx)
                        else:
                            skipped.append({"symbol": symbol, "reason": "short_ok_not_1"})
                    except Exception as e:
                        skipped.append({"symbol": str(getattr(row, "symbol", "")), "reason": f"guard_error:{e}"})
                if len(keep_idx) == before:
                    return out
                filtered = out.loc[keep_idx].copy().reset_index(drop=True) if keep_idx else out.iloc[0:0].copy()
                logger.warning(
                    "[SUMMARY AI SELL SHORT_OK PREFILTER] filtered before=%s after=%s skipped=%s kept_symbols=%s",
                    before,
                    len(filtered),
                    skipped[:20],
                    _safe_symbol_list(filtered, 20),
                )
                return filtered
            except Exception:
                logger.exception("[SUMMARY AI SELL SHORT_OK PREFILTER] failed; return original candidates")
                return out

        _wrapped_sell_candidates_from_prepared._summary_ai_sell_short_ok_prefilter_v1 = True  # type: ignore[attr-defined]
        _wrapped_sell_candidates_from_prepared._original = original  # type: ignore[attr-defined]
        candidates._sell_candidates_from_prepared = _wrapped_sell_candidates_from_prepared
        _SELL_SHORT_OK_FILTER_INSTALLED = True
        logger.warning("[SUMMARY AI SELL SHORT_OK PREFILTER] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY AI SELL SHORT_OK PREFILTER] install failed")
        return False


def install() -> bool:
    global _INSTALLED

    # これらは、このpatchが既に入っていても再install確認する。
    _install_controller_enrich_patch()
    _install_final_gate_relax_patch()
    _install_direct_dispatch_patch()
    _install_sell_short_ok_filter_patch()

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

        if getattr(original, "_summary_ai_more_candidates_v14", False):
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
                "[SUMMARY AI MORE CANDIDATES PATCH] run source=%s interval=%s top_n=%s tonosama_max=%s bypass_slope=%s explicit_top_n=%s sell_short_ok_prefilter=True",
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
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v13 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v14 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._original = original  # type: ignore[attr-defined]
        runner.run_summary_ai_entry_from_df = _wrapped_run_summary_ai_entry_from_df

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI MORE CANDIDATES PATCH] installed top_n=%s tonosama_max=%s bypass_slope=%s final_gate_relax=True direct_dispatch=True sell_short_ok_prefilter=True",
            top_n,
            tonosama_max,
            bypass_slope,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES PATCH] install failed")
        return False


__all__ = ["install"]
