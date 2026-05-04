import pandas as pd
import datetime as dt
import logging

logger = logging.getLogger(__name__)


def normalize_push_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    logger.debug(f"[CHECK-PUSH] normalize_push_df INPUT columns={df.columns.tolist()} rows={len(df)}")

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]

    # --------------------------------------------------------
    # 標準列マッピング
    # --------------------------------------------------------
    rename_map = {
        "symbol": "symbol",
        "symbolname": "symbolname",

        "currentprice": "price",
        "lastprice": "price",
        "price": "price",

        "tradingvolume": "volume",
        "volume": "volume",

        "vwap": "vwap",

        "highprice": "high_price",
        "lowprice": "low_price",

        "bidprice": "bid_price",
        "askprice": "ask_price",
        "bidqty": "bid_qty",
        "askqty": "ask_qty",

        "currentpricetime": "current_price_time",
        "pricetime": "price_time",
        "updatetime": "update_time",
    }

    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    # --------------------------------------------------------
    # 必須列
    # --------------------------------------------------------
    for key in ["symbol", "price", "volume"]:
        if key not in df.columns:
            df[key] = None

    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)

    # --------------------------------------------------------
    # 🔥 datetime生成（あなたのISO8601データに完全対応）
    # 優先順位:
    #   1) df["datetime"]（ISO8601含む）
    #   2) df["time"]（ISO8601）
    #   3) Kabus独自 current_price_time / price_time / update_time
    #   4) 最後に now（NaT 防止）
    # --------------------------------------------------------

    dt_col = None

    # ① 既存 datetime が最優先
    if "datetime" in df.columns:
        dt_col = pd.to_datetime(df["datetime"], errors="coerce")

    # ② time も ISO8601 のことが多い（あなたの環境）
    elif "time" in df.columns:
        dt_col = pd.to_datetime(df["time"], errors="coerce")

    # ③ Kabus 時刻
    elif "current_price_time" in df.columns:
        dt_col = pd.to_datetime(df["current_price_time"], errors="coerce")

    elif "price_time" in df.columns:
        dt_col = pd.to_datetime(df["price_time"], errors="coerce")

    elif "update_time" in df.columns:
        dt_col = pd.to_datetime(df["update_time"], errors="coerce")

    # ④ 最終 fallback
    else:
        dt_col = pd.Series([dt.datetime.now()] * len(df))

    # 🔥 NaT を絶対に残さない
    dt_col = dt_col.fillna(dt.datetime.now())

    df["datetime"] = dt_col

    # --------------------------------------------------------
    # 任意列補完
    # --------------------------------------------------------
    optional_cols = [
        "vwap", "trading_value", "high_price", "low_price",
        "bid_price", "ask_price", "bid_qty", "ask_qty",
    ]

    for col in optional_cols:
        if col not in df.columns:
            df[col] = None

    logger.debug(
        f"[CHECK-PUSH] normalize_push_df OUTPUT rows={len(df)} "
        f"symbols={df['symbol'].unique() if 'symbol' in df else 'NONE'}"
    )

    return df
