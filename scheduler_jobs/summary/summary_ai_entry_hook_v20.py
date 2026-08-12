# ============================================================
# File   : scheduler_jobs/summary/summary_ai_entry_hook_v20.py
# Version: PRODUCTION-STABLE-SUMMARY-AI-ENTRY-HOOK-V25-NO-DF-BOOLEAN
# ------------------------------------------------------------
# Purpose:
#   - 定時サマリー計算後のAI判定hook
#   - PUSH由来 / RANKING由来を同じ出口AIパイプラインへ通す
#   - BUY TOP20 / SELL TOP20 を確実にAIへ渡す
#   - AI_OK=0 の原因を reason/confidence/symbol 単位でログ出力する
#
# V25 修正:
#   - result_dict.get("candidates") or ... のような DataFrame 真偽値評価を廃止
#   - pandas DataFrame が返っても ValueError: truth value is ambiguous を出さない
#   - 昼休み/市場外は runner へ require_market_open=True を渡し、直発注側の安全弁を使う
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import inspect
import logging
import os
import sys
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
    if _is_ranking_source(source):
        return False
    if _is_tonosama_source(source):
        return True
    return env_bool("SUMMARY_AI_ENTRY_USE_TONOSAMA_FILTER", False)


def _effective_use_pre_slope_filter(source: str) -> bool:
    if _is_ranking_source(source) or _is_tonosama_source(source):
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
                    "[summary.runners] AI hook v25 runner resolved %s.%s file=%s",
                    module_name,
                    func_name,
                    getattr(mod, "__file__", None),
                )
                return fn
        except Exception:
            logger.debug("[summary.runners] AI hook v25 runner resolve failed %s.%s", module_name, func_name, exc_info=True)
    logger.error("[summary.runners] AI hook v25 runner resolve failed all candidates")
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


def _result_to_dict_core(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if isinstance(result, pd.DataFrame):
        return {
            "candidates": result,
            "ai_results": [],
            "ai_ok": [],
            "approved_rows": [],
            "execution": {"executed": False, "skip_reason": "runner_returned_dataframe"},
        }
    if isinstance(result, list):
        return {
            "candidates": result,
            "ai_results": result,
            "ai_ok": [x for x in result if isinstance(x, dict) and bool(x.get("allow"))],
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


# ------------------------------------------------------------
# DataFrame truth value 正規化
# (旧 core/startup/summary_ai_entry_hook_dataframe_truth_patch.py を本文化)
#
#   _result_to_dict() の戻り値に DataFrame が残っていると、下流の
#   `x or y` / `if x:` で "truth value of a DataFrame is ambiguous" になる。
#   そこで DataFrame は records へ落とし、execution は必ず dict にする。
#
#   旧パッチは sitecustomize の BACKGROUND_MAIN_PATCHES 経由 = main.py 専用
#   (SITECUSTOMIZE_ENABLE_FULL_NONMAIN=1 の明示時を含む) で install されていた。
#   このモジュールは runner_core.py 経由で DB 系プロセスからも import されるため、
#   _df_truth_scope_enabled() で当時のスコープを保つ。
# ------------------------------------------------------------

_DF_TRUTH_KEYS_MAY_BE_DF = (
    "candidates",
    "buy_candidates",
    "sell_candidates",
    "ai_results",
    "ai_ok",
    "buy_ai_ok",
    "sell_ai_ok",
    "approved_rows",
)

_DF_TRUTH_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)


def _df_truth_env_on(name: str, default: bool = False) -> bool:
    """sitecustomize._env_on と同じ解釈。

    runner_utils.env_bool とは意図的に別実装: env_bool は空文字を False、
    "ok"/"enable"/"enabled" を default 扱いにするので、
    SITECUSTOMIZE_* の判定に流用するとスコープがずれる。
    """
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _df_truth_scope_enabled() -> bool:
    """このプロセスで result 正規化を行うか (旧 install スコープの再現)。

    SUMMARY_AI_DF_TRUTH_SCOPE_ENABLED=1/0 で明示的に上書きできる。
    """
    try:
        raw = os.getenv("SUMMARY_AI_DF_TRUTH_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _df_truth_env_on("SUMMARY_AI_DF_TRUTH_SCOPE_ENABLED", True)
        # sitecustomize._install_module の disabled_env は "1" 完全一致で判定していた
        if str(os.environ.get("DISABLE_SUMMARY_AI_DF_TRUTH_PATCH", "")).strip() == "1":
            return False
        argv_parts = [str(x).replace("\\", "/").lower() for x in sys.argv]
        argv = " ".join(argv_parts)
        # sitecustomize は database context を main.py 判定より先に見る
        if any(name in argv for name in _DF_TRUTH_DB_PROCESS_NAMES):
            return False
        is_main_py = any(a.endswith("/main.py") or a == "main.py" for a in argv_parts)
        if is_main_py and _df_truth_env_on("SITECUSTOMIZE_MAIN_LITE", True):
            return True
        return _df_truth_env_on("SITECUSTOMIZE_ENABLE_FULL_NONMAIN", False)
    except Exception:
        return False


def _df_to_records(v: Any) -> Any:
    try:
        if isinstance(v, pd.DataFrame):
            if v.empty:
                return []
            try:
                if not v.columns.is_unique:
                    dup = [str(c) for c in v.columns[v.columns.duplicated()].unique().tolist()]
                    logger.warning(
                        "[SUMMARY AI HOOK DF TRUTH] duplicate columns before records dup=%s cols=%s rows=%s",
                        dup[:30], len(v.columns), len(v),
                    )
                    v = v.loc[:, ~v.columns.duplicated()].copy()
            except Exception:
                pass
            return v.to_dict(orient="records")
        if v is None:
            return []
        if isinstance(v, tuple):
            return list(v)
        return v
    except Exception:
        logger.exception("[SUMMARY AI HOOK DF TRUTH] value normalization failed type=%s", type(v).__name__)
        return [] if v is None else v


def _normalize_result_dict(d: Any) -> dict[str, Any]:
    if not isinstance(d, dict):
        return {}
    out = dict(d)
    converted: dict[str, int] = {}
    for key in _DF_TRUTH_KEYS_MAY_BE_DF:
        if key in out:
            before = out.get(key)
            after = _df_to_records(before)
            out[key] = after
            try:
                if type(before).__name__ != type(after).__name__:
                    converted[key] = len(after) if hasattr(after, "__len__") else -1
            except Exception:
                converted[key] = -1
    if not isinstance(out.get("execution"), dict):
        out["execution"] = {} if out.get("execution") is None else {"raw_execution": str(out.get("execution"))[:500]}
    if converted:
        logger.warning("[SUMMARY AI HOOK DF TRUTH] normalized result DataFrame/list fields converted=%s", converted)
    return out


def _result_to_dict(result: Any) -> dict[str, Any]:
    if not _df_truth_scope_enabled():
        return _result_to_dict_core(result)
    try:
        return _normalize_result_dict(_result_to_dict_core(result))
    except Exception:
        logger.exception("[SUMMARY AI HOOK DF TRUTH] normalized _result_to_dict failed")
        return {
            "candidates": [],
            "ai_results": [],
            "ai_ok": [],
            "sell_ai_ok": [],
            "execution": {"executed": False, "skip_reason": "result_to_dict_exception"},
        }


def _payload(result_dict: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """DataFrame/list/dict を bool 評価せずに最初に存在するpayloadを返す。"""
    for key in keys:
        if key not in result_dict:
            continue
        v = result_dict.get(key)
        if v is None:
            continue
        return v
    return [] if default is None else default


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
    for key in ("reason", "ng_reason", "skip_reason", "reject_reason", "error", "message", "ai_reason", "decision_reason"):
        v = row.get(key)
        if v is not None and str(v).strip() != "":
            return str(v).strip()[:160]
    if row.get("allow") is False:
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


def _diagnose_ai_entry_result(*, interval: int, source: str, candidates: Any, ai_results: Any, ai_ok: Any, sell_ai_ok: Any, execution: Any, min_conf: float) -> None:
    try:
        cand_rows = _records_any(candidates, limit=60)
        result_rows = _records_any(ai_results, limit=120)
        exec_dict = execution if isinstance(execution, dict) else {}
        reason_counter: Counter[str] = Counter()
        conf_low = allow_true = allow_false = unknown_allow = 0
        ng_head: list[dict[str, Any]] = []
        ok_head: list[dict[str, Any]] = []

        for row in result_rows:
            allow_raw = row.get("allow")
            allow = bool(allow_raw)
            conf = _safe_num(row.get("confidence") or row.get("conf") or row.get("ai_confidence") or row.get("score_confidence"), 0.0)
            if allow:
                allow_true += 1
                if len(ok_head) < 20:
                    ok_head.append({"symbol": _symbol_of(row), "conf": conf, "reason": _compact_reason(row)})
                continue
            if allow_raw is False:
                allow_false += 1
            else:
                unknown_allow += 1
            reason = _compact_reason(row)
            reason_counter[reason] += 1
            if conf < float(min_conf):
                conf_low += 1
            if len(ng_head) < 30:
                ng_head.append({
                    "symbol": _symbol_of(row),
                    "conf": conf,
                    "reason": reason,
                    "side": row.get("side") or row.get("signal") or row.get("entry_side"),
                    "buy_score": row.get("buy_score") or row.get("score_buy") or row.get("score"),
                    "sell_score": row.get("sell_score") or row.get("score_sell"),
                })

        cand_head = []
        for row in cand_rows[:30]:
            cand_head.append({
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
            })

        logger.warning(
            "[SUMMARY AI DIAG] interval=%s source=%s candidates=%s ai_results=%s allow_true=%s allow_false=%s allow_unknown=%s ai_ok=%s sell_ai_ok=%s executed=%s skip=%s min_conf=%.2f conf_low=%s",
            interval, source, _len_any(candidates), _len_any(ai_results), allow_true, allow_false, unknown_allow,
            _len_any(ai_ok), _len_any(sell_ai_ok), bool(exec_dict.get("executed")), exec_dict.get("skip_reason"), float(min_conf), conf_low,
        )
        logger.warning("[SUMMARY AI DIAG] interval=%s source=%s ng_reason_counts=%s", interval, source, dict(reason_counter.most_common(20)))
        logger.warning("[SUMMARY AI DIAG] interval=%s source=%s ng_head=%s", interval, source, ng_head)
        if ok_head:
            logger.warning("[SUMMARY AI DIAG] interval=%s source=%s ok_head=%s", interval, source, ok_head)
        if cand_head:
            logger.warning("[SUMMARY AI DIAG] interval=%s source=%s candidate_head=%s", interval, source, cand_head)
    except Exception:
        logger.debug("[SUMMARY AI DIAG] failed interval=%s source=%s", interval, source, exc_info=True)


def run_summary_ai_entry_safe(interval: int, now: dt.datetime, df: Optional[pd.DataFrame] = None, *, source: str = "SUMMARY") -> bool:
    interval = int(interval)
    now = (now or dt.datetime.now()).replace(microsecond=0)
    source_s = _normalize_source(source)
    try:
        if not env_bool("SUMMARY_AI_ENTRY_ENABLED", True):
            logger.info("[summary.runners] summary AI entry v25 skipped interval=%s source=%s reason=disabled_env", interval, source_s)
            return False
        if df is None or not isinstance(df, pd.DataFrame) or not is_nonempty_df(df):
            logger.warning("[summary.runners] summary AI entry v25 skipped interval=%s source=%s reason=empty_or_invalid_df type=%s", interval, source_s, type(df).__name__)
            return False
        fn = _resolve_runner()
        if not callable(fn):
            logger.warning("[summary.runners] summary AI entry v25 skipped interval=%s source=%s reason=runner_unavailable", interval, source_s)
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
            "[summary.runners] summary AI entry v25 start interval=%s source=%s rows=%s runner=%s top_n=%s max_entries=%s dry_run=%s require_market_open=%s min_conf=%.2f min_buy=%.2f max_sell=%.2f tonosama=%s pre_slope=%s min_slope=%.4f",
            interval, source_s, len(df), getattr(fn, "__name__", repr(fn)), top_n, max_entries, dry_run,
            require_market_open, min_conf, min_buy_score, max_sell_score, use_tonosama, use_pre_slope, _safe_min_slope(),
        )

        result = fn(**call_kwargs)
        result_dict = _result_to_dict(result)
        candidates = _payload(result_dict, "candidates", "buy_candidates")
        ai_results = _payload(result_dict, "ai_results")
        ai_ok = _payload(result_dict, "ai_ok", "buy_ai_ok")
        sell_ai_ok = _payload(result_dict, "sell_ai_ok")
        execution = _payload(result_dict, "execution", default={})
        if not isinstance(execution, dict):
            execution = {}

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
            "[summary.runners] summary AI entry v25 done interval=%s source=%s candidates=%s ai_results=%s ai_ok=%s sell_ai_ok=%s executed=%s skip=%s",
            interval, source_s, _len_any(candidates), _len_any(ai_results), _len_any(ai_ok), _len_any(sell_ai_ok),
            bool(execution.get("executed")), execution.get("skip_reason"),
        )
        return True
    except Exception:
        logger.exception("[summary.runners] summary AI entry v25 failed interval=%s source=%s", interval, source_s)
        return False


__all__ = ["run_summary_ai_entry_safe"]
