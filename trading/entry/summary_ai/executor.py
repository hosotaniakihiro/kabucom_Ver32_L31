# ============================================================
# File   : trading/entry/summary_ai/executor.py
# Version: PRODUCTION-STABLE-REV1.2-BUY-RESERVED-SELECTION
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
#   - SELL候補は sell_score 優先で評価する
#   - BUY候補が存在する場合は最低1件BUYを優先採用する
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

from .utils import get_bulk_entry_pipeline, is_market_open, safe_float

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 1
DEFAULT_MIN_BUY_APPROVED = 1


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


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


def _row_side(item: Dict[str, Any]) -> str:
    try:
        ai_row = dict(item.get("ai_row") or {})
        src = dict(item.get("source_row") or {})
        return _pick_side(item, ai_row, src)
    except Exception:
        return _norm_side(item.get("side") or item.get("ai_side"), "BUY")


def _row_score_for_side(item: Dict[str, Any]) -> float:
    side = _row_side(item)
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


def _sort_key_for_selection(item: Dict[str, Any]) -> tuple[float, float, float]:
    side = _row_side(item)
    return (
        safe_float(item.get("confidence")),
        _row_score_for_side(item),
        safe_float(item.get("sell_score")) if side == "SELL" else safe_float(item.get("buy_score")),
    )


def _select_ai_ok_items(ok_items: List[Dict[str, Any]], *, max_entries: int) -> List[Dict[str, Any]]:
    """
    AI_OK候補を最終approved候補に絞る。

    従来はBUY/SELL混在で単純スコア順だったため、SELLの絶対スコアが大きいと
    BUYがAI_OKでも approved から漏れていた。

    対策:
      - BUY候補が存在する場合、最低 SUMMARY_AI_MIN_BUY_APPROVED 件はBUYから確保する
      - 残り枠はBUY/SELL混在でスコア順にする
      - max_entries=1 の場合でもBUYが存在すればBUYを優先する
    """
    if not ok_items:
        return []

    try:
        max_n = int(max_entries or DEFAULT_MAX_ENTRIES)
    except Exception:
        max_n = DEFAULT_MAX_ENTRIES
    max_n = max(1, max_n)

    min_buy = _env_int("SUMMARY_AI_MIN_BUY_APPROVED", DEFAULT_MIN_BUY_APPROVED)
    min_buy = max(0, min(min_buy, max_n))

    sorted_all = sorted(ok_items, key=_sort_key_for_selection, reverse=True)
    buy_items = [x for x in sorted_all if _row_side(x) == "BUY"]

    selected: List[Dict[str, Any]] = []
    selected_ids: set[int] = set()

    if buy_items and min_buy > 0:
        for item in buy_items[:min_buy]:
            selected.append(item)
            selected_ids.add(id(item))

    for item in sorted_all:
        if len(selected) >= max_n:
            break
        if id(item) in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(id(item))

    logger.warning(
        "[SUMMARY AI EXECUTOR] side-balanced selection max_entries=%s min_buy=%s ok_total=%s buy_ok=%s sell_ok=%s selected=%s",
        max_n,
        min_buy,
        len(ok_items),
        len(buy_items),
        len([x for x in sorted_all if _row_side(x) == "SELL"]),
        [
            {
                "symbol": x.get("symbol") or dict(x.get("ai_row") or {}).get("symbol"),
                "side": _row_side(x),
                "conf": round(safe_float(x.get("confidence")), 3),
                "score": round(_row_score_for_side(x), 3),
            }
            for x in selected
        ],
    )

    return selected


def build_approved_row(ai_ok_item: Dict[str, Any]) -> Dict[str, Any]:
    """
    entry_pipeline.py に渡す approved row を作る。
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
    ok_items = _select_ai_ok_items(ok_items, max_entries=max_entries)
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
