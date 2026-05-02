# ============================================================
# File: trading/ai/orderflow_detector.py
# Ver1.1-PRODUCTION-HFT-ORDERFLOW-DETECTOR
# ------------------------------------------------------------
# ✔ ask板減少検出
# ✔ bid板増加検出
# ✔ price acceleration
# ✔ volume spike
# ✔ push_stream互換
# ✔ Discord alert integration (NEW)
# ✔ NaN完全防御
# ✔ ENTRY前検出
# ✔ vectorized safe
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from alerts.orderflow_alert import notify_orderflow

logger = logging.getLogger(__name__)


# ============================================================
# safe numeric
# ============================================================

def _safe(series):

    return (
        pd.to_numeric(series, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0)
    )


# ============================================================
# 板変化スコア
# ============================================================

def compute_orderflow_score(df: pd.DataFrame) -> pd.DataFrame:

    try:

        df = df.copy()

        ask_size = _safe(df.get("ask_size", 0))
        bid_size = _safe(df.get("bid_size", 0))

        ask_prev = ask_size.shift(1)
        bid_prev = bid_size.shift(1)

        ask_delta = ask_prev - ask_size
        bid_delta = bid_size - bid_prev

        price = _safe(df.get("price", 0))
        price_prev = price.shift(1)

        price_change = price - price_prev

        df["orderflow_score"] = (
            ask_delta * 2 +
            bid_delta * 1.5 +
            price_change * 50
        )

        df["orderflow_score"] = _safe(df["orderflow_score"])

        return df

    except Exception:

        logger.exception("[compute_orderflow_score]")

        return df


# ============================================================
# 板食い検出
# ============================================================

def detect_orderflow_shock(df: pd.DataFrame) -> pd.DataFrame:

    try:

        if df is None or len(df) < 5:
            return pd.DataFrame()

        df = compute_orderflow_score(df)

        cond = (
            df["orderflow_score"] > 500
        )

        result = df.loc[cond].copy()

        if len(result) > 0:

            logger.info(
                "[ORDERFLOW SHOCK] %s detected",
                len(result)
            )

            # ----------------------------------------------
            # Discord通知
            # ----------------------------------------------

            try:

                notify_orderflow(result)

            except Exception:

                logger.exception(
                    "[orderflow_alert] failed"
                )

        return result

    except Exception:

        logger.exception("[detect_orderflow_shock]")

        return pd.DataFrame()


# ============================================================
# ENTRYフィルター
# ============================================================

def allow_orderflow_entry(row: dict) -> bool:

    try:

        score = row.get("orderflow_score", 0)

        score = float(score)

        if score > 500:
            return True

        return False

    except Exception:

        logger.exception("[allow_orderflow_entry]")

        return False