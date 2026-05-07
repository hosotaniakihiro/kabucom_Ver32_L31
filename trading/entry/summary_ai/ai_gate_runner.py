# ============================================================
# File   : trading/entry/summary_ai/ai_gate_runner.py
# Version: PRODUCTION-STABLE-REV3.1-AI-GATE-PER-ROW-SIDE
# ------------------------------------------------------------
# Purpose:
#   - summary候補 DataFrame を AI gate に通す
#   - BUY / SELL の side を明示して AI に渡す
#   - row側に side / ai_side がある場合は行ごとに BUY/SELL を切り替える
#   - 旧呼び出しは side="BUY" のまま動作
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from .row_adapter import convert_summary_row_to_ai_gate_row
from .utils import get_ai_final_entry_check, safe_df, safe_float, safe_str

logger = logging.getLogger(__name__)

DEFAULT_MIN_AI_CONFIDENCE = 0.65


def _append_reason(base: str, extra: str) -> str:
    base = safe_str(base, "")
    extra = safe_str(extra, "")
    if not extra:
        return base
    if not base:
        return extra
    return f"{base}|{extra}"


def _safe_bool(v: Any, default: bool = False) -> bool:
    try:
        if isinstance(v, bool):
            return v
        if v is None:
            return default
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "on", "y", "ok"}:
            return True
        if s in {"0", "false", "no", "off", "n", "ng", ""}:
            return False
        return default
    except Exception:
        return default


def _side_value(side: Any) -> str:
    s = str(side or "BUY").strip().upper()
    if s not in {"BUY", "SELL"}:
        s = "BUY"
    return s


def _row_side(row: pd.Series, default_side: str) -> str:
    for c in ("ai_side", "side", "entry_decision", "signal"):
        try:
            if c in row.index:
                v = str(row.get(c) or "").strip().upper()
                if v in {"BUY", "SELL"}:
                    return v
        except Exception:
            pass
    return _side_value(default_side)


def _get_place_entry_buy():
    try:
        from trading.handlers.entry_handler import place_entry_buy
        return place_entry_buy
    except Exception:
        logger.exception("[SUMMARY AI ENTRY] failed to import place_entry_buy")
        return None


def _inject_daily_fields_to_ai_row(ai_row: Dict[str, Any], row: pd.Series) -> Dict[str, Any]:
    for c in (
        "daily_score",
        "daily_buy_score",
        "daily_sell_score",
        "daily_ok_buy",
        "daily_ok_sell",
        "daily_exit_warn",
        "daily_reason",
        "daily_date",
    ):
        if c not in row.index:
            continue
        try:
            v = row.get(c)
            if c in {"daily_ok_buy", "daily_ok_sell", "daily_exit_warn"}:
                ai_row[c] = _safe_bool(v, False)
            elif c in {"daily_score", "daily_buy_score", "daily_sell_score"}:
                ai_row[c] = safe_float(v, 0.0)
            else:
                ai_row[c] = safe_str(v, "")
        except Exception:
            pass

    ai_row["daily_trend_score"] = safe_float(ai_row.get("daily_score"), 0.0)
    ai_row["daily_trend_ok"] = _safe_bool(ai_row.get("daily_ok_buy"), False)
    ai_row["daily_exit_risk"] = _safe_bool(ai_row.get("daily_exit_warn"), False)
    return ai_row


def run_ai_gate_for_candidates(
    candidates_df: pd.DataFrame,
    *,
    interval: int | str = 1,
    source: str = "SUMMARY",
    min_ai_confidence: float = DEFAULT_MIN_AI_CONFIDENCE,
    default_dominant_ratio: float = 1.0,
    side: str = "BUY",
    use_daily_cache: bool = True,
    daily_filter_buy: bool = False,
    daily_hard_block_exit_warn: bool = False,
    daily_min_score: Optional[float] = None,
) -> List[Dict[str, Any]]:
    df = safe_df(candidates_df)
    if df.empty:
        logger.warning(
            "[SUMMARY AI GATE] skipped empty candidates interval=%s source=%s side=%s",
            interval,
            source,
            side,
        )
        return []

    default_side = _side_value(side)
    ai_check = get_ai_final_entry_check()
    if ai_check is None:
        logger.error("[SUMMARY AI GATE] ai_final_entry_check not found side=%s", default_side)
        return []

    try:
        side_counts = df.get("ai_side", df.get("side", pd.Series([default_side] * len(df)))).astype(str).str.upper().value_counts().to_dict()
    except Exception:
        side_counts = {default_side: len(df)}

    logger.warning(
        "[SUMMARY AI GATE] SEND_TO_AI start default_side=%s rows=%s side_counts=%s interval=%s source=%s min_conf=%.2f symbols=%s",
        default_side,
        len(df),
        side_counts,
        interval,
        source,
        float(min_ai_confidence),
        list(df["symbol"].astype(str).head(40)) if "symbol" in df.columns else [],
    )

    results: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        row_side = _row_side(row, default_side)

        ai_row = convert_summary_row_to_ai_gate_row(
            row,
            interval=interval,
            source=source,
            default_dominant_ratio=default_dominant_ratio,
            side=row_side,
        )
        ai_row["side"] = row_side
        ai_row["ai_side"] = row_side
        ai_row = _inject_daily_fields_to_ai_row(ai_row, row)

        symbol = safe_str(ai_row.get("symbol"), "")
        symbolname = safe_str(ai_row.get("symbolname"), "")

        try:
            gate_result = ai_check(ai_row)
            if not isinstance(gate_result, dict):
                gate_result = {
                    "allow": False,
                    "confidence": 0.0,
                    "reason": "invalid_ai_result",
                    "model_used": "UNKNOWN",
                }
        except Exception:
            logger.exception("[SUMMARY AI GATE] AI gate failed side=%s symbol=%s", row_side, symbol)
            gate_result = {
                "allow": False,
                "confidence": 0.0,
                "reason": "ai_gate_exception",
                "model_used": "ERROR",
            }

        allow = bool(gate_result.get("allow", False))
        conf = safe_float(gate_result.get("confidence"), 0.0)
        reason = safe_str(gate_result.get("reason"), "")
        model_used = safe_str(gate_result.get("model_used"), "")

        if allow and conf < float(min_ai_confidence):
            allow = False
            reason = _append_reason(reason, f"confidence_low:{conf:.3f}<{float(min_ai_confidence):.3f}")

        if allow and row_side == "BUY" and daily_hard_block_exit_warn:
            if _safe_bool(ai_row.get("daily_exit_warn"), False):
                allow = False
                reason = _append_reason(reason, "daily_exit_warn")

        if allow and daily_min_score is not None:
            try:
                min_score = float(daily_min_score)
                if safe_float(ai_row.get("daily_score"), 0.0) < min_score:
                    allow = False
                    reason = _append_reason(reason, f"daily_score_low:{safe_float(ai_row.get('daily_score'), 0.0):.2f}<{min_score:.2f}")
            except Exception:
                pass

        item = {
            "allow": allow,
            "confidence": conf,
            "reason": reason,
            "model_used": model_used,
            "lot_multiplier": safe_float(gate_result.get("lot_multiplier"), 1.0),
            "side": row_side,
            "ai_side": row_side,
            "ai_row": ai_row,
            "source_row": dict(row),
            "symbol": symbol,
            "symbolname": symbolname,
            "buy_score": ai_row.get("buy_score"),
            "sell_score": ai_row.get("sell_score"),
            "score_total": ai_row.get("score_total"),
            "final_score": ai_row.get("final_score"),
            "close_price": ai_row.get("close_price"),
            "turnover": ai_row.get("turnover"),
            "daily_score": safe_float(ai_row.get("daily_score"), 0.0),
            "daily_buy_score": safe_float(ai_row.get("daily_buy_score"), 0.0),
            "daily_sell_score": safe_float(ai_row.get("daily_sell_score"), 0.0),
            "daily_ok_buy": _safe_bool(ai_row.get("daily_ok_buy"), False),
            "daily_ok_sell": _safe_bool(ai_row.get("daily_ok_sell"), False),
            "daily_exit_warn": _safe_bool(ai_row.get("daily_exit_warn"), False),
            "daily_reason": safe_str(ai_row.get("daily_reason"), ""),
            "daily_date": safe_str(ai_row.get("daily_date"), ""),
        }
        results.append(item)

        logger.info(
            "[SUMMARY AI GATE] AI_%s side=%s symbol=%s name=%s conf=%.3f buy=%.2f sell=%.2f total=%.2f close=%.1f reason=%s model=%s",
            "OK" if allow else "NG",
            row_side,
            symbol,
            symbolname,
            conf,
            safe_float(ai_row.get("buy_score")),
            safe_float(ai_row.get("sell_score")),
            safe_float(ai_row.get("score_total")),
            safe_float(ai_row.get("close_price")),
            reason,
            model_used,
        )

    buy_sent = len([x for x in results if str(x.get("side")).upper() == "BUY"])
    sell_sent = len([x for x in results if str(x.get("side")).upper() == "SELL"])
    buy_ok = len([x for x in results if str(x.get("side")).upper() == "BUY" and bool(x.get("allow"))])
    sell_ok = len([x for x in results if str(x.get("side")).upper() == "SELL" and bool(x.get("allow"))])

    logger.warning(
        "[SUMMARY AI GATE] SEND_TO_AI done sent=%s buy_sent=%s sell_sent=%s buy_ok=%s sell_ok=%s interval=%s source=%s",
        len(results),
        buy_sent,
        sell_sent,
        buy_ok,
        sell_ok,
        interval,
        source,
    )
    return results


def _extract_entry_values(r: Dict[str, Any]) -> Dict[str, Any]:
    ai_row = r.get("ai_row") or {}
    source_row = r.get("source_row") or {}
    symbol = ai_row.get("symbol") or r.get("symbol") or source_row.get("symbol") or ""
    symbolname = ai_row.get("symbolname") or r.get("symbolname") or source_row.get("symbolname") or ""
    price = ai_row.get("close_price") or ai_row.get("close") or r.get("close_price") or source_row.get("close")
    reason = r.get("reason") or "AI_OK"
    return {
        "symbol": str(symbol).strip(),
        "symbolname": str(symbolname),
        "price": safe_float(price, 0.0),
        "reason": str(reason),
    }


def run_push_summary_ai_entry(
    summary_df: Optional[pd.DataFrame] = None,
    *,
    df: Optional[pd.DataFrame] = None,
    interval: int | str = 1,
    interval_label: Optional[str] = None,
    source: str = "SUMMARY",
    top_n: int = 20,
    max_entries: int = 1,
    min_ai_confidence: float = DEFAULT_MIN_AI_CONFIDENCE,
    min_confidence: Optional[float] = None,
    min_conf: Optional[float] = None,
    dry_run: bool = False,
    require_market_open: bool = True,
    default_dominant_ratio: float = 1.0,
    order_type: Optional[str] = None,
    test_qty: Optional[int] = None,
    side: str = "BUY",
    **kwargs,
) -> Dict[str, Any]:
    base_df = summary_df if isinstance(summary_df, pd.DataFrame) else df
    base_df = safe_df(base_df)

    if min_confidence is not None:
        min_ai_confidence = float(min_confidence)
    if min_conf is not None:
        min_ai_confidence = float(min_conf)

    try:
        top_n = int(top_n)
    except Exception:
        top_n = 20
    if top_n <= 0:
        top_n = 20

    side_s = _side_value(side)

    logger.info(
        "[SUMMARY AI ENTRY] received rows=%s interval=%s source=%s side=%s top_n=%s max_entries=%s dry_run=%s",
        len(base_df),
        interval,
        source,
        side_s,
        top_n,
        max_entries,
        dry_run,
    )

    if base_df.empty:
        return {"candidates": [], "ai_results": [], "ai_ok": [], "approved_rows": [], "execution": {"executed": False, "orders": [], "skip_reason": "empty_df"}}

    candidates_df = base_df.head(top_n).copy()
    ai_results = run_ai_gate_for_candidates(
        candidates_df,
        interval=interval,
        source=source,
        min_ai_confidence=float(min_ai_confidence),
        default_dominant_ratio=default_dominant_ratio,
        side=side_s,
    )
    ai_ok = [r for r in ai_results if bool(r.get("allow"))]

    if side_s != "BUY":
        logger.warning(
            "[SUMMARY AI ENTRY] side=%s evaluated only; real BUY entry skipped ai_ok=%s",
            side_s,
            len(ai_ok),
        )
        return {"candidates": candidates_df, "ai_results": ai_results, "ai_ok": ai_ok, "approved_rows": [], "execution": {"executed": False, "orders": [], "skip_reason": "non_buy_side_evaluated_only"}}

    place_entry_buy = _get_place_entry_buy()
    if place_entry_buy is None:
        return {"candidates": candidates_df, "ai_results": ai_results, "ai_ok": ai_ok, "approved_rows": ai_ok, "execution": {"executed": False, "orders": [], "skip_reason": "place_entry_buy_import_failed"}}

    orders: List[Dict[str, Any]] = []
    try:
        max_entries_i = max(1, int(max_entries))
    except Exception:
        max_entries_i = 1

    for r in ai_ok[:max_entries_i]:
        if str(r.get("side", "BUY")).upper() != "BUY":
            continue
        v = _extract_entry_values(r)
        if not v["symbol"]:
            continue
        if dry_run:
            logger.warning("[SUMMARY AI ENTRY DRY_RUN] would entry BUY symbol=%s price=%.1f reason=%s", v["symbol"], v["price"], v["reason"])
            orders.append({"symbol": v["symbol"], "ok": True, "dry_run": True, "order_id": None})
            continue
        try:
            order_id = place_entry_buy(v["symbol"], v["symbolname"], v["price"], v["reason"], order_type=str(order_type or "LIMIT"), qty=int(test_qty or 100))
            orders.append({"symbol": v["symbol"], "ok": bool(order_id), "dry_run": False, "order_id": order_id})
        except Exception:
            logger.exception("[SUMMARY AI ENTRY SEND FAILED] symbol=%s", v["symbol"])
            orders.append({"symbol": v["symbol"], "ok": False, "dry_run": False, "order_id": None})

    executed = any(bool(x.get("ok")) for x in orders)
    return {"candidates": candidates_df, "ai_results": ai_results, "ai_ok": ai_ok, "approved_rows": ai_ok, "execution": {"executed": executed, "orders": orders, "skip_reason": None if executed else "no_entry_sent"}}


def run_summary_ai_entry_from_df(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def run_summary_ai_gate(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def run_ai_gate_once(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def run(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


def start(*args, **kwargs):
    return run_push_summary_ai_entry(*args, **kwargs)


__all__ = [
    "DEFAULT_MIN_AI_CONFIDENCE",
    "run_ai_gate_for_candidates",
    "run_push_summary_ai_entry",
    "run_summary_ai_entry_from_df",
    "run_summary_ai_gate",
    "run_ai_gate_once",
    "run",
    "start",
]
