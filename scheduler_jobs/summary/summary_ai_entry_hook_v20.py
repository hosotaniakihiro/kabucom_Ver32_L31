# ============================================================
# File   : scheduler_jobs/summary/summary_ai_entry_hook_v20.py
# Version: PRODUCTION-STABLE-SUMMARY-AI-ENTRY-HOOK-V24-MAX10
# ------------------------------------------------------------
# Purpose:
#   - 定時サマリー計算後のAI判定hook
#   - PUSH由来 / RANKING由来を同じ出口AIパイプラインへ通す
#   - BUY TOP20 / SELL TOP20 を確実にAIへ渡す
#   - AI前段で候補が全消えしないよう、pre slope filter は既定OFF
#   - min_buy_score 既定を 5.0 -> 4.0 に緩和
#   - AI_OK=0 の原因を reason/confidence/symbol 単位でログ出力する
#   - 通常SUMMARY/PUSH/Yahoo由来では tonosama filter を既定OFF
#   - SUMMARY_AI_ENTRY_MAX_ENTRIES 既定を 3 -> 10 に変更
#
# Notes:
#   - 既存 summary_ai_entry_hook.py は長大なので壊さず残す
#   - runner_core.py / ranking_summary_jobs.py からこの軽量hookを呼ぶ
#   - 殿様イナゴは別ルートで動かし、通常SUMMARY AI entryには混ぜない
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import inspect
import logging
import os
from collections import Counter
from typing import Any, Callable, Optional

import pandas as pd

from .runner_utils import env_bool, env_float, env_int, is_nonempty_df

logger = logging.getLogger(__name__)

_RUNNER_CACHE: Optional[Callable[..., Any]] = None

DEFAULT_TOP_N = 20
DEFAULT_MAX_ENTRIES = 10
DEFAULT_MIN_CONFIDENCE = 0.65
DEFAULT_MIN_BUY_SCORE = 4.0
DEFAULT_MAX_SELL_SCORE = 2.0
DEFAULT_MIN_VOLUME = 1.0
DEFAULT_MIN_PRICE = 200.0
DEFAULT_MIN_SLOPE = 0.001


def _normalize_source(source: Any) -> str:
    s = str(source or "SUMMARY").strip().upper()
    return s or "SUMMARY"


def _is_ranking_source(source: Any) -> bool:
    return "RANKING" in _normalize_source(source)


def _is_tonosama_source(source: Any) -> bool:
    return "TONOSAMA" in _normalize_source(source)


def _safe_top_n() -> int:
    return max(DEFAULT_TOP_N, env_int("SUMMARY_AI_ENTRY_TOP_N", DEFAULT_TOP_N))


def _safe_tonosama_max() -> int:
    return max(DEFAULT_TOP_N, env_int("SUMMARY_AI_ENTRY_TONOSAMA_MAX_CANDIDATES", DEFAULT_TOP_N))


def _safe_min_price() -> float:
    return env_float("SUMMARY_AI_ENTRY_MIN_PRICE", DEFAULT_MIN_PRICE)


def _safe_min_slope() -> float:
    v = os.environ.get("SUMMARY_AI_MIN_TOP10_SLOPE")
    if v is not None and str(v).strip() != "":
        return env_float("SUMMARY_AI_MIN_TOP10_SLOPE", DEFAULT_MIN_SLOPE)
    return env_float("ENTRY_MIN_BUY_SLOPE", DEFAULT_MIN_SLOPE)


def _effective_use_tonosama_filter(source: str) -> bool:
    """
    通常SUMMARY/PUSH/Yahoo由来のAI entryでは tonosama filter を既定OFFにする。

    理由:
      - PUSHサマリーAI entryの候補抽出前に殿様フィルタを通すと、
        候補が絞られすぎる・DB参照で重くなる・AI DIAGまで進みにくい。
      - 殿様イナゴは別ルートで検知/AI判定へ回す方が安定する。

    明示的に戻したい場合だけ:
      SUMMARY_AI_ENTRY_USE_TONOSAMA_FILTER=1
    """
    if _is_ranking_source(source):
        return False
    if _is_tonosama_source(source):
        return True
    return env_bool("SUMMARY_AI_ENTRY_USE_TONOSAMA_FILTER", False)


def _effective_use_pre_slope_filter(source: str) -> bool:
    """
    エントリーが発火しない原因の多くがAI前段のslope全落ちだったため、既定OFF。

    必要なら PyCharm 環境変数で明示的にONに戻せる:
      SUMMARY_AI_ENTRY_USE_PRE_SLOPE_FILTER=1
    """
    if _is_ranking_source(source):
        return False
    if _is_tonosama_source(source):
        return False
    return env_bool("SUMMARY_AI_ENTRY_USE_PRE_SLOPE_FILTER", False)


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
                    "[summary.runners] AI hook v23 runner resolved %s.%s file=%s",
                    module_name,
                    func_name,
                    getattr(mod, "__file__", None),
                )
                return fn
        except Exception:
            logger.debug(
                "[summary.runners] AI hook v23 runner resolve failed %s.%s",
                module_name,
                func_name,
                exc_info=True,
            )

    logger.error("[summary.runners] AI hook v23 runner resolve failed all candidates")
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
        return {
            "candidates": result,
            "ai_results": result,
            "ai_ok": [],
            "approved_rows": [],
            "execution": {"executed": False, "skip_reason": "runner_returned_dataframe"},
        }
    if isinstance(result, list):
        return {
            "candidates": result,
            "ai_results": result,
            "ai_ok": [x for x in result if isinstance(x, dict) and x.get("allow")],
            "approved_rows": [],
            "execution": {"executed": False, "skip_reason": "runner_returned_list"},
        }
    return {
        "candidates": [],
        "ai_results": [],
        "ai_ok": [],
        "approved_rows": [],
        "execution": {"executed": False, "skip_reason": f"runner_returned_{type(result).__name__}"},
    }


def _len_any(v: Any) -> int:
    try:
        if v is None:
            return 0
        return len(v)
    except Exception:
        return 0


def _records_any(v: Any, *, limit: int = 200) -> list[dict[str, Any]]:
    try:
        if v is None:
            return []
        if isinstance(v, pd.DataFrame):
            return v.head(limit).to_dict(orient="records")
        if isinstance(v, list):
            return [x for x in v[:limit] if isinstance(x, dict)]
        if isinstance(v, tuple):
            return [x for x in list(v)[:limit] if isinstance(x, dict)]
    except Exception:
        return []
    return []


def _compact_reason(row: dict[str, Any]) -> str:
    for key in (
        "reason",
        "ng_reason",
        "skip_reason",
        "reject_reason",
        "error",
        "message",
        "ai_reason",
        "decision_reason",
    ):
        v = row.get(key)
        if v is not None and str(v).strip() != "":
            s = str(v).strip()
            return s[:160]
    allow = row.get("allow")
    if allow is False:
        return "allow_false_no_reason"
    return "unknown"


def _safe_num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _symbol_of(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("code") or row.get("stock_code") or "").strip()


def _diagnose_ai_entry_result(
    *,
    interval: int,
    source: str,
    candidates: Any,
    ai_results: Any,
    ai_ok: Any,
    sell_ai_ok: Any,
    execution: Any,
    min_conf: float,
) -> None:
    """
    AI_OK=0 / executed=False の理由をログで分解する。
    runner本体に手を入れず、返却payloadから原因を読む安全診断。
    """
    try:
        cand_rows = _records_any(candidates, limit=60)
        result_rows = _records_any(ai_results, limit=120)
        exec_dict = execution if isinstance(execution, dict) else {}

        reason_counter: Counter[str] = Counter()
        conf_low = 0
        allow_true = 0
        allow_false = 0
        unknown_allow = 0

        ng_head: list[dict[str, Any]] = []
        ok_head: list[dict[str, Any]] = []

        for row in result_rows:
            allow = bool(row.get("allow"))
            conf = _safe_num(
                row.get("confidence")
                or row.get("conf")
                or row.get("ai_confidence")
                or row.get("score_confidence"),
                default=0.0,
            )

            if allow:
                allow_true += 1
                if len(ok_head) < 20:
                    ok_head.append(
                        {
                            "symbol": _symbol_of(row),
                            "conf": conf,
                            "reason": _compact_reason(row),
                        }
                    )
                continue

            if row.get("allow") is False:
                allow_false += 1
            else:
                unknown_allow += 1

            reason = _compact_reason(row)
            reason_counter[reason] += 1
            if conf < float(min_conf):
                conf_low += 1

            if len(ng_head) < 30:
                ng_head.append(
                    {
                        "symbol": _symbol_of(row),
                        "conf": conf,
                        "reason": reason,
                        "side": row.get("side") or row.get("signal") or row.get("entry_side"),
                        "buy_score": row.get("buy_score") or row.get("score_buy") or row.get("score"),
                        "sell_score": row.get("sell_score") or row.get("score_sell"),
                    }
                )

        cand_head: list[dict[str, Any]] = []
        for row in cand_rows[:30]:
            cand_head.append(
                {
                    "symbol": _symbol_of(row),
                    "side": row.get("side") or row.get("signal") or row.get("entry_side"),
                    "score": row.get("score") or row.get("final_score") or row.get("display_score"),
                    "buy_score": row.get("buy_score") or row.get("score_buy"),
                    "sell_score": row.get("sell_score") or row.get("score_sell"),
                    "close": row.get("close") or row.get("close_price") or row.get("price"),
                    "slope": row.get("slope_atr_scaled") or row.get("slope") or row.get("score_slope"),
                    "rsi": row.get("rsi"),
                    "macd": row.get("macd"),
                    "mtf": row.get("mtf") or row.get("score_mtf"),
                }
            )

        logger.warning(
            "[SUMMARY AI DIAG] interval=%s source=%s candidates=%s ai_results=%s allow_true=%s allow_false=%s allow_unknown=%s ai_ok=%s sell_ai_ok=%s executed=%s skip=%s min_conf=%.2f conf_low=%s",
            interval,
            source,
            _len_any(candidates),
            _len_any(ai_results),
            allow_true,
            allow_false,
            unknown_allow,
            _len_any(ai_ok),
            _len_any(sell_ai_ok),
            bool(exec_dict.get("executed")) if isinstance(exec_dict, dict) else False,
            exec_dict.get("skip_reason") if isinstance(exec_dict, dict) else None,
            float(min_conf),
            conf_low,
        )
        logger.warning(
            "[SUMMARY AI DIAG] interval=%s source=%s ng_reason_counts=%s",
            interval,
            source,
            dict(reason_counter.most_common(20)),
        )
        logger.warning(
            "[SUMMARY AI DIAG] interval=%s source=%s ng_head=%s",
            interval,
            source,
            ng_head,
        )
        if ok_head:
            logger.warning(
                "[SUMMARY AI DIAG] interval=%s source=%s ok_head=%s",
                interval,
                source,
                ok_head,
            )
        if cand_head:
            logger.warning(
                "[SUMMARY AI DIAG] interval=%s source=%s candidate_head=%s",
                interval,
                source,
                cand_head,
            )
    except Exception:
        logger.debug(
            "[SUMMARY AI DIAG] failed interval=%s source=%s",
            interval,
            source,
            exc_info=True,
        )


def run_summary_ai_entry_safe(
    interval: int,
    now: dt.datetime,
    df: Optional[pd.DataFrame] = None,
    *,
    source: str = "SUMMARY",
) -> bool:
    interval = int(interval)
    now = (now or dt.datetime.now()).replace(microsecond=0)
    source_s = _normalize_source(source)

    try:
        if not env_bool("SUMMARY_AI_ENTRY_ENABLED", True):
            logger.info(
                "[summary.runners] summary AI entry v23 skipped interval=%s source=%s reason=disabled_env",
                interval,
                source_s,
            )
            return False

        if df is None or not isinstance(df, pd.DataFrame) or not is_nonempty_df(df):
            logger.warning(
                "[summary.runners] summary AI entry v23 skipped interval=%s source=%s reason=empty_or_invalid_df type=%s",
                interval,
                source_s,
                type(df).__name__,
            )
            return False

        fn = _resolve_runner()
        if not callable(fn):
            logger.warning(
                "[summary.runners] summary AI entry v23 skipped interval=%s source=%s reason=runner_unavailable",
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
        min_buy_score = env_float("SUMMARY_AI_ENTRY_MIN_BUY_SCORE", DEFAULT_MIN_BUY_SCORE)
        max_sell_score = env_float("SUMMARY_AI_ENTRY_MAX_SELL_SCORE", DEFAULT_MAX_SELL_SCORE)

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
            "min_buy_score": min_buy_score,
            "max_sell_score": max_sell_score,
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
            "[summary.runners] summary AI entry v23 start interval=%s source=%s rows=%s runner=%s top_n=%s max_entries=%s dry_run=%s require_market_open=%s min_conf=%.2f min_buy=%.2f max_sell=%.2f tonosama=%s pre_slope=%s min_slope=%.4f",
            interval,
            source_s,
            len(df),
            getattr(fn, "__name__", repr(fn)),
            top_n,
            max_entries,
            dry_run,
            require_market_open,
            min_conf,
            min_buy_score,
            max_sell_score,
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

        _diagnose_ai_entry_result(
            interval=interval,
            source=source_s,
            candidates=candidates,
            ai_results=ai_results,
            ai_ok=ai_ok,
            sell_ai_ok=sell_ai_ok,
            execution=execution,
            min_conf=min_conf,
        )

        logger.warning(
            "[summary.runners] summary AI entry v23 done interval=%s source=%s candidates=%s ai_results=%s ai_ok=%s sell_ai_ok=%s executed=%s skip=%s",
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
            "[summary.runners] summary AI entry v23 failed interval=%s source=%s",
            interval,
            source_s,
        )
        return False


__all__ = ["run_summary_ai_entry_safe"]
