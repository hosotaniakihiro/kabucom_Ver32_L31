# ============================================================
# File   : core/startup/summary_ai_more_candidates_patch.py
# Version: Ver1.5-SUMMARY-AI-STRICT-MIN3-CANDIDATE-REFILL
# ------------------------------------------------------------
# Purpose:
#   AIに「もっとエントリーできるか」を確認させるため、
#   SUMMARY_AI runner へ渡す候補数を起動時に拡張する。
#
# Ver1.5:
#   - strict default は 3.00 なのに、後段 runner が min_buy=4.00 で動き、
#     blowoff/low-move後の補充候補が足りなくなる問題を修正。
#   - min_buy_score / max_sell_score を「緩和」ではなく strict基準の3.00へ上限補正する。
#   - top_n / candidate_limit / max_candidates も未指定時だけ拡張する。
#
# Ver1.4:
#   - A案: SUMMARY_AI の SELL 候補を AI gate 前に short_ok=1 だけへ絞る。
#     short_ok=0 / sell_target=0 の銘柄は SELL_TOP_READY に出さない。
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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


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


def _strict_score_floor() -> float:
    return max(
        0.01,
        _env_float("SUMMARY_AI_MIN_SCORE", _env_float("SUMMARY_ENTRY_MIN_SCORE", _env_float("MIN_ENTRY_SCORE", 3.0))),
    )


def _clamp_summary_ai_scores(kwargs: dict[str, Any]) -> dict[str, tuple[float | None, float]]:
    """Keep runner thresholds aligned to strict defaults. This avoids accidental min_buy=4.00 starvation."""
    strict_min = _strict_score_floor()
    changed: dict[str, tuple[float | None, float]] = {}
    for key in ("min_buy_score", "min_buy", "min_score"):
        old_raw = kwargs.get(key)
        old = _safe_float(old_raw, 0.0) if old_raw is not None else None
        if old is None or old <= 0 or old > strict_min:
            kwargs[key] = strict_min
            changed[key] = (old, strict_min)
    for key in ("max_sell_score", "min_sell_score", "max_sell", "min_sell"):
        old_raw = kwargs.get(key)
        old = _safe_float(old_raw, 0.0) if old_raw is not None else None
        if old is None or old <= 0 or old > strict_min:
            kwargs[key] = strict_min
            changed[key] = (old, strict_min)
    return changed


def install() -> bool:
    global _INSTALLED

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

        top_n = max(60, _env_int("SUMMARY_AI_ENTRY_TOP_N", _env_int("SUMMARY_AI_TOP_N", 60)))
        tonosama_max = max(60, _env_int("SUMMARY_AI_TONOSAMA_MAX_CANDIDATES", top_n))
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

        if getattr(original, "_summary_ai_more_candidates_v15", False):
            _INSTALLED = True
            return True

        @functools.wraps(original)
        def _wrapped_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
            explicit_top_n = any(k in kwargs for k in ("top_n", "max_candidates", "candidate_limit"))
            if not explicit_top_n:
                kwargs["top_n"] = top_n
            else:
                try:
                    kwargs["top_n"] = max(int(kwargs.get("top_n") or 0), top_n)
                except Exception:
                    kwargs["top_n"] = top_n

            kwargs.setdefault("max_candidates", top_n)
            kwargs.setdefault("candidate_limit", top_n)

            if "tonosama_max_candidates" not in kwargs:
                kwargs["tonosama_max_candidates"] = tonosama_max

            if bypass_slope and "use_pre_slope_filter" not in kwargs:
                kwargs["use_pre_slope_filter"] = False

            score_changes = _clamp_summary_ai_scores(kwargs)
            logger.warning(
                "[SUMMARY AI MORE CANDIDATES PATCH] run source=%s interval=%s top_n=%s max_candidates=%s candidate_limit=%s tonosama_max=%s bypass_slope=%s explicit_top_n=%s score_changes=%s sell_short_ok_prefilter=True version=Ver1.5-SUMMARY-AI-STRICT-MIN3-CANDIDATE-REFILL",
                kwargs.get("source", "SUMMARY"),
                kwargs.get("interval", 1),
                kwargs.get("top_n"),
                kwargs.get("max_candidates"),
                kwargs.get("candidate_limit"),
                kwargs.get("tonosama_max_candidates"),
                bypass_slope,
                explicit_top_n,
                score_changes,
            )
            return original(*args, **kwargs)

        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v1 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v12 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v13 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v14 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._summary_ai_more_candidates_v15 = True  # type: ignore[attr-defined]
        _wrapped_run_summary_ai_entry_from_df._original = original  # type: ignore[attr-defined]
        runner.run_summary_ai_entry_from_df = _wrapped_run_summary_ai_entry_from_df

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI MORE CANDIDATES PATCH] installed top_n=%s tonosama_max=%s bypass_slope=%s strict_min=%.2f final_gate_relax=True direct_dispatch=True sell_short_ok_prefilter=True version=Ver1.5-SUMMARY-AI-STRICT-MIN3-CANDIDATE-REFILL",
            top_n,
            tonosama_max,
            bypass_slope,
            _strict_score_floor(),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI MORE CANDIDATES PATCH] install failed")
        return False


__all__ = ["install"]
