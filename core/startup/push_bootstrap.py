# ============================================================
# File   : core/startup/push_bootstrap.py
# Ver    : PRODUCTION-STABLE-REV3-MERGE-READY-PUSH-DF
# ------------------------------------------------------------
# ✔ pushDB 復元（当日分）
# ✔ 最大取得件数制限（メモリ保護）
# ✔ time列安全パース
# ✔ datetime列を必ず補完
# ✔ タイムゾーン安全処理
# ✔ 市場時間外データ除外
# ✔ 列名小文字正規化
# ✔ 例外完全吸収
# ✔ push_df 安全初期化
# ✔ WebSocket memory-only df とマージ可能な形式へ整える
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

MAX_RESTORE_ROWS = int(os.environ.get("PUSH_BOOTSTRAP_MAX_RESTORE_ROWS", "50000"))

# 市場時間（東京証券取引所）
MARKET_OPEN_TIME = dt.time(9, 0)
MARKET_CLOSE_TIME = dt.time(15, 30)


# ============================================================
# 内部：安全時間パース
# ============================================================

def _safe_parse_time(value) -> Optional[dt.datetime]:
    """例外を絶対に投げない time パーサー。"""
    if value is None:
        return None

    try:
        ts = pd.to_datetime(str(value), errors="coerce")
        if pd.isna(ts):
            return None

        # タイムゾーン付きなら東京へ正規化
        try:
            if ts.tzinfo is not None:
                ts = ts.tz_convert("Asia/Tokyo").tz_localize(None)
        except Exception:
            try:
                ts = ts.tz_localize(None)
            except Exception:
                pass

        return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts

    except Exception:
        return None


# ============================================================
# 内部：pushDB 読み込み
# ============================================================

def _load_push_db(db_path: str) -> pd.DataFrame:
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=10000")
            except Exception:
                pass

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


def _normalize_push_df_for_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    # --------------------------------------------------------
    # 列名正規化
    # --------------------------------------------------------
    out.columns = [str(c).strip().lower() for c in out.columns]

    # --------------------------------------------------------
    # symbol補完
    # --------------------------------------------------------
    if "symbol" not in out.columns:
        for c in ("code", "symbol_code", "銘柄コード"):
            if c in out.columns:
                out["symbol"] = out[c]
                break

    if "symbol" in out.columns:
        try:
            out["symbol"] = (
                out["symbol"]
                .astype(str)
                .str.strip()
                .str.upper()
                .str.replace(r"\.T$", "", regex=True)
                .str.replace(r"\.0$", "", regex=True)
            )
        except Exception:
            pass

    # --------------------------------------------------------
    # time / datetime 補完
    # --------------------------------------------------------
    if "time" not in out.columns:
        for c in ("datetime", "timestamp", "current_price_time", "received_at", "inserted_at"):
            if c in out.columns:
                out["time"] = out[c]
                break

    if "time" not in out.columns:
        logger.warning("⚠ pushDB missing time-like column")
        return pd.DataFrame()

    out["time"] = out["time"].apply(_safe_parse_time)
    out = out.dropna(subset=["time"])

    if out.empty:
        logger.warning("⚠ pushDB all time parse failed")
        return pd.DataFrame()

    if "datetime" not in out.columns:
        out["datetime"] = out["time"]
    else:
        try:
            out["datetime"] = out["datetime"].apply(_safe_parse_time)
            out["datetime"] = out["datetime"].fillna(out["time"])
        except Exception:
            out["datetime"] = out["time"]

    # --------------------------------------------------------
    # 市場時間帯フィルタ
    # --------------------------------------------------------
    try:
        out = out[out["time"].dt.time.between(MARKET_OPEN_TIME, MARKET_CLOSE_TIME)]
    except Exception:
        logger.warning("⚠ market time filter failed (skipped)")

    if out.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # 価格列補完
    # --------------------------------------------------------
    if "price" not in out.columns:
        for c in ("current_price", "close", "close_price", "last_price"):
            if c in out.columns:
                out["price"] = out[c]
                break

    if "current_price" not in out.columns and "price" in out.columns:
        out["current_price"] = out["price"]

    # --------------------------------------------------------
    # 重複除外（symbol + datetime）
    # --------------------------------------------------------
    if {"symbol", "datetime"}.issubset(out.columns):
        out = (
            out.sort_values("datetime")
            .drop_duplicates(["symbol", "datetime"], keep="last")
            .reset_index(drop=True)
        )

    # --------------------------------------------------------
    # メモリ保護
    # --------------------------------------------------------
    if len(out) > MAX_RESTORE_ROWS:
        out = out.tail(MAX_RESTORE_ROWS).reset_index(drop=True)

    return out.reset_index(drop=True)


# ============================================================
# 外部公開：push bootstrap
# ============================================================

def bootstrap_push(push_dir: str):
    """
    当日 pushDB を復元し、summary計算/リアルタイムmergeで使える形で
    global_data.push_df へ格納する。
    """

    logger.info("📡 push bootstrap start")

    today_str = dt.datetime.now().strftime("%Y%m%d")
    db_path = os.path.join(push_dir, f"push{today_str}.db")

    # --------------------------------------------------------
    # DB存在確認
    # --------------------------------------------------------
    if not os.path.exists(db_path):
        empty = pd.DataFrame()
        global_data.push_df = empty
        try:
            global_data.set_push_df(empty)
        except Exception:
            pass
        logger.warning("⚠ pushDB not found → push_df empty path=%s", db_path)
        return

    raw = _load_push_db(db_path)

    if raw.empty:
        empty = pd.DataFrame()
        global_data.push_df = empty
        try:
            global_data.set_push_df(empty)
        except Exception:
            pass
        logger.warning("⚠ pushDB empty or failed path=%s", db_path)
        return

    df = _normalize_push_df_for_summary(raw)

    if df.empty:
        empty = pd.DataFrame()
        global_data.push_df = empty
        try:
            global_data.set_push_df(empty)
        except Exception:
            pass
        logger.warning("⚠ pushDB normalize resulted empty raw_rows=%d path=%s", len(raw), db_path)
        return

    # --------------------------------------------------------
    # 安全格納
    # --------------------------------------------------------
    global_data.push_df = df
    try:
        global_data.set_push_df(df)
    except Exception:
        pass

    try:
        global_data.push_bootstrap_db_path = db_path
        global_data.push_bootstrap_rows = int(len(df))
        global_data.push_bootstrap_latest_datetime = df["datetime"].max() if "datetime" in df.columns else None
    except Exception:
        pass

    logger.info(
        "📡 push bootstrap complete rows=%d raw_rows=%d latest=%s limited=%d path=%s",
        len(df),
        len(raw),
        df["datetime"].max() if "datetime" in df.columns else None,
        MAX_RESTORE_ROWS,
        db_path,
    )
