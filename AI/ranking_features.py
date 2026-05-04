# AI/ranking_features.py
import sqlite3
import pandas as pd
import glob
import os

def load_ranking_features(base_dir="Y:/stock_price_data"):
    """
    過去のランキングDBを読み込み、
    symbol × date のランキング特徴量を作成する。

    戻り値：DataFrame
    columns:
        symbol
        date
        rank_値上がり率_ALL
        rank_値上がり率_TP
        rank_売買代金急増_ALL
        ...
    """
    files = sorted(glob.glob(os.path.join(base_dir, "ranking*.db")))
    if not files:
        print("⚠ ランキングDBなし")
        return pd.DataFrame()

    all_rows = []

    for db_path in files:
        date_str = os.path.basename(db_path).replace("ranking", "").replace(".db", "")
        date = pd.to_datetime(date_str, format="%Y%m%d", errors="coerce")

        conn = sqlite3.connect(db_path)
        tables = pd.read_sql("SELECT name FROM sqlite_master WHERE type='table'", conn)

        for t in tables["name"]:
            try:
                df = pd.read_sql(f"SELECT symbol, current_price, trading_volume FROM '{t}'", conn)
            except:
                continue

            # テーブル名 → 種別と市場に分ける
            try:
                type_name, market = t.split("_")
            except:
                continue

            df["date"] = date
            df["rank"] = df.index + 1
            df["type"] = type_name
            df["market"] = market

            all_rows.append(df[["symbol", "date", "rank", "type", "market"]])

        conn.close()

    if not all_rows:
        return pd.DataFrame()

    df_all = pd.concat(all_rows, ignore_index=True)

    # ピボットして特徴量にする
    df_features = df_all.pivot_table(
        index=["symbol", "date"],
        columns=["type", "market"],
        values="rank",
        aggfunc="min"
    )

    # カラム名フラット化
    df_features.columns = [
        f"rank_{t[0]}_{t[1]}" for t in df_features.columns
    ]

    df_features = df_features.reset_index()

    return df_features
