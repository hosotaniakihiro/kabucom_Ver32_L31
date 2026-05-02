# ============================================================
# File   : trading/ranking/summary/bootstrap_saver.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-BOOTSTRAP-SAVER
# ------------------------------------------------------------
# 【概要】
#   ranking_summary DB への保存
#
# 【保存テーブル】
#   ranking_summary_1min
#   ranking_summary_3min
#   ranking_summary_5min
#
# 【UNIQUE】
#   PRIMARY KEY(symbol, datetime)
# ============================================================

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from trading.ranking.summary.bootstrap_config import (
    INTEGER_COLUMNS,
    NUMERIC_COLUMNS,
    RANKING_SUMMARY_TABLES,
    SUMMARY_COLUMNS,
    TEXT_COLUMNS,
)
from trading.ranking.summary.bootstrap_db import (
    connect_sqlite,
    ensure_parent_dir,
    quote_ident,
)
from trading.ranking.summary.bootstrap_loader import resolve_callable
from trading.ranking.summary.bootstrap_ohlcv import (
    normalize_datetime,
    normalize_symbol,
)

logger = logging.getLogger(__name__)


def create_summary_table(conn, table: str) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(table)} (
            symbol TEXT NOT NULL,
            symbolname TEXT,
            datetime TEXT NOT NULL,
            interval INTEGER,

            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            trading_value REAL,
            turnover REAL,
            tick_count REAL,

            ma5 REAL,
            ma25 REAL,
            ma75 REAL,
            rsi REAL,
            macd REAL,
            signal REAL,
            hist REAL,
            atr REAL,
            vwap REAL,
            slope REAL,
            slope_atr_scaled REAL,

            score REAL,
            score_total REAL,
            final_score REAL,
            display_score REAL,
            score_buy REAL,
            score_sell REAL,
            score_slope REAL,
            score_mtf REAL,

            best_rank_position REAL,
            last_rank_position REAL,
            avg_rank_position REAL,
            rank_count INTEGER,
            rank_types TEXT,

            technical_ready INTEGER,
            hist_len INTEGER,
            source TEXT,
            updated_at TEXT,

            PRIMARY KEY (symbol, datetime)
        )
        """
    )

    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_datetime ON {quote_ident(table)} (datetime)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_symbol_datetime ON {quote_ident(table)} (symbol, datetime)"
    )


def normalize_for_save(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()
    x = normalize_symbol(x)
    x = normalize_datetime(x)

    if x.empty:
        return pd.DataFrame()

    x["interval"] = int(interval)

    for c in SUMMARY_COLUMNS:
        if c not in x.columns:
            if c in TEXT_COLUMNS:
                x[c] = ""
            elif c in INTEGER_COLUMNS:
                x[c] = 0
            else:
                x[c] = np.nan

    x["symbol"] = x["symbol"].astype(str).str.strip()
    x["symbolname"] = x["symbolname"].astype(str).replace(
        {
            "nan": "",
            "NaN": "",
            "None": "",
            "<NA>": "",
        }
    )

    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
    x = x.dropna(subset=["symbol", "datetime"]).copy()
    x["datetime"] = x["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")

    for c in NUMERIC_COLUMNS:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")

    for c in INTEGER_COLUMNS:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce").fillna(0).astype(int)

    for c in TEXT_COLUMNS:
        if c in x.columns:
            x[c] = x[c].astype(str).replace(
                {
                    "nan": "",
                    "NaN": "",
                    "None": "",
                    "<NA>": "",
                }
            )

    if "source" in x.columns:
        x["source"] = x["source"].replace({"": "ranking_snapshot"}).fillna("ranking_snapshot")
    else:
        x["source"] = "ranking_snapshot"

    x["updated_at"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

    x = x[SUMMARY_COLUMNS].copy()
    x = x.drop_duplicates(subset=["symbol", "datetime"], keep="last").copy()

    return x


def filter_after_latest(df: pd.DataFrame, latest_dt: pd.Timestamp | None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if latest_dt is None or pd.isna(latest_dt):
        return df.copy()

    x = df.copy()
    dt = pd.to_datetime(x["datetime"], errors="coerce")
    x = x[dt > pd.to_datetime(latest_dt)].copy()

    return x


def save_ranking_summary_fallback(
    db_path: str,
    df: pd.DataFrame,
    *,
    interval: int,
) -> int:
    if df is None or df.empty:
        return 0

    table = RANKING_SUMMARY_TABLES.get(interval, f"ranking_summary_{interval}min")
    x = normalize_for_save(df, interval=interval)

    if x.empty:
        return 0

    try:
        ensure_parent_dir(db_path)

        with connect_sqlite(db_path, readonly=False) as conn:
            create_summary_table(conn, table)

            cols = SUMMARY_COLUMNS
            col_sql = ",".join([quote_ident(c) for c in cols])
            placeholders = ",".join(["?"] * len(cols))

            update_cols = [c for c in cols if c not in ("symbol", "datetime")]
            update_sql = ",".join(
                [f"{quote_ident(c)}=excluded.{quote_ident(c)}" for c in update_cols]
            )

            sql = f"""
                INSERT INTO {quote_ident(table)} ({col_sql})
                VALUES ({placeholders})
                ON CONFLICT(symbol, datetime) DO UPDATE SET
                    {update_sql}
            """

            rows = []
            for _, r in x.iterrows():
                row = []
                for c in cols:
                    v = r[c]
                    if pd.isna(v):
                        row.append(None)
                    else:
                        row.append(v)
                rows.append(tuple(row))

            conn.executemany(sql, rows)
            conn.commit()

        logger.info(
            "[RANKING SUMMARY BOOTSTRAP SAVER] saved fallback interval=%s rows=%d db=%s table=%s",
            interval,
            len(x),
            db_path,
            table,
        )
        return int(len(x))

    except Exception:
        logger.exception(
            "[RANKING SUMMARY BOOTSTRAP SAVER] save failed interval=%s db=%s",
            interval,
            db_path,
        )
        return 0


def save_ranking_summary(
    db_path: str,
    df: pd.DataFrame,
    *,
    interval: int,
) -> int:
    if df is None or df.empty:
        return 0

    fn = resolve_callable(
        [
            ("trading.ranking.summary.saver", "save_ranking_summary"),
            ("trading.ranking.summary.saver", "save_ranking_summary_df"),
            ("trading.ranking.summary.saver", "upsert_ranking_summary"),
            ("trading.ranking.summary.persistence", "save_ranking_summary"),
        ]
    )

    if callable(fn):
        for kwargs in [
            {"db_path": db_path, "df": df.copy(), "interval": interval},
            {"ranking_summary_db_path": db_path, "df": df.copy(), "interval": interval},
            {"df": df.copy(), "interval": interval},
        ]:
            try:
                out = fn(**kwargs)
                if isinstance(out, int):
                    logger.info(
                        "[RANKING SUMMARY BOOTSTRAP SAVER] saved via existing saver interval=%s rows=%s",
                        interval,
                        out,
                    )
                    return int(out)

                logger.info(
                    "[RANKING SUMMARY BOOTSTRAP SAVER] saved via existing saver interval=%s rows=%d",
                    interval,
                    len(df),
                )
                return int(len(df))

            except TypeError:
                continue
            except Exception:
                logger.exception(
                    "[RANKING SUMMARY BOOTSTRAP SAVER] existing saver failed -> fallback"
                )
                break

    return save_ranking_summary_fallback(db_path, df, interval=interval)


__all__ = [
    "create_summary_table",
    "normalize_for_save",
    "filter_after_latest",
    "save_ranking_summary_fallback",
    "save_ranking_summary",
]