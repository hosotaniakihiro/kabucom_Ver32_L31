# ============================================================
# File   : trading/entry/summary_ai/executor.py
# Version: PRODUCTION-STABLE-REV1.1-SIDE-AWARE-SUMMARY-AI-EXECUTOR
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
#   - AI gate で決まった BUY / SELL side を絶対に破壊しない
#   - SELL候補は sell_score 優先で approved を選ぶ
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

from .utils import get_bulk_entry_pipeline, is_market_open, safe_float

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 1


def _norm_side(v: Any, default: str = "BUY") -> str:
    try:
        s = str(v or default).strip().upper()
        return s if s in {"BUY", "SELL"} else default
    except Exception:
        return default


def _pick_side(ai_ok_item: Dict[str, Any], ai_row: Dict[str, Any], src: Dict[str, Any]) -> str:
    return _norm_side(
        ai_ok_item.get("side")
        or ai_ok_item.get("ai_side")
        or ai_row.get("side")
        or ai_row.get("ai_side")
        or ai_row.get("entry_decision")
        or src.get("side")
        or src.get("ai_side")
        or src.get("entry_decision"),
        "BUY",
    )


def _row_score_for_side(item: Dict[str, Any]) -> float:
    side = _norm_side(item.get("side") or item.get("ai_side"), "BUY")
    if side == "SELL":
        return max(
            safe_float(item.get("sell_score")),
            abs(safe_float(item.get("score_total"))),
            abs(safe_float(item.get("final_score"))),
        )
    return max(
        safe_float(item.get("buy_score")),
        safe_float(item.get("score_total")),
        safe_float(item.get("final_score")),
    )


def build_approved_row(ai_ok_item: Dict[str, Any]) -> Dict[str, Any]:
    """
    entry_pipeline.py に渡す approved row を作る。

    entry_pipeline.py 側は rows を DataFrame 化して
    run_summary_entry_executor(df_exec, df_summary, interval)
    に渡すため、元のサマリー情報と AI判定情報の両方を入れる。

    重要:
      旧版は side / entry_decision を BUY 固定で入れていた。
      そのため AI gate では SELL AI_OK なのに、entry_pipeline では BUY として扱われ、
      SUMMARY_ENTRY_MIN_LIQUIDITY_SCORE_BUY=3.0 に引っかかって全落ちしていた。
    """
    ai_row = dict(ai_ok_item.get("ai_row") or {})
    src = dict(ai_ok_item.get("source_row") or {})

    side = _pick_side(ai_ok_item, ai_row, src)

    row = dict(src)

    buy_score = ai_row.get("buy_score", ai_ok_item.get("buy_score"))
    sell_score = ai_row.get("sell_score", ai_ok_item.get("sell_score"))
    score_total = ai_row.get("score_total", ai_ok_item.get("score_total"))
    final_score = ai_row.get("final_score", ai_ok_item.get("final_score"))

    row.update(
        {
            "symbol": ai_ok_item.get("symbol") or ai_row.get("symbol"),
            "symbolname": ai_ok_item.get("symbolname") or ai_row.get("symbolname"),
            "side": side,
            "ai_side": side,
            "entry_decision": side,
            "source": ai_row.get("source", src.get("source", "SUMMARY")),
            "interval": ai_row.get("interval", src.get("interval", 1)),

            "price": ai_row.get("close_price") or ai_row.get("price"),
            "close_price": ai_row.get("close_price") or ai_row.get("price"),
            "close": ai_row.get("close_price") or ai_row.get("price"),
            "confidence": ai_ok_item.get("confidence", 0.0),
            "ai_confidence": ai_ok_item.get("confidence", 0.0),
            "lot_multiplier": ai_ok_item.get("lot_multiplier", 1.0),
            "ai_reason": ai_ok_item.get("reason", ""),
            "reason": ai_ok_item.get("reason", ""),
            "model_used": ai_ok_item.get("model_used", ""),

            "score_total": score_total,
            "total_score": score_total,
            "score": score_total,
            "buy_score": buy_score,
            "sell_score": sell_score,
            "score_buy": buy_score,
            "score_sell": sell_score,
            "final_score": final_score,
            "display_score": final_score,
            "turnover": ai_row.get("turnover"),
            "volume": ai_row.get("volume", src.get("volume")),
            "datetime": ai_row.get("datetime"),
            "entry_type": ai_row.get("entry_type") or src.get("entry_type") or "SUMMARY_AI",
            "ai_gate_allow": True,
        }
    )

    logger.info(
        "[SUMMARY AI EXECUTOR] approved row built symbol=%s side=%s conf=%.3f buy=%.3f sell=%.3f total=%.3f close=%s",
        row.get("symbol"),
        side,
        safe_float(row.get("ai_confidence")),
        safe_float(row.get("buy_score")),
        safe_float(row.get("sell_score")),
        safe_float(row.get("score_total")),
        row.get("close_price"),
    )

    return row


def build_ai_ok_approved_rows(
    ai_results: Sequence[Dict[str, Any]],
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> List[Dict[str, Any]]:
    ok_items = [x for x in ai_results if isinstance(x, dict) and bool(x.get("allow"))]

    # BUY / SELL を混ぜる場合も、sideごとの有効スコアを優先して選ぶ。
    # SELLは sell_score / abs(score_total) を見る。BUYは buy_score / score_total を見る。
    ok_items = sorted(
        ok_items,
        key=lambda x: (
            safe_float(x.get("confidence")),
            _row_score_for_side(x),
            safe_float(x.get("sell_score")) if _norm_side(x.get("side") or x.get("ai_side"), "BUY") == "SELL" else safe_float(x.get("buy_score")),
        ),
        reverse=True,
    )

    if max_entries is not None and int(max_entries) > 0:
        ok_items = ok_items[: int(max_entries)]

    approved = [build_approved_row(x) for x in ok_items]

    logger.info(
        "[SUMMARY AI EXECUTOR] approved selection max_entries=%s rows=%s selected=%s",
        max_entries,
        len(approved),
        [
            {
                "symbol": r.get("symbol"),
                "side": r.get("side"),
                "buy": round(safe_float(r.get("buy_score")), 3),
                "sell": round(safe_float(r.get("sell_score")), 3),
                "total": round(safe_float(r.get("score_total")), 3),
            }
            for r in approved
        ],
    )

    return approved


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
                "[SUMMARY AI EXECUTOR] DRY_RUN approved symbol=%s side=%s name=%s conf=%.3f lot=%.2f price=%s reason=%s",
                row.get("symbol"),
                row.get("side"),
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
            "[SUMMARY AI EXECUTOR] REAL bulk entry start approved=%s interval=%s symbols=%s sides=%s",
            len(approved_rows),
            interval,
            [str(x.get("symbol")) for x in approved_rows],
            [str(x.get("side")) for x in approved_rows],
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
