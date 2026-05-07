# ============================================================
# File   : scheduler_jobs/summary/summary_ai_entry_hook_v20.py
# Version: PRODUCTION-STABLE-SUMMARY-AI-ENTRY-HOOK-V20-FORCE-TOP20
# ------------------------------------------------------------
# Purpose:
#   - 定時サマリー計算後のAI判定hook
#   - PUSH由来 / RANKING由来を同じ出口AIパイプラインへ通す
#   - BUY TOP20 / SELL TOP20 を確実にAIへ渡す
#   - RANKING由来では tonosama filter / pre slope filter をOFFにする
#
# Notes:
#   - 既存 summary_ai_entry_hook.py は長大なので壊さず残す
#   - runner_core.py からこの軽量hookを呼ぶ
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import inspect
import logging
import os
from typing import Any, Callable, Optional

import pandas as pd

from .runner_utils import env_bool, env_float, env_int, is_nonempty_df

logger = logging.getLogger(__name__)

_RUNNER_CACHE: Optional[Callable[..., Any]] = None

DEFAULT_TOP_N = 20
DEFAULT_MAX_ENTRIES = 3
DEFAULT_MIN_CONFIDENCE = 0.65
DEFAULT_MIN_BUY_SCORE = 5.0
DEFAULT_MAX_SELL_SCORE = 2.0
DEFAULT_MIN_VOLUME = 1.0
DEFAULT_MIN_PRICE = 200.0
DEFAULT_MIN_SLOPE = 0.01


def _normalize_source(source: Any) -> str:
    s = str(source or "SUMMARY").strip().upper()
    return s or "SUMMARY"


def _is_ranking_source(source: Any) -> bool:
    return "RANKING" in _normalize_source(source)


def _is_tonosama_source(source: Any) -> bool:
    return "TONOSAMA" in _normalize_source(source)


def _safe_top_n() -> int:
    # 旧hookや環境変数で10が指定されても、最低20に補正する。
    return max(DEFAULT_TOP_N, env_int("SUMMARY_AI_ENTRY_TOP_N", DEFAULT_TOP_N))


def _safe_tonosama_max() -> int:
    return max(DEFAULT_TOP_N, env_int("SUMMARY_AI_ENTRY_TONOSAMA_MAX_CANDIDATES", DEFAULT_TOP_N))


def _safe_min_price() -> float:
    # 1円だと低位株が混ざりやすいため、未指定時は200円。
    return env_float("SUMMARY_AI_ENTRY_MIN_PRICE", DEFAULT_MIN_PRICE)


def _safe_min_slope() -> float:
    v = os.environ.get("SUMMARY_AI_MIN_TOP10_SLOPE")
    if v is not None and str(v).strip() != "":
        return env_float("SUMMARY_AI_MIN_TOP10_SLOPE", DEFAULT_MIN_SLOPE)
    return env_float("ENTRY_MIN_BUY_SLOPE", DEFAULT_MIN_SLOPE)


def _effective_use_tonosama_filter(source: str) -> bool:
    if _is_ranking_source(source):
        return False
    return env_bool("SUMMARY_AI_ENTRY_USE_TONOSAMA_FILTER", True)


def _effective_use_pre_slope_filter(source: str) -> bool:
    if _is_ranking_source(source):
        return False
    if _is_tonosama_source(source):
        return False
    return True


def _resolve_runner() -> Optional[Callable[..., Any]]:
    global _RUNNER_CACHE
    if _RUNNER_CACHE is not None:
        return _RUNNER_CACHE

    candidates = [
        ("trading.entry.summary_ai.runner", "run_summary_ai_entry_from_df"),
        ("trading.entry.summary_ai.runner", "run_push_summary_ai_entry"),
        ("trading.entry.summary_ai.runner", "run_ranking_summary_ai_entry"),
        ("trading.entry.summary_ai", "run_summary_ai_entry_from_df"),
        ("trading.entry.summary_ai", "run_push_summary_ai_entry"),
        ("trading.entry.summary_ai.ai_gate_runner", "run_push_summary_ai_entry"),
        ("trading.entry.summary_ai.ai_gate_runner", "run_summary_ai_entry_from_df"),
    ]

    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                _RUNNER_CACHE = fn
                logger.warning(
                    "[summary.runners] AI hook v20 runner resolved %s.%s file=%s",
                    module_name,
                    func_name,
                    getattr(mod, "__file__", None),
                )
                return fn
        except Exception:
            logger.debug(
                "[summary.runners] AI hook v20 runner resolve failed %s.%s",
                module_name,
                func_name,
                exc_info=True,
            )

    logger.error("[summary.runners] AI hook v20 runner resolve failed all candidates")
    return None


def _filter_kwargs(fn: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return dict(kwargs)
        return {k: v for k, v in kwargs.items() if k in params}
    except Exception:
        return dict(kwargs)


def _result_to_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if isinstance(result, pd.DataFrame):
        return {"candidates": result, "ai_results": result, "ai_ok": [], "approved_rows": [], "execution": {"executed": False, "skip_reason": "runner_returned_dataframe"}}
    if isinstance(result, list):
        return {"candidates": result, "ai_results": result, "ai_ok": [x for x in result if isinstance(x, dict) and x.get("allow")], "approved_rows": [], "execution": {"executed": False, "skip_reason": "runner_returned_list"}}
    return {"candidates": [], "ai_results": [], "ai_ok": [], "approved_rows": [], "execution": {"executed": False, "skip_reason": f"runner_returned_{type(result).__name__}"}}


def _len_any(v: Any) -> int:
    try:
        if v is None:
            return 0
        return len(v)
    except Exception:
        return 0


def run_summary_ai_entry_safe(
    interval: int,
    now: dt.datetime,
    df: Optional[pd.DataFrame] = None,
    *,
    source: str = "SUMMARY",
) -> bool:
    """
    PUSH/RANKING共通のAI hook。

    - top_nは最低20
    - candidates.py側でBUY TOP20 + SELL TOP20へ展開
    - ai_gate_runner.py側でAI_OK/AI_NG結果付きTOP20をコンソール表示
    """
    interval = int(interval)
    now = (now or dt.datetime.now()).replace(microsecond=0)
    source_s = _normalize_source(source)

    try:
        if not env_bool("SUMMARY_AI_ENTRY_ENABLED", True):
            logger.info(
                "[summary.runners] summary AI entry v20 skipped interval=%s source=%s reason=disabled_env",
                interval,
                source_s,
            )
            return False

        if df is None or not isinstance(df, pd.DataFrame) or not is_nonempty_df(df):
            logger.warning(
                "[summary.runners] summary AI entry v20 skipped interval=%s source=%s reason=empty_or_invalid_df type=%s",
                interval,
                source_s,
                type(df).__name__,
            )
            return False

        fn = _resolve_runner()
        if not callable(fn):
            logger.warning(
                "[summary.runners] summary AI entry v20 skipped interval=%s source=%s reason=runner_unavailable",
                interval,
                source_s,
            )
            return False

        top_n = _safe_top_n()
        max_entries = env_int("SUMMARY_AI_ENTRY_MAX_ENTRIES", DEFAULT_MAX_ENTRIES)
        min_conf = env_float("SUMMARY_AI_ENTRY_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)
        dry_run = env_bool("SUMMARY_AI_ENTRY_DRY_RUN", False)
        require_market_open = env_bool("SUMMARY_AI_ENTRY_REQUIRE_MARKET_OPEN", True)
        use_tonosama = _effective_use_tonosama_filter(source_s)
        use_pre_slope = _effective_use_pre_slope_filter(source_s)

        kwargs = {
            "summary_df": df,
            "df": df,
            "interval": interval,
            "interval_label": f"{interval}min",
            "source": source_s,
            "now": now,
            "top_n": top_n,
            "max_entries": max_entries,
            "min_ai_confidence": min_conf,
            "min_confidence": min_conf,
            "min_conf": min_conf,
            "min_buy_score": env_float("SUMMARY_AI_ENTRY_MIN_BUY_SCORE", DEFAULT_MIN_BUY_SCORE),
            "max_sell_score": env_float("SUMMARY_AI_ENTRY_MAX_SELL_SCORE", DEFAULT_MAX_SELL_SCORE),
            "min_volume": env_float("SUMMARY_AI_ENTRY_MIN_VOLUME", DEFAULT_MIN_VOLUME),
            "min_price": _safe_min_price(),
            "require_buy_target": False,
            "exclude_etf_fund": True,
            "require_market_open": require_market_open,
            "dry_run": dry_run,
            "default_dominant_ratio": 1.0,
            "use_tonosama_filter": use_tonosama,
            "tonosama_ranking_db_path": os.environ.get("SUMMARY_AI_ENTRY_TONOSAMA_RANKING_DB_PATH") or None,
            "tonosama_max_candidates": _safe_tonosama_max(),
            "fail_open_tonosama": env_bool("SUMMARY_AI_ENTRY_TONOSAMA_FAIL_OPEN", True),
            "use_pre_slope_filter": use_pre_slope,
            "min_top10_slope": _safe_min_slope(),
            "min_ranking_score": env_float("RANKING_AI_MIN_SCORE", 0.0),
            "min_ranking_momentum": env_float("RANKING_AI_MIN_MOMENTUM", 0.0),
            "use_entry_dedupe_guard": True,
            "enable_sell_ai": True,
        }

        call_kwargs = _filter_kwargs(fn, kwargs)

        logger.warning(
            "[summary.runners] summary AI entry v20 start interval=%s source=%s rows=%s runner=%s top_n=%s dry_run=%s require_market_open=%s tonosama=%s pre_slope=%s min_slope=%.4f",
            interval,
            source_s,
            len(df),
            getattr(fn, "__name__", repr(fn)),
            top_n,
            dry_run,
            require_market_open,
            use_tonosama,
            use_pre_slope,
            _safe_min_slope(),
        )

        result = fn(**call_kwargs)
        result_dict = _result_to_dict(result)

        candidates = result_dict.get("candidates") or result_dict.get("buy_candidates") or []
        ai_results = result_dict.get("ai_results") or []
        ai_ok = result_dict.get("ai_ok") or result_dict.get("buy_ai_ok") or []
        sell_ai_ok = result_dict.get("sell_ai_ok") or []
        execution = result_dict.get("execution") or {}

        logger.warning(
            "[summary.runners] summary AI entry v20 done interval=%s source=%s candidates=%s ai_results=%s ai_ok=%s sell_ai_ok=%s executed=%s skip=%s",
            interval,
            source_s,
            _len_any(candidates),
            _len_any(ai_results),
            _len_any(ai_ok),
            _len_any(sell_ai_ok),
            bool(execution.get("executed")) if isinstance(execution, dict) else False,
            execution.get("skip_reason") if isinstance(execution, dict) else None,
        )
        return True

    except Exception:
        logger.exception(
            "[summary.runners] summary AI entry v20 failed interval=%s source=%s",
            interval,
            source_s,
        )
        return False


__all__ = ["run_summary_ai_entry_safe"]
