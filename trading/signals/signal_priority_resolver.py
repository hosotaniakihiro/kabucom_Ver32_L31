# ============================================================
# trading/signals/signal_priority_resolver.py
# Ver1.0-PRODUCTION-SIGNAL-PRIORITY-RESOLVER
# ------------------------------------------------------------
# ✔ BUY / SHORT 同時シグナル解決
# ✔ 強度ベース判定
# ✔ score / ranking 連携
# ✔ state連携
# ✔ decision出力
# ✔ signals_pipeline / engine 互換
# ============================================================

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ============================================================
# strength helper
# ============================================================

def _signal_strength(signals):

    if not signals:
        return 0

    return len(signals)


# ============================================================
# resolve decision
# ============================================================

def resolve_signal_decision(
    buy_signals: list[str] | None = None,
    short_signals: list[str] | None = None,
    score_buy: float | None = None,
    score_short: float | None = None,
    ranking_buy: float | None = None,
    ranking_short: float | None = None
):

    try:

        buy_strength = _signal_strength(buy_signals)
        short_strength = _signal_strength(short_signals)

        # ----------------------------------------------------
        # no signals
        # ----------------------------------------------------

        if buy_strength == 0 and short_strength == 0:

            return None

        # ----------------------------------------------------
        # signal count
        # ----------------------------------------------------

        if buy_strength > short_strength:

            return "BUY"

        if short_strength > buy_strength:

            return "SHORT"

        # ----------------------------------------------------
        # score comparison
        # ----------------------------------------------------

        if score_buy is not None and score_short is not None:

            if score_buy > score_short:

                return "BUY"

            if score_short > score_buy:

                return "SHORT"

        # ----------------------------------------------------
        # ranking comparison
        # ----------------------------------------------------

        if ranking_buy is not None and ranking_short is not None:

            if ranking_buy < ranking_short:

                return "BUY"

            if ranking_short < ranking_buy:

                return "SHORT"

        # ----------------------------------------------------
        # fallback
        # ----------------------------------------------------

        return None

    except Exception:

        logger.exception("[SignalPriorityResolver] decision failed")

        return None


# ============================================================
# summary decision helper
# ============================================================

def resolve_from_summary(summary: dict):

    if summary is None:

        return None

    try:

        return resolve_signal_decision(

            buy_signals=summary.get("buy_signals"),
            short_signals=summary.get("short_signals"),

            score_buy=summary.get("score_buy"),
            score_short=summary.get("score_short"),

            ranking_buy=summary.get("ranking_buy"),
            ranking_short=summary.get("ranking_short")

        )

    except Exception:

        logger.exception("[SignalPriorityResolver] summary resolve failed")

        return None


# ============================================================
# batch resolver
# ============================================================

def resolve_dataframe_decisions(df):

    if df is None or len(df) == 0:

        return df

    decisions = []

    for _, row in df.iterrows():

        try:

            decision = resolve_signal_decision(

                buy_signals=row.get("buy_signals"),
                short_signals=row.get("short_signals"),

                score_buy=row.get("score_buy"),
                score_short=row.get("score_short"),

                ranking_buy=row.get("ranking_buy"),
                ranking_short=row.get("ranking_short")

            )

        except Exception:

            logger.exception("[SignalPriorityResolver] dataframe resolve failed")

            decision = None

        decisions.append(decision)

    df["decision"] = decisions

    return df


# ============================================================
# final entry decision
# ============================================================

def resolve_entry_signal(summary: dict):

    decision = resolve_from_summary(summary)

    if decision is None:

        return {
            "decision": None,
            "entry": False
        }

    return {
        "decision": decision,
        "entry": True
    }