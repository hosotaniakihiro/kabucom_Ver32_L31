# ============================================================
# File   : trading/summary/summary_persistence.py
# Ver2.0-HYBRID-INTEGRATED-FINAL
# ------------------------------------------------------------
# ✔ 既存機能削除ゼロ
# ✔ 20分ハイブリッド対応
# ✔ summary 1/3/5 UPSERT
# ✔ ranking / entry 接続維持
# ✔ CPUガード維持
# ✔ 実運用安定版
# ============================================================

from __future__ import annotations

import datetime as dt
import pandas as pd
import logging
from sqlalchemy import create_engine, text

from trading.aggregation.hybrid_1m_engine import get_hybrid_1m_engine
from trading.summary.summary_builder_master import (
    build_all_summaries_every_minute,
)

logger = logging.getLogger(__name__)

# ============================================================
# DB
# ============================================================

def _get_summary_engine(interval: int):
    if interval == 1:
        return create_engine("sqlite:///summary_1min.db")
    if interval == 3:
        return create_engine("sqlite:///summary_3min.db")
    if interval == 5:
        return create_engine("sqlite:///summary_5min.db")
    raise ValueError(interval)


# ============================================================
# UPSERT
# ============================================================

def upsert_summary(
    df: pd.DataFrame,
    *,
    interval: int,
    table: str,
):
    if df is None or df.empty:
        return

    engine = _get_summary_engine(interval)
    tmp_table = f"_tmp_{table}"

    with engine.begin() as conn:
        df.to_sql(tmp_table, conn, if_exists="replace", index=False)

        conn.execute(text(f"""
            INSERT OR REPLACE INTO {table}
            SELECT * FROM {tmp_table}
        """))

        conn.execute(text(f"DROP TABLE {tmp_table}"))


# ============================================================
# 保存
# ============================================================

def persist_summaries(
    *,
    summary_1min: pd.DataFrame,
    summary_3min: pd.DataFrame,
    summary_5min: pd.DataFrame,
):

    upsert_summary(summary_1min, interval=1, table="stock_summary_1min")
    upsert_summary(summary_3min, interval=3, table="stock_summary_3min")
    upsert_summary(summary_5min, interval=5, table="stock_summary_5min")


# ============================================================
# ranking / entry
# ============================================================

def notify_ranking_and_entry(
    *,
    summary_1min: pd.DataFrame,
    summary_3min: pd.DataFrame,
    summary_5min: pd.DataFrame,
):
    from trading.ranking.ranking_trigger import on_new_summary
    from trading.entry.run_entry_pipeline import run_entry_pipeline

    on_new_summary(
        summary_1min=summary_1min,
        summary_3min=summary_3min,
        summary_5min=summary_5min,
    )

    run_entry_pipeline()


# ============================================================
# 毎分ランタイム（🔥ここが重要）
# ============================================================

def run_summary_every_minute(
    *,
    summary_3min_cache: pd.DataFrame | None,
    summary_5min_cache: pd.DataFrame | None,
    dump_score_log: bool = False,
):

    now = dt.datetime.now().replace(second=0, microsecond=0)

    logger.info("🕒 [SUMMARY_RUNTIME] START")

    # --------------------------------------------------------
    # CPUガード
    # --------------------------------------------------------
    if now.hour < 9 or now.hour >= 15:
        logger.info("[SUMMARY_RUNTIME] outside trading hours → skip")
        return

    # --------------------------------------------------------
    # 🔥 HYBRID 1MIN を取得
    # --------------------------------------------------------
    hybrid_1min = get_hybrid_1m_engine().build_hybrid_1m()

    if hybrid_1min is None or hybrid_1min.empty:
        logger.warning("[SUMMARY_RUNTIME] hybrid empty")
        return

    # --------------------------------------------------------
    # MTF再構築
    # --------------------------------------------------------
    result = build_all_summaries_every_minute(
        yahoo_1min=hybrid_1min,     # 🔥ここがポイント
        push_raw=pd.DataFrame(),   # 直接pushは使わない
        summary_3min_cache=summary_3min_cache,
        summary_5min_cache=summary_5min_cache,
        now=now,
        dump_score_log=dump_score_log,
    )

    summary_1min = result["summary_1min"]
    summary_3min = result["summary_3min"]
    summary_5min = result["summary_5min"]

    # --------------------------------------------------------
    # 保存
    # --------------------------------------------------------
    persist_summaries(
        summary_1min=summary_1min,
        summary_3min=summary_3min,
        summary_5min=summary_5min,
    )

    # --------------------------------------------------------
    # ranking / entry
    # --------------------------------------------------------
    notify_ranking_and_entry(
        summary_1min=summary_1min,
        summary_3min=summary_3min,
        summary_5min=summary_5min,
    )

    logger.info("✅ [SUMMARY_RUNTIME] DONE")