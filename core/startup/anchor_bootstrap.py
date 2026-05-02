# ============================================================
# File   : core/startup/anchor_bootstrap.py
# Ver    : PRODUCTION-STABLE-REV4.1-DBSAFE-HISTORY-SPLIT-NO-MERGED-OVERWRITE
# ------------------------------------------------------------
# ✔ REV4 全機能完全保持（削除ゼロ）
# ✔ 15:30理論終値維持
# ✔ DB最終datetime自動補正維持
# ✔ 各symbol最終バー取得維持
# ✔ datetime()比較安全化維持
# ✔ 非営業日完全対応
# ✔ 巨大データ安全
# ✔ 例外完全吸収
# ✔ 将来半日取引耐性
# ✔ summary_engine None 完全防御維持
# ✔ engine resolver 追加維持
# ✔ 表示用 anchor / 計算用 history 分離維持
# ✔ history anchor を global_data へ格納維持
# ✔ 原因可視化ログ強化維持
# ✔ 表示用 anchor は merged_summary を上書きしない（NEW）
# ✔ display anchor を別キーへ保存（NEW）
# ✔ closed-day lightweight と非干渉化（NEW）
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
from typing import Optional

import pandas as pd
from sqlalchemy import text

from global_state import global_data
from utils.business_day_utils import (
    get_last_market_close_datetime,
    is_today_business_day,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 設定
# ------------------------------------------------------------

ANCHOR_INTERVAL = 1
MAX_ANCHOR_ROWS = None

# 計算用 history 取得本数（各symbolごと）
ANCHOR_HISTORY_BARS_PER_SYMBOL = 120

# global_data に積むキー名
ANCHOR_HISTORY_KEY = "anchor_summary_history_1min"
ANCHOR_DISPLAY_KEY = "anchor_display_summary_1min"


# ============================================================
# engine resolver
# ============================================================

def _resolve_summary_engine():
    """
    summary_engine の解決を多段fallbackで実施する。
    返り値:
      SQLAlchemy Engine or None
    """

    candidates = []

    # 1) 既存互換: database.session.summary_engine
    try:
        from database.session import summary_engine as session_summary_engine
        candidates.append(("database.session.summary_engine", session_summary_engine))
    except Exception:
        logger.debug("[anchor_bootstrap] database.session.summary_engine import failed", exc_info=True)

    # 2) database.core.connection.get_summary_engine
    try:
        from database.core.connection import get_summary_engine
        try:
            eng = get_summary_engine()
            candidates.append(("database.core.connection.get_summary_engine()", eng))
        except Exception:
            logger.debug("[anchor_bootstrap] get_summary_engine() call failed", exc_info=True)
    except Exception:
        logger.debug("[anchor_bootstrap] database.core.connection.get_summary_engine import failed", exc_info=True)

    # 3) database.connection.get_summary_engine（旧互換）
    try:
        from database.connection import get_summary_engine as legacy_get_summary_engine
        try:
            eng = legacy_get_summary_engine()
            candidates.append(("database.connection.get_summary_engine()", eng))
        except Exception:
            logger.debug("[anchor_bootstrap] legacy get_summary_engine() call failed", exc_info=True)
    except Exception:
        logger.debug("[anchor_bootstrap] database.connection.get_summary_engine import failed", exc_info=True)

    # 4) database.core.connection.get_engine("summary") fallback
    try:
        from database.core.connection import get_engine
        try:
            eng = get_engine("summary")
            candidates.append(("database.core.connection.get_engine('summary')", eng))
        except Exception:
            logger.debug("[anchor_bootstrap] get_engine('summary') call failed", exc_info=True)
    except Exception:
        logger.debug("[anchor_bootstrap] database.core.connection.get_engine import failed", exc_info=True)

    for name, eng in candidates:
        try:
            if eng is None:
                logger.warning("[anchor_bootstrap] engine candidate is None -> %s", name)
                continue

            if not hasattr(eng, "connect"):
                logger.warning("[anchor_bootstrap] engine candidate has no connect() -> %s", name)
                continue

            logger.info("[anchor_bootstrap] summary engine resolved -> %s", name)
            return eng

        except Exception:
            logger.exception("[anchor_bootstrap] engine candidate validation failed -> %s", name)

    logger.error("[anchor_bootstrap] summary engine unresolved (all candidates failed)")
    return None


# ============================================================
# 内部：DB最終datetime取得
# ============================================================

def _get_db_last_datetime(summary_engine) -> Optional[dt.datetime]:
    query = text("""
        SELECT MAX(datetime) AS last_dt
        FROM stock_summary_1min
    """)

    if summary_engine is None:
        logger.error("❌ Failed to fetch DB last datetime: summary_engine is None")
        return None

    try:
        with summary_engine.connect() as conn:
            df = pd.read_sql(query, conn)

        if df.empty or "last_dt" not in df.columns or df["last_dt"].isna().all():
            logger.warning("⚠ DB last datetime not found (stock_summary_1min empty)")
            return None

        last_dt = pd.to_datetime(df["last_dt"].iloc[0], errors="coerce")
        if pd.isna(last_dt):
            logger.warning("⚠ DB last datetime cast failed")
            return None

        if hasattr(last_dt, "to_pydatetime"):
            last_dt = last_dt.to_pydatetime()

        return last_dt

    except Exception:
        logger.exception("❌ Failed to fetch DB last datetime")
        return None


# ============================================================
# 内部：表示用 Anchor データ取得
# ============================================================

def _load_anchor_dataframe(summary_engine, anchor_dt: dt.datetime) -> pd.DataFrame:
    """
    各symbolごとに anchor_dt 以下の最新バーのみ取得
    表示用 anchor
    """

    query = text("""
        SELECT s.*
        FROM stock_summary_1min s
        INNER JOIN (
            SELECT symbol, MAX(datetime) AS max_dt
            FROM stock_summary_1min
            WHERE datetime <= :anchor_dt
            GROUP BY symbol
        ) t
        ON s.symbol = t.symbol
        AND s.datetime = t.max_dt
    """)

    if summary_engine is None:
        logger.error("❌ Anchor query skipped: summary_engine is None")
        return pd.DataFrame()

    try:
        with summary_engine.connect() as conn:
            df = pd.read_sql(query, conn, params={"anchor_dt": anchor_dt})

        return df

    except Exception:
        logger.exception("❌ Anchor query failed")
        return pd.DataFrame()


# ============================================================
# 内部：計算用 history anchor 取得
# ============================================================

def _load_anchor_history_dataframe(
    summary_engine,
    anchor_dt: dt.datetime,
    bars_per_symbol: int = ANCHOR_HISTORY_BARS_PER_SYMBOL,
) -> pd.DataFrame:
    """
    各symbolごとに anchor_dt 以下の最新 N 本を取得
    indicator / scoring 用
    SQLite window function 前提
    """

    query = text(f"""
        SELECT *
        FROM (
            SELECT
                s.*,
                ROW_NUMBER() OVER (
                    PARTITION BY s.symbol
                    ORDER BY s.datetime DESC
                ) AS rn
            FROM stock_summary_1min s
            WHERE s.datetime <= :anchor_dt
        ) z
        WHERE z.rn <= :bars_per_symbol
    """)

    if summary_engine is None:
        logger.error("❌ Anchor history query skipped: summary_engine is None")
        return pd.DataFrame()

    try:
        with summary_engine.connect() as conn:
            df = pd.read_sql(
                query,
                conn,
                params={
                    "anchor_dt": anchor_dt,
                    "bars_per_symbol": int(bars_per_symbol),
                },
            )

        if "rn" in df.columns:
            try:
                df = df.drop(columns=["rn"])
            except Exception:
                logger.debug("[anchor_bootstrap] rn drop failed", exc_info=True)

        return df

    except Exception:
        logger.exception("❌ Anchor history query failed")
        return pd.DataFrame()


# ============================================================
# 内部：Anchor データ整形
# ============================================================

def _prepare_anchor_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    df = df.copy()

    try:
        if "datetime" in df.columns:
            df["datetime"] = (
                pd.to_datetime(df["datetime"], errors="coerce")
                .dt.tz_localize(None)
            )
    except Exception:
        logger.warning("⚠ datetime normalization failed", exc_info=True)

    if "datetime" in df.columns:
        df = df.dropna(subset=["datetime"])

    if "symbol" in df.columns:
        try:
            df["symbol"] = df["symbol"].astype(str).str.strip()
        except Exception:
            logger.warning("⚠ symbol normalization failed", exc_info=True)

    if {"symbol", "datetime"}.issubset(df.columns):
        df = (
            df.sort_values(["symbol", "datetime"])
            .drop_duplicates(["symbol", "datetime"], keep="last")
            .reset_index(drop=True)
        )

    if MAX_ANCHOR_ROWS and len(df) > MAX_ANCHOR_ROWS:
        df = df.tail(MAX_ANCHOR_ROWS).reset_index(drop=True)

    return df


# ============================================================
# 内部：history データ整形
# ============================================================

def _prepare_anchor_history_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    df = df.copy()

    try:
        if "datetime" in df.columns:
            df["datetime"] = (
                pd.to_datetime(df["datetime"], errors="coerce")
                .dt.tz_localize(None)
            )
    except Exception:
        logger.warning("⚠ history datetime normalization failed", exc_info=True)

    if "datetime" in df.columns:
        df = df.dropna(subset=["datetime"])

    if "symbol" in df.columns:
        try:
            df["symbol"] = df["symbol"].astype(str).str.strip()
        except Exception:
            logger.warning("⚠ history symbol normalization failed", exc_info=True)

    if {"symbol", "datetime"}.issubset(df.columns):
        df = (
            df.sort_values(["symbol", "datetime"])
            .drop_duplicates(["symbol", "datetime"], keep="last")
            .reset_index(drop=True)
        )

    return df


# ============================================================
# 内部：global_data 格納
# ============================================================

def _set_anchor_display_to_global(df: pd.DataFrame) -> bool:
    """
    表示用 anchor は merged_summary を上書きしない。
    closed-day 表示専用の別キーへ保存する。
    """
    if df is None or df.empty:
        logger.warning("⚠ display anchor empty -> global_data set skipped")
        return False

    # 1) 専用 setter があれば優先
    try:
        if hasattr(global_data, "set_anchor_display_summary"):
            global_data.set_anchor_display_summary(ANCHOR_INTERVAL, df)
            return True
    except Exception:
        logger.debug("[anchor_bootstrap] set_anchor_display_summary failed", exc_info=True)

    # 2) 汎用 set_data fallback
    try:
        if hasattr(global_data, "set_data"):
            global_data.set_data(ANCHOR_DISPLAY_KEY, df)
            return True
    except Exception:
        logger.debug("[anchor_bootstrap] global_data.set_data(display) failed", exc_info=True)

    # 3) 属性直付け fallback
    try:
        setattr(global_data, ANCHOR_DISPLAY_KEY, df)
        return True
    except Exception:
        logger.exception("❌ Failed to set display anchor to global_data")
        return False


def _set_anchor_history_to_global(df: pd.DataFrame) -> bool:
    if df is None or df.empty:
        logger.warning("⚠ history anchor empty -> global_data set skipped")
        return False

    try:
        if hasattr(global_data, "set_anchor_summary_history"):
            global_data.set_anchor_summary_history(ANCHOR_INTERVAL, df)
            return True
    except Exception:
        logger.debug("[anchor_bootstrap] set_anchor_summary_history failed", exc_info=True)

    try:
        if hasattr(global_data, "set_data"):
            global_data.set_data(ANCHOR_HISTORY_KEY, df)
            return True
    except Exception:
        logger.debug("[anchor_bootstrap] global_data.set_data(history) failed", exc_info=True)

    try:
        setattr(global_data, ANCHOR_HISTORY_KEY, df)
        return True
    except Exception:
        logger.exception("❌ Failed to set anchor history to global_data")
        return False


# ============================================================
# 外部公開：anchor bootstrap
# ============================================================

def bootstrap_anchor():
    logger.info("📌 anchor bootstrap start")

    # 営業日はanchor不要
    try:
        if is_today_business_day():
            logger.info("📌 Today is business day → anchor skipped")
            return
    except Exception:
        logger.exception("❌ Failed to determine business day")
        return

    # 理論終値取得（15:30基準）
    try:
        theoretical_anchor = get_last_market_close_datetime()
    except Exception:
        logger.exception("❌ Failed to get last market close datetime")
        return

    # engine 解決
    summary_engine = _resolve_summary_engine()
    if summary_engine is None:
        logger.warning("⚠ Anchor summary skipped because summary engine unresolved")
        return

    # DB実際最終値取得
    db_last_dt = _get_db_last_datetime(summary_engine)

    if db_last_dt is None:
        logger.warning("⚠ Anchor summary empty (DB has no data)")
        return

    # 安全補正
    anchor_dt = min(theoretical_anchor, db_last_dt)

    logger.info(
        "📌 Anchor summary target theoretical=%s db_last=%s actual=%s",
        theoretical_anchor,
        db_last_dt,
        anchor_dt,
    )

    # --------------------------------------------------------
    # 表示用 anchor（各symbol最新1本）
    # --------------------------------------------------------
    try:
        df_display = _load_anchor_dataframe(summary_engine, anchor_dt)
    except Exception:
        logger.exception("❌ Display anchor query wrapper failed")
        df_display = pd.DataFrame()

    if df_display.empty:
        logger.warning("⚠ Display anchor summary empty")
    else:
        try:
            df_display = _prepare_anchor_dataframe(df_display)
        except Exception:
            logger.exception("❌ Display anchor dataframe preparation failed")
            df_display = pd.DataFrame()

    if not df_display.empty:
        ok_display = _set_anchor_display_to_global(df_display)
        if ok_display:
            logger.info(
                "📌 Display anchor loaded rows=%d interval=%s symbols=%d key=%s",
                len(df_display),
                ANCHOR_INTERVAL,
                df_display["symbol"].nunique() if "symbol" in df_display.columns else -1,
                ANCHOR_DISPLAY_KEY,
            )

    # --------------------------------------------------------
    # 計算用 history anchor（各symbol最新N本）
    # --------------------------------------------------------
    try:
        df_history = _load_anchor_history_dataframe(
            summary_engine,
            anchor_dt,
            bars_per_symbol=ANCHOR_HISTORY_BARS_PER_SYMBOL,
        )
    except Exception:
        logger.exception("❌ History anchor query wrapper failed")
        df_history = pd.DataFrame()

    if df_history.empty:
        logger.warning("⚠ History anchor summary empty")
    else:
        try:
            df_history = _prepare_anchor_history_dataframe(df_history)
        except Exception:
            logger.exception("❌ History anchor dataframe preparation failed")
            df_history = pd.DataFrame()

    if not df_history.empty:
        ok_history = _set_anchor_history_to_global(df_history)
        if ok_history:
            try:
                hist_symbols = df_history["symbol"].nunique() if "symbol" in df_history.columns else -1
                hist_min_dt = df_history["datetime"].min() if "datetime" in df_history.columns else None
                hist_max_dt = df_history["datetime"].max() if "datetime" in df_history.columns else None

                logger.info(
                    "📌 History anchor loaded rows=%d symbols=%d range=[%s .. %s] bars_per_symbol=%s key=%s",
                    len(df_history),
                    hist_symbols,
                    hist_min_dt,
                    hist_max_dt,
                    ANCHOR_HISTORY_BARS_PER_SYMBOL,
                    ANCHOR_HISTORY_KEY,
                )
            except Exception:
                logger.exception("❌ History anchor profile log failed")

    # --------------------------------------------------------
    # 完了判定
    # --------------------------------------------------------
    if df_display.empty and df_history.empty:
        logger.warning("⚠ Anchor bootstrap completed but both display/history anchors are empty")
        return

    logger.info("📌 anchor bootstrap complete")