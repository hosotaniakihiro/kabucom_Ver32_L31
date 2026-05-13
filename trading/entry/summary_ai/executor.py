# ============================================================
# File   : trading/entry/summary_ai/executor.py
# Version: PRODUCTION-STABLE-REV1.7-BUY-FIRST-MAX10-AFFORDABLE
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
#   - BUY候補が存在する場合は最大10件までBUYを優先採用する
#   - 50万円・100株単位で数量0になる高価格銘柄は選抜前に除外する
#   - trade_restricted / SELL reject cache 済み銘柄は選抜前に除外する
#   - 制限中の候補で枠を消費せず、次の候補を採用する
#   - entry_pipeline の戻り値が None の場合は executed=False にする
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

from .utils import get_bulk_entry_pipeline, is_market_open, safe_float

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 10
DEFAULT_MIN_BUY_APPROVED = 3

# 50万円・100株単位の場合、5000円超は最低100株でも50万円を超える。
# 例: 5801 58,340円 -> 100株で5,834,000円のため qty=0 になり枠を消費する。
DEFAULT_MAX_PRICE_FOR_100_SHARE_ENTRY = 5000.0


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
        return float(v)
    except Exception:
        return float(default)


def _norm_side(v: Any, default: str = "BUY") -> str:
    try:
        s = str(v or default).strip().upper()
        return s if s in {"BUY", "SELL"} else default
    except Exception:
        return default


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _pick_symbol(item: Dict[str, Any]) -> str:
    try:
        ai_row = dict(item.get("ai_row") or {})
        src = dict(item.get("source_row") or {})
        return _norm_symbol(item.get("symbol") or ai_row.get("symbol") or src.get("symbol"))
    except Exception:
        return _norm_symbol(item.get("symbol"))


def _pick_price(item: Dict[str, Any]) -> float:
    """
    AI_OK item から発注価格候補を拾う。
    ここで価格を拾えない場合は除外せず、後段の lot_sizer / order_builder に任せる。
    """
    try:
        ai_row = dict(item.get("ai_row") or {})
        src = dict(item.get("source_row") or {})

        for d in (item, ai_row, src):
            if not isinstance(d, dict):
                continue
            for key in (
                "close_price",
                "price",
                "current_price",
                "close",
                "last_price",
                "CurrentPrice",
            ):
                v = d.get(key)
                x = safe_float(v, 0.0)
                if x > 0:
                    return x
        return 0.0
    except Exception:
        return 0.0


def _max_price_for_100_share_entry() -> float:
    """
    AI候補選抜前の価格上限。

    既定:
      5000円 = 500,000円 / 100株

    無効化したい場合:
      SUMMARY_AI_ENTRY_MAX_PRICE_FOR_100_SHARE=0
    """
    return _env_float(
        "SUMMARY_AI_ENTRY_MAX_PRICE_FOR_100_SHARE",
        DEFAULT_MAX_PRICE_FOR_100_SHARE_ENTRY,
    )


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


def _is_trade_restricted_symbol(symbol: str) -> tuple[bool, Any]:
    """
    send_order.py / entry_controller が登録した取引制限を、executor 選抜前に見る。
    期限切れなら可能な範囲で解除する。
    """
    if not symbol:
        return False, None

    try:
        from global_state import global_data

        root = getattr(global_data, "trade_restricted", {}) or {}
        until = root.get(symbol)
        if not until:
            return False, None

        if isinstance(until, dt.datetime):
            if dt.datetime.now() < until:
                return True, until
            try:
                root.pop(symbol, None)
            except Exception:
                pass
            return False, None

        return True, until
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR] trade_restricted check failed symbol=%s", symbol)
        return False, None


def _is_sell_reject_cached(symbol: str, side: str) -> tuple[bool, Any]:
    if side != "SELL" or not symbol:
        return False, None

    try:
        from AI.sell_order_reject_cache import is_sell_rejected, get_sell_reject_reason

        if is_sell_rejected(symbol):
            return True, get_sell_reject_reason(symbol)
        return False, None
    except Exception:
        # cache が無い環境でも executor は止めない
        return False, None


def _filter_blocked_ai_ok_items(ok_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    AI_OK だが、直前サイクルで取引制限やSELL拒否キャッシュに該当する銘柄を除外する。
    加えて、最低100株でも50万円枠を超える高価格銘柄を選抜前に除外する。

    ここで除外してから max_entries を選ぶことで、通らない候補で枠を消費しない。
    """
    if not ok_items:
        return []

    kept: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    max_price = _max_price_for_100_share_entry()

    for item in ok_items:
        symbol = _pick_symbol(item)
        side = _row_side(item)
        price = _pick_price(item)

        if max_price > 0 and price > max_price:
            skipped.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "reason": "price_over_100share_cap",
                    "price": price,
                    "max_price": max_price,
                    "min_notional_100": round(price * 100, 1),
                }
            )
            continue

        restricted, until = _is_trade_restricted_symbol(symbol)
        if restricted:
            skipped.append({"symbol": symbol, "side": side, "reason": "trade_restricted", "until": str(until)})
            continue

        sell_rejected, reason = _is_sell_reject_cached(symbol, side)
        if sell_rejected:
            skipped.append({"symbol": symbol, "side": side, "reason": "sell_reject_cache", "detail": str(reason)})
            continue

        kept.append(item)

    if skipped:
        logger.warning(
            "[SUMMARY AI EXECUTOR] filtered blocked/unaffordable candidates before selection before=%s after=%s max_price=%s skipped=%s",
            len(ok_items),
            len(kept),
            max_price,
            skipped[:50],
        )

    return kept


def _select_ai_ok_items(ok_items: List[Dict[str, Any]], *, max_entries: int) -> List[Dict[str, Any]]:
    """
    AI_OK候補を最終approved候補に絞る。

    対策:
      - 高価格でqty=0になる候補は選抜前に除外
      - trade_restricted / SELL reject cache 済み銘柄は選抜前に除外
      - BUY候補が存在する場合、最大 max_entries 件までBUYを優先する
      - BUYが足りない場合のみSELLを補欠採用する
      - SELL高スコア候補がBUY枠を潰さないようにする
    """
    if not ok_items:
        return []

    ok_items = _filter_blocked_ai_ok_items(ok_items)
    if not ok_items:
        logger.warning("[SUMMARY AI EXECUTOR] all AI_OK candidates filtered by trade restriction / reject cache / price cap")
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
    sell_items = [x for x in sorted_all if _row_side(x) == "SELL"]

    selected: List[Dict[str, Any]] = []
    selected_ids: set[int] = set()

    # BUYがある場合は、まずBUYで枠を埋める。
    if buy_items and min_buy > 0:
        for item in buy_items[:max_n]:
            selected.append(item)
            selected_ids.add(id(item))

    # BUYが不足した場合だけSELLを補欠で入れる。
    for item in sell_items:
        if len(selected) >= max_n:
            break
        if id(item) in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(id(item))

    # 特殊ケース用: まだ枠が残るなら通常順で補充。
    for item in sorted_all:
        if len(selected) >= max_n:
            break
        if id(item) in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(id(item))

    logger.warning(
        "[SUMMARY AI EXECUTOR] buy-first selection max_entries=%s min_buy=%s ok_total=%s buy_ok=%s sell_ok=%s selected=%s",
        max_n,
        min_buy,
        len(ok_items),
        len(buy_items),
        len(sell_items),
        [
            {
                "symbol": _pick_symbol(x),
                "side": _row_side(x),
                "price": _pick_price(x),
                "conf": round(safe_float(x.get("confidence")), 3),
                "score": round(_row_score_for_side(x), 3),
            }
            for x in selected
        ],
    )

    return selected


def _is_positive_order_result(result: Any) -> bool:
    """
    entry_pipeline の戻り値から「実際に注文系処理が成功した」と判断する。

    重要:
      - result is None は成功扱いにしない
      - boolだけ返る場合はその値を使う
      - dictの場合は order_id / orders / executed などを見る
      - list/tupleの場合は中身があれば成功候補とする
    """
    try:
        if result is None:
            return False

        if isinstance(result, bool):
            return result

        if isinstance(result, dict):
            for key in (
                "executed",
                "order_sent",
                "order_submitted",
                "success",
                "approved",
                "entry_executed",
            ):
                if bool(result.get(key)):
                    return True

            for key in ("order_id", "OrderId", "orders", "order_ids", "sent_orders"):
                v = result.get(key)
                if isinstance(v, (list, tuple, set, dict)):
                    if len(v) > 0:
                        return True
                elif v:
                    return True

            return False

        if isinstance(result, (list, tuple, set)):
            return len(result) > 0

        return bool(result)

    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR] result judgement failed result=%s", result)
        return False


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
                "price": safe_float(r.get("close_price") or r.get("price"), 0.0),
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

        entry_executed = _is_positive_order_result(result)

        logger.info(
            "[SUMMARY AI EXECUTOR] REAL bulk entry done approved=%s executed=%s result=%s",
            len(approved_rows),
            entry_executed,
            result,
        )

        if not entry_executed:
            logger.warning(
                "[SUMMARY AI EXECUTOR] REAL bulk entry finished but no order confirmed approved=%s result=%s",
                len(approved_rows),
                result,
            )

        return {
            "executed": entry_executed,
            "dry_run": False,
            "approved_rows": approved_rows,
            "result": result,
            "skip_reason": None if entry_executed else "entry_pipeline_no_order",
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
