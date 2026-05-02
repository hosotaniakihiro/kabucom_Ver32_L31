# ============================================================
# File   : scheduler_jobs/ranking_jobs.py
# Version: Ver1.5-PRODUCTION-RANKING-PIPELINE-AI-FULL-RAWMULTI
# ------------------------------------------------------------
# ✔ Ver1.4 全機能保持（削除ゼロ）
# ✔ dataframe hard guard
# ✔ MultiIndex flatten
# ✔ duplicate column / duplicate index guard
# ✔ symbol normalize
# ✔ datetime normalize
# ✔ ranking duplicate guard
# ✔ ranking stability engine
# ✔ ETF / REIT / PRO Market filter
# ✔ pipeline crash isolation
# ✔ DB load safe
# ✔ entry pipeline safe
# ✔ production logging
# ------------------------------------------------------------
# 🔥 FIX:
# ✔ RAW multi-type 保存後でも安定して最新1銘柄1行へ整理
# ✔ snapshot_time desc + rank_position asc 優先で重複整理
# ✔ job_ranking_entry logging 強化
# ✔ global_data.latest_ranking_raw 更新安定化
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from global_state import global_data

from database.session import Session_ranking
from database.models import RankingRaw1Min

# ============================================================
# Ranking engines
# ============================================================

from trading.ranking.scheduler import save_ranking_data_loop
from trading.ranking.job import run_ranking_job
from trading.ranking.active_symbol_manager import update_active_symbols

# ============================================================
# Ranking pipeline
# ============================================================

from trading.ranking.ranking_strength_builder import build_ranking_strength
from trading.ranking.ranking_velocity_builder import build_ranking_velocity
from trading.ranking.ranking_acceleration_builder import build_ranking_acceleration
from trading.ranking.ranking_theme_heat_builder import build_ranking_theme_heat
from trading.ranking.ranking_breakout_detector import detect_ranking_breakout
from trading.ranking.ranking_liquidity_filter import apply_ranking_liquidity_filter

# ============================================================
# AI modules
# ============================================================

from trading.ai.surge_detector_ai import build_surge_probability
from trading.ai.institutional_flow_detector import detect_institutional_flow
from trading.ai.tonosama_detector import detect_tonosama

# ============================================================
# Entry
# ============================================================

from trading.handlers.entry_controller import run_entry_pipeline

# ============================================================
# Market filter
# ============================================================

from utils.market_filter import filter_tradeable_dataframe

# ============================================================
# Ranking stability
# ============================================================

from trading.ranking.stability.ranking_stability_engine import apply_ranking_stability


logger = logging.getLogger(__name__)


# ============================================================
# dataframe guard
# ============================================================

def _sanitize_dataframe(df):

    try:

        if df is None:
            return None

        if not isinstance(df, pd.DataFrame):

            try:
                df = pd.DataFrame(df)
            except Exception:
                return None

        if df.empty:
            return df

        df = df.copy()

        # flatten MultiIndex
        if isinstance(df.columns, pd.MultiIndex):

            df.columns = [
                "_".join([str(x) for x in col if x not in ("", None)])
                for col in df.columns
            ]

        # duplicate column guard
        if df.columns.duplicated().any():

            dup = df.columns[df.columns.duplicated()].tolist()

            logger.warning(
                "[RANKING] duplicate columns removed=%s",
                dup
            )

            df = df.loc[:, ~df.columns.duplicated()].copy()

        # duplicate index guard
        try:
            if df.index.duplicated().any():
                df = df.loc[~df.index.duplicated(keep="last")].copy()
        except Exception:
            logger.exception("[RANKING] duplicate index guard failed")

        # symbol normalize
        if "symbol" in df.columns:

            try:
                df["symbol"] = (
                    df["symbol"]
                    .astype(str)
                    .str.strip()
                )
                df = df[df["symbol"] != ""].copy()
            except Exception:
                logger.exception("[RANKING] symbol normalize failed")

        # datetime normalize
        time_col = None
        if "snapshot_time" in df.columns:
            time_col = "snapshot_time"
        elif "datetime" in df.columns:
            time_col = "datetime"

        if time_col is not None:

            try:
                df[time_col] = pd.to_datetime(
                    df[time_col],
                    errors="coerce"
                )
                df = df.dropna(subset=[time_col]).copy()
            except Exception:
                logger.exception("[ranking datetime normalize]")

        # numeric normalize
        for col in [
            "rank_position",
            "current_price",
            "trading_volume",
            "trading_value",
            "volume_speed",
            "price_delta_1m",
            "volume_delta_1m",
            "rank_type_id",
        ]:
            if col in df.columns:
                try:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                except Exception:
                    logger.exception("[RANKING] numeric normalize failed col=%s", col)

        # duplicate symbol guard
        # RAW multi-type 保存後は symbol が複数行あり得るため、
        # 最新時刻優先 + rank_position が小さいもの優先で 1銘柄1行へ縮約する
        if "symbol" in df.columns:

            sort_cols = []
            ascending = []

            if "snapshot_time" in df.columns:
                sort_cols.append("snapshot_time")
                ascending.append(False)
            elif "datetime" in df.columns:
                sort_cols.append("datetime")
                ascending.append(False)

            if "rank_position" in df.columns:
                sort_cols.append("rank_position")
                ascending.append(True)

            if "rank_type_id" in df.columns:
                sort_cols.append("rank_type_id")
                ascending.append(True)

            before = len(df)

            if sort_cols:
                df = (
                    df
                    .sort_values(sort_cols, ascending=ascending, kind="stable")
                    .drop_duplicates("symbol", keep="first")
                    .reset_index(drop=True)
                )
            else:
                df = (
                    df
                    .drop_duplicates("symbol", keep="last")
                    .reset_index(drop=True)
                )

            removed = before - len(df)

            if removed > 0:

                logger.info(
                    "[RANKING] duplicate symbol rows consolidated=%s",
                    removed
                )

        return df

    except Exception:

        logger.exception("[ranking dataframe sanitize]")

        return df


# ============================================================
# Ranking Pipeline
# ============================================================

def job_build_ranking_metrics():

    try:

        df = getattr(global_data, "latest_ranking_raw", None)

        if df is None or df.empty:
            logger.info("[RANKING_PIPELINE] latest_ranking_raw empty")
            return

        df = _sanitize_dataframe(df)

        if df is None or df.empty:
            logger.info("[RANKING_PIPELINE] sanitized latest_ranking_raw empty")
            return

        # -----------------------------------------------------
        # market filter
        # -----------------------------------------------------

        df = filter_tradeable_dataframe(df)

        if df is None or df.empty:
            logger.info("[RANKING_PIPELINE] market filter result empty")
            return

        # -----------------------------------------------------
        # ranking stability
        # -----------------------------------------------------

        try:
            df = apply_ranking_stability(df)
        except Exception:
            logger.exception("[ranking_stability]")

        # =====================================================
        # strength
        # =====================================================

        strength_df = build_ranking_strength(df)

        if strength_df is None or strength_df.empty:
            logger.info("[RANKING_PIPELINE] strength_df empty")
            return

        # =====================================================
        # velocity
        # =====================================================

        velocity_df = build_ranking_velocity(strength_df)

        # =====================================================
        # acceleration
        # =====================================================

        try:
            acc_df = build_ranking_acceleration(velocity_df)
        except Exception:
            logger.exception("[ranking_acceleration]")
            acc_df = None

        # =====================================================
        # theme heat
        # =====================================================

        try:
            theme_df = build_ranking_theme_heat(acc_df) if acc_df is not None else None
        except Exception:
            logger.exception("[ranking_theme_heat]")
            theme_df = None

        # =====================================================
        # breakout detector
        # =====================================================

        try:
            breakout_df = detect_ranking_breakout(theme_df) if theme_df is not None else None
        except Exception:
            logger.exception("[ranking_breakout]")
            breakout_df = None

        # =====================================================
        # liquidity filter
        # =====================================================

        try:
            liquidity_df = apply_ranking_liquidity_filter(breakout_df) if breakout_df is not None else None
        except Exception:
            logger.exception("[ranking_liquidity]")
            liquidity_df = None

        # =====================================================
        # surge detector AI
        # =====================================================

        try:
            surge_df = build_surge_probability(liquidity_df) if liquidity_df is not None else None
        except Exception:
            logger.exception("[surge_detector]")
            surge_df = None

        # =====================================================
        # institutional flow detector
        # =====================================================

        try:
            flow_df = detect_institutional_flow(surge_df) if surge_df is not None else None
        except Exception:
            logger.exception("[institutional_flow]")
            flow_df = None

        # =====================================================
        # tonosama detector
        # =====================================================

        try:
            tonosama_df = detect_tonosama(flow_df) if flow_df is not None else None
        except Exception:
            logger.exception("[tonosama_detector]")
            tonosama_df = None

        # =====================================================
        # cache
        # =====================================================

        global_data.ranking_strength = strength_df
        global_data.ranking_velocity = velocity_df

        if acc_df is not None:
            global_data.ranking_acceleration = acc_df

        if theme_df is not None:
            global_data.ranking_theme_heat = theme_df

        if breakout_df is not None:
            global_data.ranking_breakout = breakout_df

        if liquidity_df is not None:
            global_data.ranking_liquidity = liquidity_df

        if surge_df is not None:
            global_data.surge_probability = surge_df

        if flow_df is not None:
            global_data.institutional_flow = flow_df

        if tonosama_df is not None:
            global_data.tonosama_detector = tonosama_df

        # =====================================================
        # logging
        # =====================================================

        logger.info(
            "[RANKING_PIPELINE] "
            "strength=%s velocity=%s acc=%s theme=%s breakout=%s liquidity=%s surge=%s flow=%s tonosama=%s",
            len(strength_df),
            len(velocity_df),
            len(acc_df) if acc_df is not None else 0,
            len(theme_df) if theme_df is not None else 0,
            len(breakout_df) if breakout_df is not None else 0,
            len(liquidity_df) if liquidity_df is not None else 0,
            len(surge_df) if surge_df is not None else 0,
            len(flow_df) if flow_df is not None else 0,
            len(tonosama_df) if tonosama_df is not None else 0,
        )

    except Exception:

        logger.exception("[job_build_ranking_metrics]")


# ============================================================
# Ranking + Entry
# ============================================================

def job_ranking_entry():

    try:

        logger.info("[RANKING_ENTRY] START")

        # =====================================================
        # ranking crawler / post pipeline
        # =====================================================

        run_ranking_job(global_data)

        session = Session_ranking()

        try:

            rows = (
                session.query(
                    RankingRaw1Min.symbol,
                    RankingRaw1Min.snapshot_time,
                    RankingRaw1Min.symbolname,
                    RankingRaw1Min.rank_type,
                    RankingRaw1Min.rank_type_id,
                    RankingRaw1Min.market,
                    RankingRaw1Min.rank_position,
                    RankingRaw1Min.current_price,
                    RankingRaw1Min.trading_volume,
                    RankingRaw1Min.trading_value,
                    RankingRaw1Min.volume_speed,
                    RankingRaw1Min.price_delta_1m,
                    RankingRaw1Min.volume_delta_1m,
                    RankingRaw1Min.minute_of_day,
                    RankingRaw1Min.source,
                    RankingRaw1Min.created_at,
                )
                .order_by(
                    RankingRaw1Min.snapshot_time.desc(),
                    RankingRaw1Min.rank_position.asc(),
                )
                .limit(5000)
                .all()
            )

            if not rows:

                logger.warning("[RANKING_ENTRY] ranking_raw DB empty")
                return

            df = pd.DataFrame(rows)

            logger.info(
                "[RANKING_ENTRY] ranking_raw loaded rows=%s cols=%s",
                len(df),
                len(df.columns),
            )

            df = _sanitize_dataframe(df)

            if df is None or df.empty:
                logger.warning("[RANKING_ENTRY] sanitized df empty")
                return

            logger.info(
                "[RANKING_ENTRY] after sanitize rows=%s symbols=%s",
                len(df),
                df["symbol"].nunique() if "symbol" in df.columns else 0,
            )

            df = filter_tradeable_dataframe(df)

            if df is None or df.empty:
                logger.warning("[RANKING_ENTRY] tradeable df empty")
                return

            logger.info(
                "[RANKING_ENTRY] after market filter rows=%s symbols=%s",
                len(df),
                df["symbol"].nunique() if "symbol" in df.columns else 0,
            )

            global_data.latest_ranking_raw = df.copy()

        finally:

            session.close()

        # =====================================================
        # active symbols
        # =====================================================

        update_active_symbols()

        # =====================================================
        # entry pipeline
        # =====================================================

        symbols_active = getattr(global_data, "symbols_active", None)

        if symbols_active:

            try:
                run_entry_pipeline()
            except Exception:
                logger.exception("[entry_pipeline]")

        else:

            logger.info("[RANKING_ENTRY] symbols_active empty")

    except Exception:

        logger.exception("[job_ranking_entry]")