# ============================================================
# File   : trading/entry/summary_ai/executor.py
# Version: PRODUCTION-STABLE-REV1.0-SUMMARY-AI-EXECUTOR
# ------------------------------------------------------------
# 【概要】
#   AI_OK 銘柄を approved_rows に変換し、
#   既存の entry_pipeline.py へまとめて渡す。
#
# 【接続先】
#   trading.summary.pipeline.entry_pipeline.run_entry_pipeline(
#       approved_rows,
#       df_summary,
#       interval,
#   )
#
# 【重要】
#   - dry_run=True の場合は実行せずログのみ
#   - pending_entries / 注文API は直接触らない
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

from .utils import get_bulk_entry_pipeline, is_market_open, safe_float

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 1


def build_approved_row(ai_ok_item: Dict[str, Any]) -> Dict[str, Any]:
    """
    entry_pipeline.py に渡す approved row を作る。

    entry_pipeline.py 側は rows を DataFrame 化して
    run_summary_entry_executor(df_exec, df_summary, interval)
    に渡すため、元のサマリー情報と AI判定情報の両方を入れる。
    """
    ai_row = dict(ai_ok_item.get("ai_row") or {})
    src = dict(ai_ok_item.get("source_row") or {})

    row = dict(src)

    row.update(
        {
            "symbol": ai_ok_item.get("symbol") or ai_row.get("symbol"),
            "symbolname": ai_ok_item.get("symbolname") or ai_row.get("symbolname"),
            "side": "BUY",
            "entry_decision": "BUY",
            "source": ai_row.get("source", src.get("source", "SUMMARY")),
            "interval": ai_row.get("interval", src.get("interval", 1)),

            "price": ai_row.get("close_price") or ai_row.get("price"),
            "close_price": ai_row.get("close_price") or ai_row.get("price"),
            "confidence": ai_ok_item.get("confidence", 0.0),
            "ai_confidence": ai_ok_item.get("confidence", 0.0),
            "lot_multiplier": ai_ok_item.get("lot_multiplier", 1.0),
            "ai_reason": ai_ok_item.get("reason", ""),
            "reason": ai_ok_item.get("reason", ""),
            "model_used": ai_ok_item.get("model_used", ""),

            "score_total": ai_row.get("score_total"),
            "buy_score": ai_row.get("buy_score"),
            "sell_score": ai_row.get("sell_score"),
            "score_buy": ai_row.get("buy_score"),
            "score_sell": ai_row.get("sell_score"),
            "final_score": ai_row.get("final_score"),
            "turnover": ai_row.get("turnover"),
            "datetime": ai_row.get("datetime"),
            "ai_gate_allow": True,
        }
    )

    return row


def build_ai_ok_approved_rows(
    ai_results: Sequence[Dict[str, Any]],
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> List[Dict[str, Any]]:
    ok_items = [x for x in ai_results if bool(x.get("allow"))]

    ok_items = sorted(
        ok_items,
        key=lambda x: (
            safe_float(x.get("confidence")),
            safe_float(x.get("buy_score")),
            safe_float(x.get("score_total")),
        ),
        reverse=True,
    )

    if max_entries is not None and int(max_entries) > 0:
        ok_items = ok_items[: int(max_entries)]

    return [build_approved_row(x) for x in ok_items]


def execute_ai_ok_entries_bulk(
    ai_results: Sequence[Dict[str, Any]],
    *,
    df_summary: pd.DataFrame,
    interval: int | str = 1,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    dry_run: bool = True,
    require_market_open: bool = True,
    entry_pipeline: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """
    AI_OK銘柄をまとめて entry_pipeline.py へ渡す。

    dry_run=True:
      実行せず、approved_rows を返す。

    dry_run=False:
      run_entry_pipeline(approved_rows, df_summary, interval) を呼ぶ。
    """
    approved_rows = build_ai_ok_approved_rows(
        ai_results,
        max_entries=max_entries,
    )

    if not approved_rows:
        logger.info("[SUMMARY AI EXECUTOR] no AI_OK approved rows")
        return {
            "executed": False,
            "dry_run": dry_run,
            "approved_rows": [],
            "result": None,
            "skip_reason": "no_ai_ok",
        }

    if require_market_open and not is_market_open():
        logger.warning(
            "[SUMMARY AI EXECUTOR] market closed; bulk entry skipped approved=%s dry_run=%s",
            len(approved_rows),
            dry_run,
        )
        return {
            "executed": False,
            "dry_run": dry_run,
            "approved_rows": approved_rows,
            "result": None,
            "skip_reason": "market_closed",
        }

    if dry_run:
        for row in approved_rows:
            logger.info(
                "[SUMMARY AI EXECUTOR] DRY_RUN approved symbol=%s name=%s conf=%.3f lot=%.2f price=%s reason=%s",
                row.get("symbol"),
                row.get("symbolname"),
                safe_float(row.get("ai_confidence")),
                safe_float(row.get("lot_multiplier"), 1.0),
                row.get("price"),
                row.get("ai_reason"),
            )

        return {
            "executed": False,
            "dry_run": True,
            "approved_rows": approved_rows,
            "result": None,
            "skip_reason": "dry_run",
        }

    if entry_pipeline is None:
        entry_pipeline = get_bulk_entry_pipeline()

    if entry_pipeline is None:
        logger.warning(
            "[SUMMARY AI EXECUTOR] bulk entry pipeline not found; skip real entry approved=%s",
            len(approved_rows),
        )
        return {
            "executed": False,
            "dry_run": False,
            "approved_rows": approved_rows,
            "result": None,
            "skip_reason": "entry_pipeline_not_found",
        }

    try:
        logger.info(
            "[SUMMARY AI EXECUTOR] REAL bulk entry start approved=%s interval=%s symbols=%s",
            len(approved_rows),
            interval,
            [str(x.get("symbol")) for x in approved_rows],
        )

        result = entry_pipeline(
            approved_rows,
            df_summary,
            interval,
        )

        logger.info(
            "[SUMMARY AI EXECUTOR] REAL bulk entry done approved=%s result=%s",
            len(approved_rows),
            result,
        )

        return {
            "executed": True,
            "dry_run": False,
            "approved_rows": approved_rows,
            "result": result,
            "skip_reason": None,
        }

    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR] REAL bulk entry failed")
        return {
            "executed": False,
            "dry_run": False,
            "approved_rows": approved_rows,
            "result": None,
            "skip_reason": "entry_exception",
        }