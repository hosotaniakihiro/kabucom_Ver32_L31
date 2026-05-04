import pandas as pd
import glob
import os
import re
import datetime as dt

from load_summary_1min import load_summary_1min
from load_summary_3min import load_summary_3min
from load_summary_5min import load_summary_5min
from daily_features import build_daily_features
from ranking_loader import load_ranking_features


DB_DIR = "y:/y_stock_data_price"
USE_DAYS = 180


def _extract_date_from_filename(path):
    """summary20250917.db → date"""
    base = os.path.basename(path)
    m = re.search(r"(20\d{6})", base)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except:
        return None


def _safe_concat(dfs):
    return pd.concat([df for df in dfs if df is not None and len(df) > 0],
                     ignore_index=True)


def _load_all_1min():
    files = glob.glob(os.path.join(DB_DIR, "summary*.db"))
    out = []
    for f in sorted(files):
        try:
            df = load_summary_1min(f)
            out.append(df)
        except Exception as e:
            print(f"⚠ 1min 読み込み失敗: {f} → {e}")
    if not out:
        raise ValueError("❌ 1分足が0件です")
    return _safe_concat(out)


def _load_all_3min():
    files = glob.glob(os.path.join(DB_DIR, "summary*.db"))
    out = []
    for f in sorted(files):
        try:
            df = load_summary_3min(f)
            out.append(df)
        except Exception as e:
            print(f"⚠ 3min 読み込み失敗: {f} → {e}")
    if not out:
        raise ValueError("❌ 3分足が0件です")
    return _safe_concat(out)


def _load_all_5min():
    files = glob.glob(os.path.join(DB_DIR, "summary*.db"))
    out = []
    for f in sorted(files):
        try:
            df = load_summary_5min(f)
            out.append(df)
        except Exception as e:
            print(f"⚠ 5min 読み込み失敗: {f} → {e}")
    if not out:
        raise ValueError("❌ 5分足が0件です")
    return _safe_concat(out)


# ==========================================================
# ★ MTF データセット生成
# ==========================================================
def build_mtf_dataset():

    #print("📘 1分足読込中...")
    #df1 = _load_all_1min()

    #print("📘 3分足読込中...")
    #df3 = _load_all_3min()

    print("📘 5分足読込中...")
    df5 = _load_all_5min()

    print("📘 日足生成中...")
    #dfd = build_daily_features()

    print("📘 ランキング読込中...")
    dfr = load_ranking_features()

    # ========== 5分足をベース ==========
    df = df5.copy()

    # ---- date 統一 ----
    df["date"] = pd.to_datetime(df["date"]).dt.date
    dfd["date"] = pd.to_datetime(dfd["date"]).dt.date
    dfr["date"] = pd.to_datetime(dfr["date"]).dt.date

    # ---- 日足
    df = df.merge(dfd, on=["symbol", "date"], how="left")

    # ---- ランキング
    df = df.merge(dfr, on=["symbol", "date"], how="left")

    # ---- 3分足
    df3["date"] = pd.to_datetime(df3["date"]).dt.date
    df3m = df3.rename(columns={
        "open_price": "m3_open",
        "high_price": "m3_high",
        "low_price": "m3_low",
        "close_price": "m3_close",
        "volume": "m3_volume",
        "vwap": "m3_vwap"
    })
    df = df.merge(df3m, on=["symbol", "datetime"], how="left")

    # ---- 1分足
    df1["date"] = pd.to_datetime(df1["date"]).dt.date
    df1m = df1.rename(columns={
        "open_price": "m1_open",
        "high_price": "m1_high",
        "low_price": "m1_low",
        "close_price": "m1_close",
        "volume": "m1_volume",
        "vwap": "m1_vwap"
    })
    df = df.merge(df1m, on=["symbol", "datetime"], how="left")

    # ===== ラベル（未来5分）
    df = df.sort_values(["symbol", "datetime"])
    df["future_close"] = df.groupby("symbol")["close_price"].shift(-1)
    df = df.dropna(subset=["future_close"])

    df["y"] = (df["future_close"] > df["close_price"]).astype(int)

    # ===== 特徴量 =====
    FEATURES = [
        # 5分
        "open_price", "high_price", "low_price", "close_price",
        "volume", "vwap",
        "ma5", "ma25", "ma75",
        "macd", "signal", "rsi", "rci",
        "slowk", "slowd",
        # 1分
        "m1_close", "m1_volume",
        # 3分
        "m3_close", "m3_volume",
        # 日足
        "day_ma25", "day_ma75", "day_rsi",
        "day_pos", "vol_ratio", "day_change",
        # ランキング
        "rank_gain_top20", "rank_vol_top30",
        "rank_gain_speed", "rank_cont_days"
    ]

    # 不足列補完
    for col in FEATURES:
        if col not in df.columns:
            print(f"⚠ 欠損列補完 → {col}")
            df[col] = 0

    X = df[FEATURES].fillna(0)
    y = df["y"]

    return X, y, FEATURES
