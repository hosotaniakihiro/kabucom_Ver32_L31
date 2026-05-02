# ============================================================
# File   : trading/entry/entry_from_ranking_runtime.py
# Version: PRODUCTION-STABLE-REV1.0-ENTRY-FROM-RANKING-RUNTIME
# ------------------------------------------------------------
# 当日ランキング銘柄を global runtime から取得し、
# 1min / 3min / 5min サマリー計算後に AI判定→entry まで行う。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pandas as pd

from trading.ranking.runtime_symbols import get_ranking_symbols_filtered

logger = logging.getLogger(__name__)


def _is_due_for_interval(now: dt.datetime, interval: int) -> bool:
    if interval <= 1:
        return True
    return now.minute % interval == 0


def _resolve_callable(candidates):
    import importlib

    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                return fn
        except Exception:
            continue
    return None


def _run_summary_for_interval(symbols: list[str], interval: int) -> pd.DataFrame:
    """
    対象銘柄のみ summary を更新する。
    実際のプロジェクト内関数に委譲する。
    """
    runner = _resolve_callable([
        ("trading.summary.engine.summary_incremental_engine", "run_summary_for_symbols"),
        ("trading.summary.engine.summary_recovery_engine", "run_summary_for_symbols"),
        ("trading.yahoo.pipeline.complement_pipeline", "run_yahoo_mtf_summary_pipeline"),
    ])

    if runner is None:
        logger.warning("[ENTRY FROM RUNTIME] no summary runner interval=%s", interval)
        return pd.DataFrame()

    try:
        out = runner(symbols=symbols, interval=interval)
        if isinstance(out, pd.DataFrame):
            return out
        if isinstance(out, dict):
            v = out.get(interval) or out.get(f"{interval}min")
            if isinstance(v, pd.DataFrame):
                return v
    except Exception:
        logger.exception("[ENTRY FROM RUNTIME] summary failed interval=%s", interval)

    return pd.DataFrame()


def _extract_top_candidates(df: pd.DataFrame, limit: int = 10) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    sort_col = None
    for c in ["final_score", "display_score", "score", "score_total"]:
        if c in out.columns:
            sort_col = c
            break

    if sort_col is None:
        return out.head(limit)

    try:
        out = out.sort_values(sort_col, ascending=False)
    except Exception:
        pass

    return out.head(limit).copy()


def _run_ai_gate(df_candidates: pd.DataFrame, interval: int) -> pd.DataFrame:
    ai_fn = _resolve_callable([
        ("trading.ai_gate.pipeline", "run_ai_gate_for_candidates"),
        ("trading.entry.ai_gate", "run_ai_gate_for_candidates"),
    ])

    if ai_fn is None:
        logger.warning("[ENTRY FROM RUNTIME] no ai gate found interval=%s", interval)
        return df_candidates

    try:
        out = ai_fn(df_candidates, interval=interval)
        if isinstance(out, pd.DataFrame):
            return out
    except Exception:
        logger.exception("[ENTRY FROM RUNTIME] ai gate failed interval=%s", interval)

    return df_candidates


def _run_entry(df_candidates: pd.DataFrame, interval: int) -> Any:
    entry_fn = _resolve_callable([
        ("trading.entry.pipeline", "run_entry_pipeline"),
        ("trading.entry.runner", "run_entry_pipeline"),
        ("core.entry_exit_tasks", "run_entry_pipeline"),
    ])

    if entry_fn is None:
        logger.warning("[ENTRY FROM RUNTIME] no entry pipeline found interval=%s", interval)
        return None

    try:
        return entry_fn(df_candidates, interval=interval)
    except Exception:
        logger.exception("[ENTRY FROM RUNTIME] entry failed interval=%s", interval)
        return None


def run_entry_from_ranking_runtime(now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now()

    symbols = sorted(get_ranking_symbols_filtered())
    if not symbols:
        logger.warning("[ENTRY FROM RUNTIME] no ranking symbols in runtime")
        return {"ok": False, "reason": "no_symbols"}

    result: dict[str, Any] = {
        "ok": True,
        "symbols": len(symbols),
        "intervals": {},
    }

    for interval in (1, 3, 5):
        if not _is_due_for_interval(now, interval):
            continue

        summary_df = _run_summary_for_interval(symbols, interval)
        if summary_df is None or summary_df.empty:
            result["intervals"][str(interval)] = {"summary_rows": 0}
            continue

        top_df = _extract_top_candidates(summary_df, limit=10)
        ai_df = _run_ai_gate(top_df, interval=interval)
        entry_result = _run_entry(ai_df, interval=interval)

        result["intervals"][str(interval)] = {
            "summary_rows": len(summary_df),
            "top_rows": len(top_df),
            "ai_rows": len(ai_df) if isinstance(ai_df, pd.DataFrame) else 0,
            "entry_result": entry_result,
        }

    logger.info(
        "[ENTRY FROM RUNTIME] done symbols=%s intervals=%s",
        len(symbols),
        list(result["intervals"].keys()),
    )
    return result