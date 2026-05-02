# ============================================================
# File   : trading/summary/pipeline/ai_pipeline.py
# Version: Ver3.0-PRODUCTION-HARDENED-AI-PIPELINE
# ------------------------------------------------------------
# ✔ ranking → AI entry decision
# ✔ AI/entry_gate.py 統合
# ✔ ENTRY 最終判断をAI層に統一
# ✔ confidence / lot multiplier 取得
# ✔ pullback_entry_ai 統合
# ✔ DataFrame / list 両対応
# ✔ symbol normalize
# ✔ NaN / inf 完全防御
# ✔ logger
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from AI.entry_gate import ai_final_entry_check
from trading.ai.pullback_entry_ai import detect_pullback_candidates

logger = logging.getLogger(__name__)

# ============================================================
# parameter
# ============================================================

MAX_CANDIDATES = 10


# ============================================================
# DataFrame safety
# ============================================================

def _safe_df(df):

    try:

        if df is None:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            return pd.DataFrame()

        if df.empty:
            return pd.DataFrame()

        df = df.copy()

        try:
            df = df.reset_index(drop=True)
        except Exception:
            pass

        try:
            df.replace([np.inf, -np.inf], np.nan, inplace=True)
        except Exception:
            pass

        return df

    except Exception:

        logger.exception("[ai_pipeline] safe_df failed")

        return pd.DataFrame()


# ============================================================
# row normalize
# ============================================================

def _normalize_row(row):

    try:

        if isinstance(row, dict):
            return row

        if hasattr(row, "to_dict"):
            return row.to_dict()

        return {}

    except Exception:

        return {}


# ============================================================
# symbol normalize
# ============================================================

def _normalize_symbol(row):

    try:

        symbol = str(row.get("symbol", "")).strip()

        row["symbol"] = symbol

        return row

    except Exception:

        return row


# ============================================================
# pullback filter
# ============================================================

def _apply_pullback_filter(df_entry, df_summary):

    try:

        if df_summary is None:
            return df_entry

        if not isinstance(df_summary, pd.DataFrame):
            return df_entry

        if df_summary.empty:
            return df_entry

        pullbacks = detect_pullback_candidates(df_summary)

        if pullbacks is None or pullbacks.empty:
            return df_entry

        if "symbol" not in pullbacks.columns:
            return df_entry

        pull_symbols = set(pullbacks["symbol"].astype(str))

        df_entry = df_entry[
            df_entry["symbol"].astype(str).isin(pull_symbols)
        ]

        logger.info(
            "[ai_pipeline] pullback candidates=%s",
            len(df_entry)
        )

        return df_entry

    except Exception:

        logger.exception("[ai_pipeline] pullback filter failed")

        return df_entry


# ============================================================
# AI PIPELINE
# ============================================================

def run_ai_pipeline(
    df_entry: pd.DataFrame,
    df_summary: pd.DataFrame | None,
    interval: int
) -> pd.DataFrame:

    try:

        df_entry = _safe_df(df_entry)

        if df_entry.empty:
            return pd.DataFrame()

        # ----------------------------------------------------
        # pullback entry filter
        # ----------------------------------------------------

        try:
            df_entry = _apply_pullback_filter(df_entry, df_summary)
        except Exception:
            logger.exception("[ai_pipeline] pullback stage failed")

        if df_entry.empty:

            logger.info(
                "[ai_pipeline] interval=%s pullback filtered=0",
                interval
            )

            return pd.DataFrame()

        approved = []

        # ----------------------------------------------------
        # iterate candidates
        # ----------------------------------------------------

        for _, row in df_entry.iterrows():

            try:

                row_dict = _normalize_row(row)

                row_dict = _normalize_symbol(row_dict)

                if not row_dict.get("symbol"):
                    continue

                # interval保証
                row_dict["interval"] = interval

                # ------------------------------------------------
                # AI FINAL GATE
                # ------------------------------------------------

                result = ai_final_entry_check(row_dict)

                if not result:
                    continue

                if result.get("allow") is not True:
                    continue

                # ------------------------------------------------
                # approved row
                # ------------------------------------------------

                row_out = row_dict.copy()

                row_out["ai_confidence"] = result.get(
                    "confidence",
                    0.0
                )

                row_out["lot_multiplier"] = result.get(
                    "lot_multiplier",
                    1.0
                )

                row_out["ai_reason"] = result.get(
                    "reason",
                    ""
                )

                row_out["ai_model"] = result.get(
                    "model_used",
                    ""
                )

                approved.append(row_out)

            except Exception:

                logger.exception(
                    "[ai_pipeline] row processing failed"
                )

        # ----------------------------------------------------
        # empty
        # ----------------------------------------------------

        if not approved:

            logger.info(
                "[ai_pipeline] interval=%s approved=0",
                interval
            )

            return pd.DataFrame()

        # ----------------------------------------------------
        # dataframe build
        # ----------------------------------------------------

        df_out = pd.DataFrame(approved)

        try:

            df_out.replace(
                [np.inf, -np.inf],
                np.nan,
                inplace=True
            )

            df_out.fillna(0, inplace=True)

        except Exception:
            pass

        # ----------------------------------------------------
        # ranking
        # ----------------------------------------------------

        sort_col = None

        if "ai_confidence" in df_out.columns:
            sort_col = "ai_confidence"

        elif "score" in df_out.columns:
            sort_col = "score"

        if sort_col:

            df_out = df_out.sort_values(
                sort_col,
                ascending=False
            )

        df_out = df_out.head(MAX_CANDIDATES)

        logger.info(
            "[ai_pipeline] interval=%s approved=%s",
            interval,
            len(df_out)
        )

        return df_out.reset_index(drop=True)

    except Exception:

        logger.exception(
            "[ai_pipeline] fatal interval=%s",
            interval
        )

        return pd.DataFrame()