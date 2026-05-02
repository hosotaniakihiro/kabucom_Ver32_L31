# ============================================================
# trading/logger/vwap_logger.py
# Ver1.1-PRODUCTION-VWAP-DEVIATION-LOGGER
# ------------------------------------------------------------
# ✔ VWAP deviation ranking
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
# VWAP DEVIATION RANKING
# ============================================================

def log_vwap_deviation(df, top_n: int = 10):

    try:

        df = safe_copy(df)

        if df.empty:
            return

        if "symbol" not in df.columns:
            return

        if "vwap" not in df.columns:
            return

        # 最新バーのみ
        df = latest_per_symbol(df)

        # close取得
        if "close" not in df.columns:
            if "close_price" in df.columns:
                df["close"] = df["close_price"]
            elif "c" in df.columns:
                df["close"] = df["c"]
            else:
                return

        df["close"] = safe_numeric(df["close"])
        df["vwap"] = safe_numeric(df["vwap"])

        # VWAP乖離率
        df["vwap_dev"] = (df["close"] - df["vwap"]) / df["vwap"]

        rank = (
            df.sort_values("vwap_dev", ascending=False)
            .head(top_n)
        )

        if rank.empty:
            return

        logger.info("========== 📊 VWAP DEVIATION ==========")

        for i, r in enumerate(rank.itertuples(), 1):

            symbol = str(getattr(r, "symbol", "不明"))
            name = safe_symbolname(r)

            close = fmt_float(safe_close(r))
            volume = fmt_float(safe_volume(r))
            rsi = fmt_float(safe_rsi(r))

            dev = float(getattr(r, "vwap_dev", 0)) * 100

            logger.info(
                "%2d. %s(%s) dev=%.2f%% C=%s V=%s RSI=%s",
                i,
                symbol,
                name,
                dev,
                close,
                volume,
                rsi,
            )

    except Exception:

        logger.exception("[VWAP LOGGER ERROR]")


# ============================================================
# VWAP乖離件数
# ============================================================

def count_vwap_extreme(df, threshold: float = 0.03):

    try:

        df = safe_copy(df)

        if df.empty:
            return 0

        if "vwap" not in df.columns:
            return 0

        if "close" not in df.columns:
            return 0

        df["close"] = safe_numeric(df["close"])
        df["vwap"] = safe_numeric(df["vwap"])

        dev = (df["close"] - df["vwap"]) / df["vwap"]

        return int((dev >= threshold).sum())

    except Exception:

        logger.exception("[VWAP COUNT ERROR]")

        return 0


# ============================================================
# VWAP乖離銘柄取得
# ============================================================

def get_vwap_extreme_symbols(df, threshold: float = 0.03):

    try:

        df = safe_copy(df)

        if df.empty:
            return []

        if "symbol" not in df.columns:
            return []

        if "vwap" not in df.columns:
            return []

        if "close" not in df.columns:
            return []

        df["close"] = safe_numeric(df["close"])
        df["vwap"] = safe_numeric(df["vwap"])

        dev = (df["close"] - df["vwap"]) / df["vwap"]

        df = df[dev >= threshold]

        if df.empty:
            return []

        return df["symbol"].astype(str).tolist()

    except Exception:

        logger.exception("[VWAP SYMBOL ERROR]")

        return []