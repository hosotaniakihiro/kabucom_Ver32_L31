# ============================================================
# File   : trading/summary/scheduled_summary.py
# Ver5.3-ULTRA-PRODUCTION-SAFE-SUMMARY-LOGGER
# ------------------------------------------------------------
# ✔ Ver5.2 全機能保持
# ✔ summary_logger追加
# ✔ 3min / 5min 確定ログ
# ✔ datetime自動修復
# ✔ MTF防御
# ✔ ranking snapshot防御
# ✔ DB保存安全化
# ✔ NaN安全
# ✔ 本番完全耐性
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from global_state import global_data
from trading.aggregation.hybrid_1m_engine import get_hybrid_1m_engine
from trading.summary.summary_builder_master import (
    build_all_summaries_every_minute,
)

from trading.summary.summary_mtf_join import apply_mtf_join
from trading.summary.summary_printer import (
    print_latest_bar,
    print_summary_top10,
)

from trading.summary.summary_logger import summary_logger

from trading.ranking.live_ranking_engine import build_live_ranking
from trading.ranking.snapshot_ranking_engine import build_snapshot_ranking
from trading.ranking.rank_gap_engine import build_rank_gap
from trading.ranking.ranking_trigger import trigger_ranking_entry

from database.summary_dao import insert_summary
from database import session

logger = logging.getLogger(__name__)

MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MIN = 30


# ============================================================
# datetime 安全生成
# ============================================================

def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "datetime" in df.columns:
        return df

    if "time" in df.columns:
        df["datetime"] = pd.to_datetime(df["time"])
        return df

    if "timestamp" in df.columns:
        df["datetime"] = pd.to_datetime(df["timestamp"])
        return df

    if "date" in df.columns and "time" in df.columns:
        df["datetime"] = pd.to_datetime(
            df["date"].astype(str) + " " + df["time"].astype(str)
        )
        return df

    logger.error("[DATETIME] column missing")
    return df


# ============================================================
# DB整形
# ============================================================

def _prepare_for_db(df: pd.DataFrame, interval: int) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = _ensure_datetime(df)

    if "datetime" not in df.columns:
        logger.error("[DB PREP] datetime missing")
        return df

    df = df.copy()

    df["date"] = df["datetime"].dt.date
    df["time"] = df["datetime"].dt.time
    df["start_time"] = df["datetime"].dt.time
    df["end_time"] = (df["datetime"] + pd.Timedelta(minutes=interval)).dt.time

    df["time_range"] = (
        df["start_time"].astype(str).str[:5]
        + " - "
        + df["end_time"].astype(str).str[:5]
    )

    return df


# ============================================================
# 表示
# ============================================================

def _display(df: pd.DataFrame, interval: int):

    if df is None or df.empty:
        logger.warning("[DISPLAY] %smin empty → skip", interval)
        return

    logger.info(
        "========== ⏱ SUMMARY DISPLAY (%smin) rows=%d ==========",
        interval,
        len(df),
    )

    print_latest_bar(interval)
    print_summary_top10(interval=interval, df=df)

    logger.info(
        "========== ⏱ END SUMMARY DISPLAY (%smin) ==========",
        interval,
    )


# ============================================================
# FREEZE
# ============================================================

def _freeze(interval: int) -> pd.DataFrame:

    df_existing = global_data.latest_summary_by_interval.get(interval)

    if df_existing is None or df_existing.empty:
        logger.info("[FREEZE] no summary in memory")
        return pd.DataFrame()

    _display(df_existing, interval)
    return df_existing


# ============================================================
# MAIN
# ============================================================

def run_scheduled_summary(interval: int) -> pd.DataFrame:

    try:

        logger.info("[SCHEDULED] interval=%s", interval)

        now = pd.Timestamp.now().floor("min")

        outside_trading_hours = (
            now.hour < 9
            or (now.hour == MARKET_CLOSE_HOUR and now.minute > MARKET_CLOSE_MIN)
            or now.hour > MARKET_CLOSE_HOUR
        )

        # ----------------------------------------------------
        # FREEZE
        # ----------------------------------------------------
        if outside_trading_hours:
            return _freeze(interval)

        # ----------------------------------------------------
        # HYBRID 1MIN
        # ----------------------------------------------------
        summary_1min = get_hybrid_1m_engine().build_hybrid_1m()

        if summary_1min is None or summary_1min.empty:
            logger.warning("[HYBRID] empty result")
            return pd.DataFrame()

        summary_1min = _ensure_datetime(summary_1min)

        # ----------------------------------------------------
        # MTF BUILD
        # ----------------------------------------------------
        prev_3min = global_data.latest_summary_by_interval.get(3)
        prev_5min = global_data.latest_summary_by_interval.get(5)

        result = build_all_summaries_every_minute(
            yahoo_1min=summary_1min,
            push_raw=pd.DataFrame(),
            summary_3min_cache=prev_3min,
            summary_5min_cache=prev_5min,
            now=now,
            dump_score_log=False,
        )

        summary_1min = result.get("summary_1min", pd.DataFrame())
        summary_3min = result.get("summary_3min", pd.DataFrame())
        summary_5min = result.get("summary_5min", pd.DataFrame())

        # ==========================================
        # ★ 3分 / 5分 確定ログ
        # ==========================================
        try:
            summary_logger.log_if_new(summary_3min, 3)
            summary_logger.log_if_new(summary_5min, 5)
        except Exception:
            logger.exception("[SUMMARY LOGGER] failed")

        summary_1min = _ensure_datetime(summary_1min)

        # ----------------------------------------------------
        # MTF JOIN
        # ----------------------------------------------------
        summary_1min = apply_mtf_join(
            summary_1min,
            summary_3min,
            summary_5min,
        )

        # ----------------------------------------------------
        # 整形
        # ----------------------------------------------------
        if "datetime" in summary_1min.columns:

            summary_1min = (
                summary_1min
                .drop_duplicates(subset=["symbol", "datetime"], keep="last")
                .sort_values("datetime")
                .reset_index(drop=True)
            )

        summary_1min["interval"] = 1
        summary_1min["interval_name"] = "1min"

        # ----------------------------------------------------
        # LIVE / SNAPSHOT / GAP
        # ----------------------------------------------------
        try:

            snapshot_df = global_data.get_latest_ranking_snapshot_1min()

            if snapshot_df is None:
                snapshot_df = pd.DataFrame()

            df_live = build_live_ranking(summary_1min)
            df_snapshot = build_snapshot_ranking(snapshot_df)

            df_gap = build_rank_gap(df_live, df_snapshot)

            if df_gap is not None and not df_gap.empty:

                top_candidates = (
                    df_gap
                    .sort_values("rank_live")
                    .head(30)
                )

                for _, row in top_candidates.iterrows():

                    trigger_ranking_entry(
                        symbol=row["symbol"],
                        symbolname=row.get("symbolname", ""),
                        entry_decision="BUY",
                        trend_score=int(row.get("trend_score", 0)),
                        volume_speed=float(row.get("volume_speed", 0)),
                        reason="LIVE_GAP",
                        market="ALL",
                        extra_features={
                            "rank_gap": row.get("rank_gap", 0),
                            "score_gap": row.get("score_gap", 0),
                            "rank_gap_ratio": row.get("rank_gap_ratio", 0),
                            "score_gap_ratio": row.get("score_gap_ratio", 0),
                            "last_push_sec": row.get("last_push_sec", 999),
                        }
                    )

        except Exception:
            logger.exception("[RANK_GAP] integration failed")

        # ----------------------------------------------------
        # STATE同期
        # ----------------------------------------------------
        global_data.latest_summary_by_interval[1] = summary_1min
        global_data.set_merged_summary(1, summary_1min)

        for _, row in summary_1min.iterrows():

            global_data.summary.set(
                1,
                str(row["symbol"]),
                row.to_dict(),
            )

        _display(summary_1min, 1)

        # ----------------------------------------------------
        # DB保存 1min
        # ----------------------------------------------------
        Session = session.Session_summary
        db_session = Session()

        try:

            df_db = _prepare_for_db(summary_1min, 1)

            for _, row in df_db.iterrows():

                insert_summary(
                    session=db_session,
                    interval="1min",
                    row=row.to_dict(),
                )

            db_session.commit()

        except Exception:

            db_session.rollback()
            logger.exception("[SUMMARY DB] 1min save failed")

        finally:
            db_session.close()

        # ----------------------------------------------------
        # 3 / 5 MIN
        # ----------------------------------------------------
        for tf, df_tf in [(3, summary_3min), (5, summary_5min)]:

            if df_tf is None or df_tf.empty:
                continue

            df_tf = _ensure_datetime(df_tf)

            if "datetime" in df_tf.columns:

                df_tf = (
                    df_tf
                    .drop_duplicates(subset=["symbol", "datetime"], keep="last")
                    .sort_values("datetime")
                    .reset_index(drop=True)
                )

            df_tf["interval"] = tf
            df_tf["interval_name"] = f"{tf}min"

            db_session = Session()

            try:

                df_db = _prepare_for_db(df_tf, tf)

                for _, row in df_db.iterrows():

                    insert_summary(
                        session=db_session,
                        interval=f"{tf}min",
                        row=row.to_dict(),
                    )

                db_session.commit()

            except Exception:

                db_session.rollback()
                logger.exception("[SUMMARY DB] %smin save failed", tf)

            finally:
                db_session.close()

            global_data.latest_summary_by_interval[tf] = df_tf
            global_data.set_merged_summary(tf, df_tf)

            for _, row in df_tf.iterrows():

                global_data.summary.set(
                    tf,
                    str(row["symbol"]),
                    row.to_dict(),
                )

            _display(df_tf, tf)

        return summary_1min

    except Exception:

        logger.exception("[SCHEDULED] fatal error")
        return pd.DataFrame()