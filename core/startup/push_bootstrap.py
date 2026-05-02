# ============================================================
# File   : core/startup/push_bootstrap.py
# Ver    : PRODUCTION-STABLE-REV2-MARKET-FIXED
# ------------------------------------------------------------
# ✔ pushDB 復元（当日分）
# ✔ 最大取得件数制限（メモリ保護）
# ✔ time列安全パース
# ✔ タイムゾーン安全処理
# ✔ 市場時間外データ除外（★修正：時刻ベース）
# ✔ 列名小文字正規化
# ✔ 例外完全吸収
# ✔ push_df 安全初期化
# ✔ REV9 完全互換
# ============================================================

from __future__ import annotations

import os
import sqlite3
import logging
import datetime as dt
from typing import Optional

import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 設定
# ------------------------------------------------------------

MAX_RESTORE_ROWS = 50000  # メモリ保護上限

# 市場時間（東京証券取引所）
MARKET_OPEN_TIME  = dt.time(9, 0)
MARKET_CLOSE_TIME = dt.time(15, 30)


# ============================================================
# 内部：安全時間パース
# ============================================================

def _safe_parse_time(value) -> Optional[dt.datetime]:
    """
    例外を絶対に投げない time パーサー
    """

    if value is None:
        return None

    try:
        ts = pd.to_datetime(str(value), errors="coerce")
        if pd.isna(ts):
            return None

        # タイムゾーン付きなら東京へ正規化
        if ts.tzinfo is not None:
            ts = ts.tz_convert("Asia/Tokyo").tz_localize(None)

        return ts

    except Exception:
        return None


# ============================================================
# 内部：pushDB 読み込み
# ============================================================

def _load_push_db(db_path: str) -> pd.DataFrame:

    try:
        with sqlite3.connect(db_path) as conn:
            df = pd.read_sql(
                f"""
                SELECT *
                FROM stream_data
                ORDER BY rowid DESC
                LIMIT {MAX_RESTORE_ROWS}
                """,
                conn,
            )
        return df

    except Exception:
        logger.exception("❌ pushDB restore failed")
        return pd.DataFrame()


# ============================================================
# 外部公開：push bootstrap
# ============================================================

def bootstrap_push(push_dir: str):
    """
    ✔ 当日 pushDB を復元
    ✔ 最大50000行制限
    ✔ 不正データ除外
    ✔ 市場時間帯のみ抽出（★正しく修正）
    ✔ push_df に安全格納
    """

    logger.info("📡 push bootstrap start")

    today_str = dt.datetime.now().strftime("%Y%m%d")
    db_path = os.path.join(push_dir, f"push{today_str}.db")

    # --------------------------------------------------------
    # DB存在確認
    # --------------------------------------------------------
    if not os.path.exists(db_path):
        global_data.push_df = pd.DataFrame()
        logger.warning("⚠ pushDB not found → push_df empty")
        return

    df = _load_push_db(db_path)

    if df.empty:
        global_data.push_df = pd.DataFrame()
        logger.warning("⚠ pushDB empty or failed")
        return

    # --------------------------------------------------------
    # 列名正規化
    # --------------------------------------------------------
    df.columns = [str(c).strip().lower() for c in df.columns]

    if "time" not in df.columns:
        global_data.push_df = pd.DataFrame()
        logger.warning("⚠ pushDB missing 'time' column")
        return

    # --------------------------------------------------------
    # time 安全変換
    # --------------------------------------------------------
    df["time"] = df["time"].apply(_safe_parse_time)
    df = df.dropna(subset=["time"])

    if df.empty:
        global_data.push_df = pd.DataFrame()
        logger.warning("⚠ pushDB all time parse failed")
        return

    # --------------------------------------------------------
    # ★ 市場時間帯フィルタ（修正版）
    # --------------------------------------------------------
    try:
        df = df[
            df["time"].dt.time.between(
                MARKET_OPEN_TIME,
                MARKET_CLOSE_TIME
            )
        ]
    except Exception:
        logger.warning("⚠ market time filter failed (skipped)")

    # --------------------------------------------------------
    # 重複除外（symbol + time）
    # --------------------------------------------------------
    if {"symbol", "time"}.issubset(df.columns):
        df = (
            df.sort_values("time")
              .drop_duplicates(["symbol", "time"], keep="last")
              .reset_index(drop=True)
        )

    # --------------------------------------------------------
    # メモリ保護（再制限）
    # --------------------------------------------------------
    if len(df) > MAX_RESTORE_ROWS:
        df = df.tail(MAX_RESTORE_ROWS).reset_index(drop=True)

    # --------------------------------------------------------
    # 安全格納
    # --------------------------------------------------------
    global_data.push_df = df

    logger.info(
        "📡 push bootstrap complete rows=%d (limited=%d)",
        len(df),
        MAX_RESTORE_ROWS,
    )