# ============================================================
# PATH: core/realtime/sql_summary_printer.py
# PRODUCTION-SQL-BASED-SUMMARY-PRINTER
# ------------------------------------------------------------
# ✔ DuckDB直接取得
# ✔ 3m / 5m 両対応
# ✔ BUY / SELL TOP10
# ✔ slope / mtf表示
# ✔ NaN完全防御
# ✔ 例外耐性
# ✔ 本番安定設計
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from colorama import Fore, Style

from database.duckdb.manager import duck_manager

logger = logging.getLogger(__name__)


# ============================================================
# 表示ユーティリティ
# ============================================================

def _fmt(v, digits=4):
    try:
        v = float(v)
        if pd.isna(v):
            return "0.0000"
        return f"{v:.{digits}f}"
    except Exception:
        return "0.0000"


# ============================================================
# TOP10表示（SQL直読み）
# ============================================================

def print_sql_top10(interval: int):

    try:

        print(f"\n========== 📊 SUMMARY TOP10 ({interval}min) ==========")

        # 最新バー取得
        latest_dt = duck_manager.conn.execute(f"""
            SELECT MAX(datetime) FROM summary_{interval}m
        """).fetchone()[0]

        if not latest_dt:
            print("データなし")
            return

        # BUY TOP10
        df_buy = duck_manager.conn.execute(f"""
            SELECT
                s.symbol,
                sc.score,
                ind.slope5 AS slope,
                (sc.score + ind.slope5 * 10) AS mtf_score
            FROM summary_{interval}m s
            JOIN scoring_table sc
                ON s.symbol = sc.symbol
                AND s.datetime = sc.datetime
            LEFT JOIN summary_1m_ind ind
                ON s.symbol = ind.symbol
                AND s.datetime = ind.datetime
            WHERE s.datetime = ?
            ORDER BY sc.score DESC
            LIMIT 10
        """, [latest_dt]).fetchdf()

        print("🔵 BUY TOP10（score / slope / mtf）")

        for i, r in enumerate(df_buy.itertuples(), 1):

            print(
                f"{i:>2}. ⚪ {r.symbol:<6} "
                f"score={_fmt(r.score):>8} "
                f"slope={_fmt(r.slope)} "
                f"mtf={_fmt(r.mtf_score)}"
            )

        # SELL TOP10
        df_sell = duck_manager.conn.execute(f"""
            SELECT
                s.symbol,
                sc.score,
                ind.slope5 AS slope,
                (sc.score + ind.slope5 * 10) AS mtf_score
            FROM summary_{interval}m s
            JOIN scoring_table sc
                ON s.symbol = sc.symbol
                AND s.datetime = sc.datetime
            LEFT JOIN summary_1m_ind ind
                ON s.symbol = ind.symbol
                AND s.datetime = ind.datetime
            WHERE s.datetime = ?
            ORDER BY sc.score ASC
            LIMIT 10
        """, [latest_dt]).fetchdf()

        print("🔴 SELL TOP10（下落圧が強い）")

        for i, r in enumerate(df_sell.itertuples(), 1):

            print(
                f"{i:>2}. 🔴 {r.symbol:<6} "
                f"score={_fmt(r.score):>8} "
                f"slope={_fmt(r.slope)} "
                f"mtf={_fmt(r.mtf_score)}"
            )

        print("=" * 60)

    except Exception:
        logger.exception("SQL TOP10 display error")