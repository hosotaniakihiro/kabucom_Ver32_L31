# ============================================================
# File   : scheduler_jobs/summary/display_ai.py
# Function:
#   - AI passed 判定
#   - AI表示 line builder
# ------------------------------------------------------------
# Version: Ver1.0-PRODUCTION-DISPLAY-SPLIT-AI
# ============================================================

from __future__ import annotations

import pandas as pd

from .display_base import (
    first_existing,
    fmt_price,
    fmt_metric,
    fmt_confidence,
    normalize_bool,
)
from .display_reasons import coalesce_reason_text


def normalize_decision_text(v) -> str:
    try:
        if v is None or pd.isna(v):
            return ""
    except Exception:
        pass
    s = str(v).strip()
    if not s:
        return ""
    return s.upper()


def is_ai_buy_passed(row: pd.Series) -> bool:
    try:
        if normalize_bool(first_existing(row, ["ai_buy_passed", "buy_ai_passed"], False)):
            return True

        side = normalize_decision_text(first_existing(row, ["ai_side", "decision_side", "side"], ""))
        decision = normalize_decision_text(
            first_existing(row, ["ai_decision", "decision", "ai_judgement", "ai_result"], "")
        )
        generic = normalize_bool(first_existing(row, ["ai_passed", "passed_ai", "ai_ok"], False))

        if generic and side in {"", "BUY", "LONG", "ENTRY", "ENTER"}:
            return True
        if decision in {"BUY", "LONG", "ENTRY", "ENTER"}:
            return True
        if generic and decision in {"OK", "PASS", "PASSED"} and side in {"", "BUY", "LONG"}:
            return True
        return False
    except Exception:
        return False


def is_ai_sell_passed(row: pd.Series) -> bool:
    try:
        if normalize_bool(first_existing(row, ["ai_sell_passed", "sell_ai_passed"], False)):
            return True

        side = normalize_decision_text(first_existing(row, ["ai_side", "decision_side", "side"], ""))
        decision = normalize_decision_text(
            first_existing(row, ["ai_decision", "decision", "ai_judgement", "ai_result"], "")
        )
        generic = normalize_bool(first_existing(row, ["ai_passed", "passed_ai", "ai_ok"], False))

        if generic and side in {"SELL", "SHORT"}:
            return True
        if decision in {"SELL", "SHORT"}:
            return True
        return False
    except Exception:
        return False


def is_ai_exit_passed(row: pd.Series) -> bool:
    try:
        if normalize_bool(first_existing(row, ["ai_exit_passed", "exit_ai_passed", "passed_ai_exit"], False)):
            return True

        decision = normalize_decision_text(
            first_existing(
                row,
                ["ai_exit_decision", "exit_decision", "ai_decision", "decision", "ai_result"],
                "",
            )
        )

        if decision in {"EXIT", "CLOSE", "CLOSED", "TAKE_PROFIT", "LOSSCUT", "STOP", "SELL"}:
            return True
        return False
    except Exception:
        return False


def build_ai_buy_line(i: int, row: pd.Series) -> str:
    symbol = first_existing(row, ["symbol"], "")
    symbolname = first_existing(row, ["symbolname_view", "symbolname", "name"], "")
    close = first_existing(row, ["disp_close", "close", "close_price", "current_price"], pd.NA)
    score = first_existing(row, ["disp_score", "score", "display_score", "final_score"], pd.NA)
    total = first_existing(row, ["disp_total_score", "score_total", "total_score"], pd.NA)
    slope = first_existing(row, ["disp_slope", "slope", "score_slope"], pd.NA)
    mtf = first_existing(row, ["disp_mtf", "mtf", "score_mtf"], pd.NA)
    decision = first_existing(row, ["ai_decision_view", "ai_decision"], "")
    conf = first_existing(row, ["ai_confidence_view", "ai_confidence"], pd.NA)
    reason = coalesce_reason_text(
        first_existing(row, ["buy_reason_ja_view"], "-"),
        first_existing(row, ["ai_reason_view"], "-"),
        default="-",
    )

    return (
        f"{i:2d}. {str(symbol):<6} {str(symbolname):<28} "
        f"price={fmt_price(close):>7} "
        f"score={fmt_metric(score):>6} "
        f"total={fmt_metric(total):>6} "
        f"slope={fmt_metric(slope):>6} "
        f"mtf={fmt_metric(mtf):>6} "
        f"AI={str(decision):<8} "
        f"conf={fmt_confidence(conf):>5} "
        f"理由={reason}"
    )


def build_ai_sell_line(i: int, row: pd.Series) -> str:
    symbol = first_existing(row, ["symbol"], "")
    symbolname = first_existing(row, ["symbolname_view", "symbolname", "name"], "")
    close = first_existing(row, ["disp_close", "close", "close_price", "current_price"], pd.NA)
    score_sell = first_existing(row, ["disp_sell_score", "score_sell"], pd.NA)
    slope = first_existing(row, ["disp_slope", "slope", "score_slope"], pd.NA)
    mtf = first_existing(row, ["disp_mtf", "mtf", "score_mtf"], pd.NA)
    decision = first_existing(row, ["ai_decision_view", "ai_decision"], "")
    conf = first_existing(row, ["ai_confidence_view", "ai_confidence"], pd.NA)
    reason = coalesce_reason_text(
        first_existing(row, ["sell_reason_ja_view"], "-"),
        first_existing(row, ["ai_reason_view"], "-"),
        default="-",
    )

    return (
        f"{i:2d}. {str(symbol):<6} {str(symbolname):<28} "
        f"price={fmt_price(close):>7} "
        f"sell={fmt_metric(score_sell):>6} "
        f"slope={fmt_metric(slope):>6} "
        f"mtf={fmt_metric(mtf):>6} "
        f"AI={str(decision):<8} "
        f"conf={fmt_confidence(conf):>5} "
        f"理由={reason}"
    )


def build_ai_exit_line(i: int, row: pd.Series) -> str:
    symbol = first_existing(row, ["symbol"], "")
    symbolname = first_existing(row, ["symbolname_view", "symbolname", "name"], "")
    close = first_existing(row, ["disp_close", "close", "close_price", "current_price"], pd.NA)
    score_sell = first_existing(row, ["disp_sell_score", "score_sell"], pd.NA)
    slope = first_existing(row, ["disp_slope", "slope", "score_slope"], pd.NA)
    mtf = first_existing(row, ["disp_mtf", "mtf", "score_mtf"], pd.NA)
    decision = first_existing(row, ["ai_exit_decision_view", "ai_exit_decision", "ai_decision_view"], "")
    conf = first_existing(row, ["ai_exit_confidence_view", "ai_exit_confidence"], pd.NA)
    reason = coalesce_reason_text(
        first_existing(row, ["exit_reason_ja_view"], "-"),
        first_existing(row, ["ai_exit_reason_view"], "-"),
        default="-",
    )

    return (
        f"{i:2d}. {str(symbol):<6} {str(symbolname):<28} "
        f"price={fmt_price(close):>7} "
        f"sell={fmt_metric(score_sell):>6} "
        f"slope={fmt_metric(slope):>6} "
        f"mtf={fmt_metric(mtf):>6} "
        f"EXIT={str(decision):<8} "
        f"conf={fmt_confidence(conf):>5} "
        f"理由={reason}"
    )