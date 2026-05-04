# ============================================================
# database/crud/crud_ranking_summary.py
# ------------------------------------------------------------
# ✔ ranking_summary_1min を DB に保存（学習用）
# ✔ tosama DB 使用
# ✔ Engine.begin() による安全トランザクション
# ✔ INSERT ONLY（既存DB非破壊）
# ============================================================

import logging
from sqlalchemy import text
import pandas as pd

from database.session import tosama_engine

logger = logging.getLogger(__name__)

# ============================================================
# INSERT ranking_summary_1min
# ============================================================
def insert_ranking_summary_1min(df: pd.DataFrame):
    """
    ranking_summary_1min を tosama DB に保存する
    （学習用・履歴用途）
    """

    if df is None or df.empty:
        logger.debug("[ranking_summary] empty df → skip")
        return

    required_cols = {"symbol", "datetime", "close"}
    if not required_cols.issubset(df.columns):
        logger.warning(
            "[ranking_summary] missing columns: %s",
            required_cols - set(df.columns),
        )
        return

    sql = text("""
        INSERT INTO ranking_summary_1min (
            symbol,
            datetime,
            close,
            ma5,
            ma25,
            ma75,
            vwap,
            source,
            created_at
        )
        VALUES (
            :symbol,
            :datetime,
            :close,
            :ma5,
            :ma25,
            :ma75,
            :vwap,
            :source,
            datetime('now')
        )
    """)

    inserted = 0

    try:
        with tosama_engine.begin() as conn:
            for _, r in df.iterrows():
                conn.execute(sql, {
                    "symbol": str(r.get("symbol")),
                    "datetime": str(r.get("datetime")),
                    "close": _safe_float(r.get("close")),
                    "ma5": _safe_float(r.get("ma5")),
                    "ma25": _safe_float(r.get("ma25")),
                    "ma75": _safe_float(r.get("ma75")),
                    "vwap": _safe_float(r.get("vwap")),
                    "source": "RANKING",
                })
                inserted += 1

        logger.info(
            "[ranking_summary] inserted rows=%d symbols=%d",
            inserted,
            df["symbol"].nunique(),
        )

    except Exception:
        logger.exception("❌ insert_ranking_summary_1min failed")


# ============================================================
# utils
# ============================================================
def _safe_float(v):
    """
    None / NaN / 非数値 → None
    """
    try:
        if v is None:
            return None
        v = float(v)
        if pd.isna(v):
            return None
        return v
    except Exception:
        return None
