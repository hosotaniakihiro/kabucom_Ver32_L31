# ============================================================
# yahoo_to_summary.py（Ver18-FINAL-ROBUST）
# Yahoo の 1分OHLCV → 1分 / 3分 / 5分 サマリーへ完全変換
# ・MultiIndex columns対応
# ・open/high/low/close の全パターン対応
# ・VWAP安全計算
# ・FutureWarning 対策（T → min）
# ============================================================

import pandas as pd
import numpy as np
import logging
import datetime as dt

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# Yahooカラム完全正規化（全パターン吸収）
# ------------------------------------------------------------
def normalize_yahoo_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Yahoo DF をサマリー形式：
        symbol, time, open_price, high_price, low_price, close_price, volume
    に強制整形する。
    MultiIndexでも単階層でも100%対応。
    """
    if df.empty:
        return df

    df = df.copy()

    # ---------------------------------------
    # 1) MultiIndex columns の flatten
    # ---------------------------------------
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ['_'.join([str(c) for c in col if c]) for col in df.columns]

    # ---------------------------------------
    # 2) 代表的な Yahoo列名を rename
    #    flatten後も対応
    # ---------------------------------------
    rename_map = {
        "Datetime": "time",
        "datetime": "time",
        "Timestamp": "time",
        "timestamp": "time",

        # 単階層
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "close_price",

        # flatten後（例: close_2432.T）
        # 値に “close_” を含む列は後で抽出するため ignore
    }
    df = df.rename(columns=rename_map)

    # ---------------------------------------
    # 3) symbol 列がない場合 → 自動推定
    # ---------------------------------------
    if "symbol" not in df.columns:
        # flatten で "symbol_2432.T" みたいな列が生まれる
        symbol_cols = [c for c in df.columns if "symbol" in c]
        if symbol_cols:
            df["symbol"] = df[symbol_cols[0]]
        else:
            logger.error("❌ normalize: symbol 列が見つかりません")
            logger.error(f"columns = {df.columns.tolist()}")
            return pd.DataFrame()

    # ---------------------------------------
    # 4) open/high/low/close/volume の抽出
    #    (flattenされた列名にも対応)
    # ---------------------------------------
    def extract_one(prefix):
        # 例: "open_price", "open_price_2432.T"
        cand = [c for c in df.columns if c.startswith(prefix)]
        if not cand:
            return None
        s = df[cand[0]]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        return s.astype(float)

    open_s  = extract_one("open_price")
    high_s  = extract_one("high_price")
    low_s   = extract_one("low_price")
    close_s = extract_one("close_price")
    vol_s   = extract_one("volume")

    required = [open_s, high_s, low_s, close_s, vol_s]
    if any(s is None for s in required):
        logger.error("❌ normalize: 必須 OHLCV 列を抽出できません")
        logger.error(f"columns = {df.columns.tolist()}")
        return pd.DataFrame()

    # ---------------------------------------
    # 5) time 列の正規化
    # ---------------------------------------
    if "time" not in df.columns:
        time_cols = [c for c in df.columns if "time" in c.lower()]
        if time_cols:
            df["time"] = df[time_cols[0]]
        else:
            logger.error("❌ normalize: time列が見つかりません")
            return pd.DataFrame()

    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"])

    # ---------------------------------------
    # 6) 必要なカラムで再構築
    # ---------------------------------------
    df_out = pd.DataFrame({
        "symbol": df["symbol"].astype(str),
        "time": df["time"],
        "open_price": open_s,
        "high_price": high_s,
        "low_price": low_s,
        "close_price": close_s,
        "volume": vol_s.astype(float)
    })

    return df_out


# ------------------------------------------------------------
# 1分足サマリー
# ------------------------------------------------------------
def build_1min_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["date"] = df["time"].dt.date

    df["time_range"] = (
        df["time"].dt.strftime("%H:%M") + " - " +
        (df["time"] + pd.Timedelta(minutes=1)).dt.strftime("%H:%M")
    )

    # VWAP safe calc
    vwap_raw = df["close_price"] * df["volume"]
    vwap_raw = vwap_raw.replace({0: np.nan})
    df["vwap"] = vwap_raw / df["volume"].replace({0: np.nan})

    return df[
        [
            "symbol", "date", "time_range",
            "open_price", "high_price", "low_price",
            "close_price", "volume", "vwap"
        ]
    ].copy()


# ------------------------------------------------------------
# 3分 / 5分足生成
# ------------------------------------------------------------
def resample_minutes(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    df = df.copy().set_index("time")

    ohlc_dict = {
        "open_price": "first",
        "high_price": "max",
        "low_price": "min",
        "close_price": "last",
        "volume": "sum",
    }

    df_res = df.resample(f"{interval}min").agg(ohlc_dict)
    df_res = df_res.dropna(subset=["open_price"])
    df_res = df_res.reset_index()

    symbol = df["symbol"].iloc[0]
    df_res["symbol"] = symbol

    df_res["date"] = df_res["time"].dt.date
    df_res["time_range"] = (
        df_res["time"].dt.strftime("%H:%M") + " - " +
        (df_res["time"] + pd.Timedelta(minutes=interval)).dt.strftime("%H:%M")
    )

    df_res["vwap"] = (
        (df_res["close_price"] * df_res["volume"]).replace({0: np.nan})
        / df_res["volume"].replace({0: np.nan})
    )

    return df_res[
        [
            "symbol", "date", "time_range",
            "open_price", "high_price", "low_price",
            "close_price", "volume", "vwap"
        ]
    ]


# ------------------------------------------------------------
# メイン：1/3/5分サマリー生成
# ------------------------------------------------------------
def yahoo_build_all_summaries(df_yahoo_1min: pd.DataFrame) -> dict:

    df = normalize_yahoo_columns(df_yahoo_1min)
    if df.empty:
        return {"1min": pd.DataFrame(), "3min": pd.DataFrame(), "5min": pd.DataFrame()}

    df_1 = build_1min_summary(df)
    df_3 = resample_minutes(df, 3)
    df_5 = resample_minutes(df, 5)

    return {"1min": df_1, "3min": df_3, "5min": df_5}
