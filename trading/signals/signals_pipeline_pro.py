# ============================================================
# trading/signals/signals_pipeline_pro.py
# Ver1.0-PRO-SIGNALS-PIPELINE
# ------------------------------------------------------------
# ✔ ranking / scoring / signals 統合
# ✔ DataFrame batch processing
# ✔ OHLC列正規化
# ✔ BUY / SHORT 同時評価
# ✔ KeyError / NaN 防御
# ✔ 例外で停止しない
# ✔ 数百銘柄対応
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from trading.signals.price_normalizer import normalize_dataframe
from trading.signals.signals_engine_pro import evaluate_symbol_signals
from trading.signals.signals_engine_pro import build_signal_summary
from trading.signals.signals_engine_pro import resolve_signal_decision

logger = logging.getLogger(__name__)


# ============================================================
# scoring helper
# ============================================================

def _compute_basic_scores(df: pd.DataFrame) -> pd.DataFrame:

    try:

        df["score_buy"] = (
            (df["ma5"] > df["ma25"]).astype(int)
            + (df["close_price"] > df["vwap"]).astype(int)
            + (df["macd"] > df["signal"]).astype(int)
        )

        df["score_short"] = (
            (df["ma5"] < df["ma25"]).astype(int)
            + (df["close_price"] < df["vwap"]).astype(int)
            + (df["macd"] < df["signal"]).astype(int)
        )

    except Exception:

        logger.exception("[PIPELINE] scoring failed")

    return df


# ============================================================
# ranking helper
# ============================================================

def _compute_ranking(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if "score_buy" in df.columns:

            df["ranking_buy"] = (
                df.groupby("datetime")["score_buy"]
                .rank(ascending=False, method="first")
            )

        if "score_short" in df.columns:

            df["ranking_short"] = (
                df.groupby("datetime")["score_short"]
                .rank(ascending=False, method="first")
            )

    except Exception:

        logger.exception("[PIPELINE] ranking failed")

    return df


# ============================================================
# symbol processor
# ============================================================

def _process_symbol(symbol_df: pd.DataFrame) -> pd.DataFrame:

    results = []

    for i in range(len(symbol_df)):

        try:

            curr = symbol_df.iloc[i].to_dict()

            prev = (
                symbol_df.iloc[i - 1].to_dict()
                if i > 0
                else None
            )

            recent = symbol_df.iloc[: i + 1]

            signals = evaluate_symbol_signals(
                curr,
                prev,
                recent
            )

            summary = build_signal_summary(signals)

            decision = resolve_signal_decision(summary)

            summary["decision"] = decision

            results.append(summary)

        except Exception:

            logger.exception("[PIPELINE] symbol processing error")

            results.append({
                "buy_signals": [],
                "short_signals": [],
                "buy_strength": 0,
                "short_strength": 0,
                "decision": None
            })

    return pd.DataFrame(results)


# ============================================================
# pipeline main
# ============================================================

def run_signals_pipeline(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:

        return pd.DataFrame()

    df = normalize_dataframe(df)

    try:

        df = _compute_basic_scores(df)

        df = _compute_ranking(df)

    except Exception:

        logger.exception("[PIPELINE] preprocessing error")

    if "symbol" not in df.columns:

        raise ValueError("symbol column required")

    outputs = []

    grouped = df.groupby("symbol")

    for symbol, symbol_df in grouped:

        try:

            symbol_df = symbol_df.reset_index(drop=True)

            sig_df = _process_symbol(symbol_df)

            sig_df["symbol"] = symbol

            if "datetime" in symbol_df.columns:

                sig_df["datetime"] = symbol_df["datetime"]

            outputs.append(sig_df)

        except Exception:

            logger.exception(
                f"[PIPELINE] failed symbol {symbol}"
            )

    if not outputs:

        return pd.DataFrame()

    return pd.concat(outputs, ignore_index=True)


# ============================================================
# realtime helper
# ============================================================

def evaluate_latest_signal(df: pd.DataFrame):

    try:

        if df is None or len(df) == 0:

            return None

        df = normalize_dataframe(df)

        curr = df.iloc[-1].to_dict()

        prev = df.iloc[-2].to_dict() if len(df) > 1 else None

        recent = df

        signals = evaluate_symbol_signals(
            curr,
            prev,
            recent
        )

        summary = build_signal_summary(signals)

        summary["decision"] = resolve_signal_decision(summary)

        return summary

    except Exception:

        logger.exception("[PIPELINE] realtime evaluation failed")

        return None