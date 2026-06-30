# ============================================================
# File   : scheduler_jobs/summary/display_ai.py
# Function:
#   - AI passed 判定
#   - AI表示 line builder
# ------------------------------------------------------------
# Version: Ver1.1-PRODUCTION-DISPLAY-PREVIEW-FALLBACK
# ============================================================

from __future__ import annotations

import os

import pandas as pd

from .display_base import (
    first_existing,
    fmt_price,
    fmt_metric,
    fmt_confidence,
    normalize_bool,
)
from .display_reasons import coalesce_reason_text


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _num(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        if isinstance(v, str) and v.strip() in {"", "-", "nan", "None", "<NA>"}:
            return float(default)
        x = float(v)
        if pd.isna(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _has_ai_result_columns(row: pd.Series) -> bool:
    try:
        cols = (
            "ai_buy_passed", "buy_ai_passed",
            "ai_sell_passed", "sell_ai_passed",
            "ai_exit_passed", "exit_ai_passed", "passed_ai_exit",
            "ai_passed", "passed_ai", "ai_ok",
            "ai_decision", "decision", "ai_judgement", "ai_result",
            "ai_confidence", "confidence_ai", "ai_score_confidence", "ai_prob",
            "ai_reason", "reason_ai", "ai_comment", "ai_message",
        )
        for c in cols:
            if c not in row.index:
                continue
            v = row.get(c)
            try:
                if pd.isna(v):
                    continue
            except Exception:
                pass
            if str(v).strip() != "":
                return True
    except Exception:
        pass
    return False


def _preview_enabled() -> bool:
    return _env_bool("SUMMARY_DISPLAY_AI_PREVIEW_WHEN_AI_MISSING", True)


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

        if _preview_enabled() and not _has_ai_result_columns(row):
            buy = _num(first_existing(row, ["disp_buy_score", "score_buy", "buy_score", "buy"], 0.0), 0.0)
            sell = _num(first_existing(row, ["disp_sell_score", "score_sell", "sell_score", "sell"], 0.0), 0.0)
            return buy > 0.0 and buy >= sell
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

        if _preview_enabled() and not _has_ai_result_columns(row):
            buy = _num(first_existing(row, ["disp_buy_score", "score_buy", "buy_score", "buy"], 0.0), 0.0)
            sell = _num(first_existing(row, ["disp_sell_score", "score_sell", "sell_score", "sell"], 0.0), 0.0)
            return sell > 0.0 and sell >= buy
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


def _decision_text(row: pd.Series, side: str) -> str:
    decision = first_existing(row, ["ai_decision_view", "ai_decision", "decision", "ai_result"], "")
    if decision:
        return str(decision)
    if _preview_enabled() and not _has_ai_result_columns(row):
        return f"PREVIEW_{str(side).upper()}"
    return ""


def _confidence_text(row: pd.Series):
    conf = first_existing(row, ["ai_confidence_view", "ai_confidence"], pd.NA)
    try:
        if not pd.isna(conf):
            return conf
    except Exception:
        pass
    if _preview_enabled() and not _has_ai_result_columns(row):
        return 0.0
    return conf


def _preview_reason(row: pd.Series, base_reason: str) -> str:
    if _preview_enabled() and not _has_ai_result_columns(row):
        if base_reason and base_reason != "-":
            return "display_preview / " + str(base_reason)
        return "display_preview"
    return base_reason


def build_ai_buy_line(i: int, row: pd.Series) -> str:
    symbol = first_existing(row, ["symbol"], "")
    symbolname = first_existing(row, ["symbolname_view", "symbolname", "name"], "")
    close = first_existing(row, ["disp_close", "close", "close_price", "current_price"], pd.NA)
    score = first_existing(row, ["disp_score", "score", "display_score", "final_score"], pd.NA)
    total = first_existing(row, ["disp_total_score", "score_total", "total_score"], pd.NA)
    slope = first_existing(row, ["disp_slope", "slope", "score_slope"], pd.NA)
    mtf = first_existing(row, ["disp_mtf", "mtf", "score_mtf"], pd.NA)
    decision = _decision_text(row, "BUY")
    conf = _confidence_text(row)
    reason = coalesce_reason_text(
        first_existing(row, ["buy_reason_ja_view"], "-"),
        first_existing(row, ["ai_reason_view"], "-"),
        default="-",
    )
    reason = _preview_reason(row, reason)

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
    decision = _decision_text(row, "SELL")
    conf = _confidence_text(row)
    reason = coalesce_reason_text(
        first_existing(row, ["sell_reason_ja_view"], "-"),
        first_existing(row, ["ai_reason_view"], "-"),
        default="-",
    )
    reason = _preview_reason(row, reason)

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
