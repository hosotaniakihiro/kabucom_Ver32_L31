# ============================================================
# trading/signals/signals_engine.py
# Ver1.0-PRODUCTION-SIGNALS-ENGINE
# ------------------------------------------------------------
# ✔ BUY / SHORT 条件統合
# ✔ OHLC列自動正規化
# ✔ dict / DataFrame row 両対応
# ✔ KeyError完全防止
# ✔ 高速条件評価
# ✔ 例外で絶対停止しない
# ✔ conditions_long / conditions_short と完全互換
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from trading.signals.conditions_buy import conditions_buy
from trading.signals.conditions_short import conditions_short

logger = logging.getLogger(__name__)


# ============================================================
# OHLC normalize
# ============================================================

def _normalize_price_columns(df):

    if df is None or not isinstance(df, pd.DataFrame):
        return df

    rename_map = {}

    if "open" in df.columns and "open_price" not in df.columns:
        rename_map["open"] = "open_price"

    if "high" in df.columns and "high_price" not in df.columns:
        rename_map["high"] = "high_price"

    if "low" in df.columns and "low_price" not in df.columns:
        rename_map["low"] = "low_price"

    if "close" in df.columns and "close_price" not in df.columns:
        rename_map["close"] = "close_price"

    if rename_map:
        df = df.rename(columns=rename_map)

    return df


def _normalize_row(row):

    if row is None:
        return row

    if isinstance(row, dict):

        if "open" in row and "open_price" not in row:
            row["open_price"] = row["open"]

        if "high" in row and "high_price" not in row:
            row["high_price"] = row["high"]

        if "low" in row and "low_price" not in row:
            row["low_price"] = row["low"]

        if "close" in row and "close_price" not in row:
            row["close_price"] = row["close"]

    return row


def _normalize_inputs(curr, prev, recent):

    curr = _normalize_row(curr)
    prev = _normalize_row(prev)
    recent = _normalize_price_columns(recent)

    return curr, prev, recent


# ============================================================
# condition runner
# ============================================================

def _run_conditions(cond_list, curr, prev, recent, prev_state):

    hits = []

    for fn in cond_list:

        try:

            ok, reason = fn(curr, prev, recent, prev_state)

            if ok and reason:
                hits.append(reason)

        except Exception as e:

            logger.error(
                f"[SIGNALS] condition error {fn.__name__}: {e}",
                exc_info=True
            )

    return hits


# ============================================================
# BUY signals
# ============================================================

def evaluate_buy_signals(
    curr: dict,
    prev: dict | None = None,
    recent: pd.DataFrame | None = None,
    prev_state: dict | None = None
):

    curr, prev, recent = _normalize_inputs(curr, prev, recent)

    return _run_conditions(
        conditions_buy,
        curr,
        prev,
        recent,
        prev_state
    )


# ============================================================
# SHORT signals
# ============================================================

def evaluate_short_signals(
    curr: dict,
    prev: dict | None = None,
    recent: pd.DataFrame | None = None,
    prev_state: dict | None = None
):

    curr, prev, recent = _normalize_inputs(curr, prev, recent)

    return _run_conditions(
        conditions_short,
        curr,
        prev,
        recent,
        prev_state
    )


# ============================================================
# unified signal evaluation
# ============================================================

def evaluate_signals(
    curr: dict,
    prev: dict | None = None,
    recent: pd.DataFrame | None = None,
    prev_state: dict | None = None
):

    curr, prev, recent = _normalize_inputs(curr, prev, recent)

    buy_hits = _run_conditions(
        conditions_buy,
        curr,
        prev,
        recent,
        prev_state
    )

    short_hits = _run_conditions(
        conditions_short,
        curr,
        prev,
        recent,
        prev_state
    )

    return {
        "buy": buy_hits,
        "short": short_hits
    }


# ============================================================
# fast scoring helper
# ============================================================

def count_signal_strength(signals: list[str]) -> int:
    """
    シグナル強度（単純カウント）
    """
    if not signals:
        return 0
    return len(signals)


def build_signal_summary(signals_dict: dict):

    buy = signals_dict.get("buy", [])
    short = signals_dict.get("short", [])

    return {
        "buy_signals": buy,
        "short_signals": short,
        "buy_strength": count_signal_strength(buy),
        "short_strength": count_signal_strength(short),
    }


# ============================================================
# dataframe batch evaluator
# ============================================================

def evaluate_dataframe_signals(df: pd.DataFrame):

    df = _normalize_price_columns(df)

    results = []

    for i in range(len(df)):

        curr = df.iloc[i].to_dict()
        prev = df.iloc[i-1].to_dict() if i > 0 else None
        recent = df.iloc[:i+1]

        signals = evaluate_signals(curr, prev, recent)

        results.append(
            build_signal_summary(signals)
        )

    return pd.DataFrame(results)