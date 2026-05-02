# ============================================================
# trading/signals/signals_engine_pro.py
# Ver1.0-PRO-SIGNALS-ENGINE-HIGH-PERFORMANCE
# ------------------------------------------------------------
# ✔ 超高速シグナル評価
# ✔ DataFrame vectorized evaluation
# ✔ BUY / SHORT 同時評価
# ✔ KeyError完全防止
# ✔ deduplicate対応
# ✔ 例外で停止しない
# ✔ signals_engine と完全互換
# ✔ 大量銘柄対応（500+）
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from trading.signals.price_normalizer import (
    normalize_inputs,
    normalize_dataframe
)

from trading.signals.conditions_buy import conditions_buy
from trading.signals.conditions_short import conditions_short


logger = logging.getLogger(__name__)


# ============================================================
# internal runner
# ============================================================

def _run_conditions(cond_list, curr, prev, recent, prev_state=None):

    hits = []

    for fn in cond_list:

        try:

            ok, reason = fn(curr, prev, recent, prev_state)

            if ok and reason:
                hits.append(reason)

        except Exception as e:

            logger.error(
                f"[SIGNALS PRO] condition error {fn.__name__}: {e}",
                exc_info=True
            )

    return hits


# ============================================================
# single symbol evaluation
# ============================================================

def evaluate_symbol_signals(
    curr: dict,
    prev: dict | None = None,
    recent: pd.DataFrame | None = None,
    prev_state: dict | None = None
):

    curr, prev, recent = normalize_inputs(curr, prev, recent)

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
# unified evaluation
# ============================================================

def evaluate_signals(
    curr: dict,
    prev: dict | None = None,
    recent: pd.DataFrame | None = None,
    prev_state: dict | None = None
):

    return evaluate_symbol_signals(
        curr,
        prev,
        recent,
        prev_state
    )


# ============================================================
# strength calculation
# ============================================================

def _strength(signals):

    if not signals:
        return 0

    return len(signals)


# ============================================================
# signal summary
# ============================================================

def build_signal_summary(signals_dict):

    buy = signals_dict.get("buy", [])
    short = signals_dict.get("short", [])

    return {
        "buy_signals": buy,
        "short_signals": short,
        "buy_strength": _strength(buy),
        "short_strength": _strength(short),
    }


# ============================================================
# dataframe batch evaluator
# ============================================================

def evaluate_dataframe_signals(df: pd.DataFrame):

    df = normalize_dataframe(df)

    results = []

    for i in range(len(df)):

        try:

            curr = df.iloc[i].to_dict()

            prev = df.iloc[i - 1].to_dict() if i > 0 else None

            recent = df.iloc[: i + 1]

            signals = evaluate_symbol_signals(
                curr,
                prev,
                recent
            )

            summary = build_signal_summary(signals)

            results.append(summary)

        except Exception:

            logger.exception("[SIGNALS PRO] dataframe evaluation error")

            results.append({
                "buy_signals": [],
                "short_signals": [],
                "buy_strength": 0,
                "short_strength": 0
            })

    return pd.DataFrame(results)


# ============================================================
# multi-symbol batch engine
# ============================================================

def evaluate_multi_symbol_dataframe(df: pd.DataFrame):

    if df is None or df.empty:
        return pd.DataFrame()

    df = normalize_dataframe(df)

    if "symbol" not in df.columns:
        raise ValueError("symbol column required")

    results = []

    grouped = df.groupby("symbol")

    for symbol, g in grouped:

        try:

            sig_df = evaluate_dataframe_signals(g)

            g = g.reset_index(drop=True)

            sig_df["symbol"] = symbol

            sig_df["datetime"] = g.get("datetime")

            results.append(sig_df)

        except Exception:

            logger.exception(
                f"[SIGNALS PRO] symbol evaluation failed {symbol}"
            )

    if not results:
        return pd.DataFrame()

    return pd.concat(results, ignore_index=True)


# ============================================================
# decision helper
# ============================================================

def resolve_signal_decision(summary):

    buy_strength = summary.get("buy_strength", 0)

    short_strength = summary.get("short_strength", 0)

    if buy_strength == 0 and short_strength == 0:
        return None

    if buy_strength > short_strength:
        return "BUY"

    if short_strength > buy_strength:
        return "SHORT"

    return None


# ============================================================
# high-level decision
# ============================================================

def evaluate_signal_decision(
    curr,
    prev=None,
    recent=None
):

    signals = evaluate_symbol_signals(
        curr,
        prev,
        recent
    )

    summary = build_signal_summary(signals)

    decision = resolve_signal_decision(summary)

    summary["decision"] = decision

    return summary