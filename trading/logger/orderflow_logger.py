# ============================================================
# trading/logger/orderflow_logger.py
# Ver1.1-PRODUCTION-ORDERFLOW-LOGGER
# ------------------------------------------------------------
# ✔ OrderFlow Imbalance ranking
# ✔ symbol(symbolname) 表示
# ✔ close / volume / RSI 表示
# ✔ NaN / inf 完全防御
# ✔ stable sort
# ✔ duplicate symbol remove
# ✔ 最新バーのみ表示
# ✔ list / dict / DataFrame 対応
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from .format_utils import (
    safe_copy,
    safe_numeric,
    safe_symbolname,
    safe_close,
    safe_volume,
    safe_rsi,
    fmt_float,
    latest_per_symbol,
)

logger = logging.getLogger(__name__)


# ============================================================
# ORDERFLOW RANKING
# ============================================================

def log_orderflow_ranking(df, top_n: int = 10):

    try:

        df = safe_copy(df)

        if df.empty:
            return

        if "symbol" not in df.columns:
            return

        if "orderflow_imbalance" not in df.columns:
            return

        # 最新バーのみ
        df = latest_per_symbol(df)

        # 数値安全化
        df["orderflow_imbalance"] = safe_numeric(df["orderflow_imbalance"])

        rank = (
            df.sort_values("orderflow_imbalance", ascending=False)
            .head(top_n)
        )

        if rank.empty:
            return

        logger.info("========== 📊 ORDERFLOW IMBALANCE ==========")

        for i, r in enumerate(rank.itertuples(), 1):

            symbol = str(getattr(r, "symbol", "不明"))
            name = safe_symbolname(r)

            close = fmt_float(safe_close(r))
            volume = fmt_float(safe_volume(r))
            rsi = fmt_float(safe_rsi(r))

            imbalance = float(getattr(r, "orderflow_imbalance", 0))

            logger.info(
                "%2d. %s(%s) imbalance=%.2f C=%s V=%s RSI=%s",
                i,
                symbol,
                name,
                imbalance,
                close,
                volume,
                rsi,
            )

    except Exception:

        logger.exception("[ORDERFLOW LOGGER ERROR]")


# ============================================================
# ORDERFLOW 件数
# ============================================================

def count_orderflow_signals(df, threshold: float = 1.0):

    try:

        df = safe_copy(df)

        if df.empty:
            return 0

        if "orderflow_imbalance" not in df.columns:
            return 0

        df["orderflow_imbalance"] = safe_numeric(df["orderflow_imbalance"])

        return int((df["orderflow_imbalance"] >= threshold).sum())

    except Exception:

        logger.exception("[ORDERFLOW COUNT ERROR]")

        return 0


# ============================================================
# ORDERFLOW 銘柄一覧
# ============================================================

def get_orderflow_symbols(df, threshold: float = 1.0):

    try:

        df = safe_copy(df)

        if df.empty:
            return []

        if "symbol" not in df.columns:
            return []

        if "orderflow_imbalance" not in df.columns:
            return []

        df["orderflow_imbalance"] = safe_numeric(df["orderflow_imbalance"])

        df = df[df["orderflow_imbalance"] >= threshold]

        if df.empty:
            return []

        return df["symbol"].astype(str).tolist()

    except Exception:

        logger.exception("[ORDERFLOW SYMBOL ERROR]")

        return []