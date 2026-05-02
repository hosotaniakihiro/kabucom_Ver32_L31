# ============================================================
# trading/logger/liquidity_logger.py
# Ver1.1-PRODUCTION-LIQUIDITY-LOGGER
# ------------------------------------------------------------
# ✔ Liquidity (volume) ranking
# ✔ symbol(symbolname) 表示
# ✔ close / RSI 表示
# ✔ volume / vol 列互換
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
    fmt_int,
    latest_per_symbol,
)

logger = logging.getLogger(__name__)


# ============================================================
# LIQUIDITY RANKING
# ============================================================

def log_liquidity_ranking(df, top_n: int = 10):

    try:

        df = safe_copy(df)

        if df.empty:
            return

        if "symbol" not in df.columns:
            return

        # volume列互換
        if "volume" not in df.columns:

            if "vol" in df.columns:
                df["volume"] = df["vol"]
            else:
                return

        # 最新バーのみ
        df = latest_per_symbol(df)

        df["volume"] = safe_numeric(df["volume"])

        rank = (
            df.sort_values("volume", ascending=False)
            .head(top_n)
        )

        if rank.empty:
            return

        logger.info("========== 💰 LIQUIDITY RANKING ==========")

        for i, r in enumerate(rank.itertuples(), 1):

            symbol = str(getattr(r, "symbol", "不明"))
            name = safe_symbolname(r)

            close = fmt_float(safe_close(r))
            rsi = fmt_float(safe_rsi(r))

            volume = fmt_int(getattr(r, "volume", 0))

            logger.info(
                "%2d. %s(%s) volume=%s C=%s RSI=%s",
                i,
                symbol,
                name,
                volume,
                close,
                rsi,
            )

    except Exception:

        logger.exception("[LIQUIDITY LOGGER ERROR]")


# ============================================================
# LIQUIDITY 件数
# ============================================================

def count_high_liquidity(df, threshold: int = 1_000_000):

    try:

        df = safe_copy(df)

        if df.empty:
            return 0

        if "volume" not in df.columns:
            return 0

        df["volume"] = safe_numeric(df["volume"])

        return int((df["volume"] >= threshold).sum())

    except Exception:

        logger.exception("[LIQUIDITY COUNT ERROR]")

        return 0


# ============================================================
# LIQUIDITY 銘柄取得
# ============================================================

def get_high_liquidity_symbols(df, threshold: int = 1_000_000):

    try:

        df = safe_copy(df)

        if df.empty:
            return []

        if "symbol" not in df.columns:
            return []

        if "volume" not in df.columns:
            return []

        df["volume"] = safe_numeric(df["volume"])

        df = df[df["volume"] >= threshold]

        if df.empty:
            return []

        return df["symbol"].astype(str).tolist()

    except Exception:

        logger.exception("[LIQUIDITY SYMBOL ERROR]")

        return []