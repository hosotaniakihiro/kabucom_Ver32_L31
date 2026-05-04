# ============================================================
# trading/yahoo/complement.py
# Ver27-ABSOLUTE-UNIFIED-SAVE
# ------------------------------------------------------------
# ✔ RESTORE方式: bulk_upsert_summary 経由で安全保存
# ✔ HYBRID方式: DataFrameマージのみ
# ✔ tz-aware → tz-naive 完全統一
# ✔ ORM直接INSERT完全廃止
# ✔ date / datetime NOT NULL 永久防止
# ✔ 既存機能削除ゼロ
# ============================================================

from __future__ import annotations

import pandas as pd
import datetime as dt
import yfinance as yf
import logging

from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# 🔧 tz-aware → tz-naive
# ============================================================
def _to_naive(dt_series):
    try:
        return pd.to_datetime(dt_series, utc=True).dt.tz_convert(None)
    except Exception:
        return pd.to_datetime(dt_series, errors="coerce")


# ============================================================
# 🔥 Yahoo 1min取得
# ============================================================
def _fetch_yahoo_1min(symbol: str, days=1):

    try:
        ticker = yf.Ticker(f"{symbol}.T")
        df = ticker.history(interval="1m", period=f"{days}d")

        if df.empty:
            return pd.DataFrame()

        df = df.reset_index()

        df = df.rename(columns={
            "Datetime": "datetime",
            "Open": "open_price",
            "High": "high_price",
            "Low": "low_price",
            "Close": "close_price",
            "Volume": "volume",
        })

        df["datetime"] = _to_naive(df["datetime"])

        df["symbol"] = symbol
        df["date"] = df["datetime"].dt.date
        df["time"] = df["datetime"].dt.time

        # VWAP
        try:
            df["vwap"] = (
                (df["close_price"] * df["volume"]).cumsum()
                / df["volume"].cumsum()
            )
        except Exception:
            df["vwap"] = df["close_price"]

        return df

    except Exception as e:
        logger.error(f"❌ Yahoo取得エラー {symbol}: {e}")
        return pd.DataFrame()


# ============================================================
# 🔥 1min → HTF変換
# ============================================================
def _resample_tf(df_1m: pd.DataFrame, tf_min: int):

    if df_1m.empty:
        return pd.DataFrame()

    df = df_1m.copy()
    df["datetime"] = _to_naive(df["datetime"])
    df = df.set_index("datetime")

    df_tf = df.resample(f"{tf_min}T").agg({
        "open_price": "first",
        "high_price": "max",
        "low_price": "min",
        "close_price": "last",
        "volume": "sum",
        "vwap": "last",
        "symbol": "last",
    })

    df_tf = df_tf.dropna(subset=["open_price"]).reset_index()

    df_tf["date"] = df_tf["datetime"].dt.date
    df_tf["time"] = df_tf["datetime"].dt.time

    return df_tf


# ============================================================
# 🔥 最新datetime取得
# ============================================================
def _summary_latest_dt(df):

    if df is None or df.empty:
        return None

    if "datetime" in df.columns:
        return pd.to_datetime(df["datetime"], errors="coerce").max()

    if "date" in df.columns and "time" in df.columns:
        dt_series = pd.to_datetime(
            df["date"].astype(str) + " " + df["time"].astype(str),
            errors="coerce"
        )
        return dt_series.max()

    return None


# ============================================================
# ============================================================
# 【A】RESTORE方式（安全版）
# ============================================================
# ============================================================
def yahoo_complement():

    symbols = global_data.symbols
    if not symbols:
        return

    df1 = global_data.get_merged_summary(1)
    latest_dt = _summary_latest_dt(df1)

    if latest_dt is None:
        latest_dt = dt.datetime.now().replace(
            hour=9, minute=0, second=0, microsecond=0
        )

    added_total = 0

    for symbol in symbols:

        df_1m = _fetch_yahoo_1min(symbol, days=1)
        if df_1m.empty:
            continue

        df_new = df_1m[df_1m["datetime"] > latest_dt]
        if df_new.empty:
            continue

        added_total += len(df_new)

        # ===== 1min保存 =====
        bulk_upsert_summary(df_new, interval=1)

        # ===== 3min保存 =====
        df_3m = _resample_tf(df_new, 3)
        if not df_3m.empty:
            bulk_upsert_summary(df_3m, interval=3)

        # ===== 5min保存 =====
        df_5m = _resample_tf(df_new, 5)
        if not df_5m.empty:
            bulk_upsert_summary(df_5m, interval=5)

    logger.info(f"🔵 Yahoo補完(UNIFIED SAVE): 追加バー={added_total}")


# ============================================================
# ============================================================
# 【B】HYBRID方式（DB書込なし）
# ============================================================
# ============================================================
def build_yahoo_merged(df1, df3, df5):

    symbols = global_data.symbols
    if not symbols:
        return df1, df3, df5

    latest_dt = _summary_latest_dt(df1)
    if latest_dt is None:
        latest_dt = dt.datetime.now().replace(
            hour=9, minute=0, second=0, microsecond=0
        )

    df1_new_all, df3_new_all, df5_new_all = [], [], []

    for symbol in symbols:

        df_y = _fetch_yahoo_1min(symbol)
        if df_y.empty:
            continue

        df_new = df_y[df_y["datetime"] > latest_dt]
        if df_new.empty:
            continue

        df1_new_all.append(df_new)

        df_3m = _resample_tf(df_new, 3)
        if not df_3m.empty:
            df3_new_all.append(df_3m)

        df_5m = _resample_tf(df_new, 5)
        if not df_5m.empty:
            df5_new_all.append(df_5m)

    df1_new = pd.concat(df1_new_all, ignore_index=True) if df1_new_all else pd.DataFrame()
    df3_new = pd.concat(df3_new_all, ignore_index=True) if df3_new_all else pd.DataFrame()
    df5_new = pd.concat(df5_new_all, ignore_index=True) if df5_new_all else pd.DataFrame()

    df1_merged = pd.concat([df1, df1_new], ignore_index=True)
    df3_merged = pd.concat([df3, df3_new], ignore_index=True)
    df5_merged = pd.concat([df5, df5_new], ignore_index=True)

    def _clean(df):
        if df is None or df.empty:
            return df
        df = df.sort_values("datetime")
        df = df.drop_duplicates(subset=["symbol", "datetime"], keep="last")
        return df.reset_index(drop=True)

    return _clean(df1_merged), _clean(df3_merged), _clean(df5_merged)