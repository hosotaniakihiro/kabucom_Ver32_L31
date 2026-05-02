# ============================================================
# trading/logger/tosama_logger.py
# Ver1.1-PRODUCTION-TOSAMA-INAGO-LOGGER
# ------------------------------------------------------------
# ✔ Tosama Inago signal detection
# ✔ symbol(symbolname) 表示
# ✔ close / volume / RSI 表示
# ✔ NaN / inf 完全防御
# ✔ 最新バーのみ表示
# ✔ symbol重複排除
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
# 殿様イナゴログ
# ============================================================

def log_tosama_inago(df):

    try:

        df = safe_copy(df)

        if df.empty:
            return

        if "symbol" not in df.columns:
            return

        if "tosama_signal" not in df.columns:
            return

        df = latest_per_symbol(df)

        df["tosama_signal"] = safe_numeric(df["tosama_signal"])

        df = df[df["tosama_signal"] == 1]

        if df.empty:
            return

        logger.info("========== 🐸 TOSAMA INAGO SIGNAL ==========")

        for r in df.itertuples():

            symbol = str(getattr(r, "symbol", "不明"))
            name = safe_symbolname(r)

            close = fmt_float(safe_close(r))
            volume = fmt_float(safe_volume(r))
            rsi = fmt_float(safe_rsi(r))

            logger.info(
                "🐸 %s(%s) C=%s V=%s RSI=%s",
                symbol,
                name,
                close,
                volume,
                rsi,
            )

    except Exception:

        logger.exception("[TOSAMA LOGGER ERROR]")


# ============================================================
# 殿様イナゴ件数
# ============================================================

def count_tosama_signals(df):

    try:

        df = safe_copy(df)

        if df.empty:
            return 0

        if "tosama_signal" not in df.columns:
            return 0

        df["tosama_signal"] = safe_numeric(df["tosama_signal"])

        return int((df["tosama_signal"] == 1).sum())

    except Exception:

        logger.exception("[TOSAMA COUNT ERROR]")

        return 0


# ============================================================
# 殿様イナゴ銘柄一覧
# ============================================================

def get_tosama_symbols(df):

    try:

        df = safe_copy(df)

        if df.empty:
            return []

        if "symbol" not in df.columns:
            return []

        if "tosama_signal" not in df.columns:
            return []

        df["tosama_signal"] = safe_numeric(df["tosama_signal"])

        df = df[df["tosama_signal"] == 1]

        if df.empty:
            return []

        return df["symbol"].astype(str).tolist()

    except Exception:

        logger.exception("[TOSAMA SYMBOL ERROR]")

        return []