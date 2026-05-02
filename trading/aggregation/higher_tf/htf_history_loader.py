"""
============================================================
htf_history_loader.py
Higher Timeframe History Loader
------------------------------------------------------------
✔ merged_summary cache優先取得
✔ DB fallback
✔ DuckDB / SQLite 互換
✔ symbolフィルタ
✔ DataFrame安全化
✔ NaN / datetime防御
✔ 本番運用安定版
============================================================
"""

from __future__ import annotations

import logging
import pandas as pd

from database.session import get_summary_engine
from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# PUBLIC
# ============================================================

def load_history(tf: int, symbol: str) -> pd.DataFrame:
    """
    HTF履歴取得

    Priority
    --------
    1. global_data cache
    2. summary DB
    """

    try:

        # --------------------------------------------------
        # cache
        # --------------------------------------------------

        df = _load_from_cache(tf, symbol)

        if df is not None and not df.empty:
            return df

        # --------------------------------------------------
        # DB fallback
        # --------------------------------------------------

        df = _load_from_db(tf, symbol)

        if df is None:
            return pd.DataFrame()

        return df

    except Exception:

        logger.exception("[HTF history load fatal]")

        return pd.DataFrame()


# ============================================================
# CACHE
# ============================================================

def _load_from_cache(tf: int, symbol: str):

    try:

        df = global_data.get_multi_summary(tf)

        if df is None or df.empty:
            return None

        df = df[df["symbol"] == symbol]

        if df.empty:
            return None

        return _sanitize_dataframe(df)

    except Exception:

        logger.exception("[HTF cache load failed]")

        return None


# ============================================================
# DB
# ============================================================

def _load_from_db(tf: int, symbol: str):

    try:

        table = f"stock_summary_{tf}min"

        query = f"""
        SELECT *
        FROM {table}
        WHERE symbol = ?
        ORDER BY datetime ASC
        """

        engine = get_summary_engine()

        with engine.connect() as conn:

            df = pd.read_sql(
                query,
                conn,
                params=[symbol]
            )

        if df is None or df.empty:
            return pd.DataFrame()

        return _sanitize_dataframe(df)

    except Exception:

        logger.exception("[HTF DB load failed]")

        return pd.DataFrame()


# ============================================================
# SANITIZE
# ============================================================

def _sanitize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    DataFrame安全化
    """

    try:

        df = df.copy()

        if "datetime" in df.columns:

            df["datetime"] = pd.to_datetime(
                df["datetime"],
                errors="coerce"
            )

            df = df.dropna(subset=["datetime"])

        df = df.sort_values("datetime")

        df = df.reset_index(drop=True)

        return df

    except Exception:

        logger.exception("[HTF sanitize failed]")

        return pd.DataFrame()