# ============================================================
# File   : scheduler_jobs/summary/display_reasons.py
# Function:
#   - 理由文生成
#   - 日本語理由列構築
# ------------------------------------------------------------
# Version: Ver1.0-PRODUCTION-DISPLAY-SPLIT-REASONS
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .display_base import safe_df, pick_text_series, first_existing

logger = logging.getLogger(__name__)

try:
    from trading.scoring.config.flag_label_map import build_reason_text_from_row
except Exception:
    build_reason_text_from_row = None


def coalesce_reason_text(*values: Any, default: str = "-") -> str:
    for v in values:
        try:
            if v is None or pd.isna(v):
                continue
        except Exception:
            pass
        s = str(v).strip()
        if s and s not in {"nan", "None", "null", "-"}:
            return s
    return default


def build_reason_series(df: pd.DataFrame, side: str = "BUY") -> pd.Series:
    out = safe_df(df)
    if out.empty:
        return pd.Series(dtype="object")

    explicit_cols = {
        "BUY": [
            "buy_reason_ja",
            "reason_buy_ja",
            "buy_reason",
            "entry_reason_ja",
            "entry_reason",
        ],
        "SELL": [
            "sell_reason_ja",
            "reason_sell_ja",
            "sell_reason",
            "short_reason_ja",
            "short_reason",
        ],
        "EXIT": [
            "exit_reason_ja",
            "reason_exit_ja",
            "exit_reason",
            "ai_exit_reason",
        ],
    }.get(str(side).upper(), [])

    explicit = pick_text_series(out, explicit_cols, default="").astype(str).str.strip()

    if callable(build_reason_text_from_row):
        try:
            auto_reason = out.apply(
                lambda row: build_reason_text_from_row(row, side=str(side).upper(), max_items=5),
                axis=1,
            ).astype(str)
            explicit = explicit.mask(explicit.eq(""), auto_reason)
        except Exception:
            logger.debug("[SUMMARY DISPLAY] build_reason_text_from_row failed side=%s", side, exc_info=True)

    explicit = explicit.fillna("").astype(str).str.strip()
    explicit = explicit.mask(explicit.eq(""), "-")
    return explicit


def build_buy_reason_line(row: pd.Series) -> str:
    reason = coalesce_reason_text(
        first_existing(row, ["buy_reason_ja_view", "buy_reason_ja", "buy_reason"], "-"),
        first_existing(row, ["ai_reason_view", "ai_reason"], "-"),
        default="-",
    )
    return f"    理由(BUY)={reason}"


def build_sell_reason_line(row: pd.Series) -> str:
    reason = coalesce_reason_text(
        first_existing(row, ["sell_reason_ja_view", "sell_reason_ja", "sell_reason"], "-"),
        first_existing(row, ["ai_reason_view", "ai_reason"], "-"),
        default="-",
    )
    return f"    理由(SELL)={reason}"


def build_exit_reason_line(row: pd.Series) -> str:
    reason = coalesce_reason_text(
        first_existing(row, ["exit_reason_ja_view", "exit_reason_ja", "exit_reason"], "-"),
        first_existing(row, ["ai_exit_reason_view", "ai_exit_reason"], "-"),
        default="-",
    )
    return f"    理由(EXIT)={reason}"