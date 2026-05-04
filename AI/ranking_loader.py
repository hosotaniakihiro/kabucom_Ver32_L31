import sqlite3
import pandas as pd
import glob
import os
import re
import datetime as dt


RANKING_DIR = "y:/stock_price_data"     # ← あなたの環境に合わせる
USE_DAYS = 120                          # 直近120日分使用


def _extract_date_from_filename(path):
    base = os.path.basename(path)
    m = re.search(r"(20\d{6})", base)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except:
        return None


def load_ranking_features():

    # ranking*.db を取得
    files = glob.glob(os.path.join(RANKING_DIR, "ranking*.db"))
    if not files:
        raise ValueError("❌ ranking*.db が見つかりません。")

    today = dt.date.today()
    cutoff = today - dt.timedelta(days=USE_DAYS)

    valid_files = []
    for f in files:
        d = _extract_date_from_filename(f)
        if d and d >= cutoff:
            valid_files.append(f)

    if not valid_files:
        raise ValueError("❌ 直近120日のランキングDBがありません。")

    df_all = []

    for f in sorted(valid_files):
        try:
            conn = sqlite3.connect(f)

            # 各ランキングテーブルを読み込み（存在しない場合もあるので例外は無視）
            df_gain = pd.read_sql("SELECT * FROM ranking_gain LIMIT 100", conn)
            df_vol = pd.read_sql("SELECT * FROM ranking_volume LIMIT 100", conn)
            df_speed = pd.read_sql("SELECT * FROM ranking_gain_speed LIMIT 100", conn)
            df_cont = pd.read_sql("SELECT * FROM ranking_continuous LIMIT 100", conn)

            # フラグ化：TOP20 / TOP30入賞
            df_gain["rank_gain_top20"] = (df_gain["rank"] <= 20).astype(int)
            df_vol["rank_vol_top30"] = (df_vol["rank"] <= 30).astype(int)
            df_speed["rank_gain_speed"] = (df_speed["rank"] <= 20).astype(int)

            # 連続上昇日数
            df_cont["rank_cont_days"] = df_cont["cont_days"]

            # 必要列だけ残して日付と銘柄でJOINできるように
            df_gain = df_gain[["symbol", "date", "rank_gain_top20"]]
            df_vol = df_vol[["symbol", "date", "rank_vol_top30"]]
            df_speed = df_speed[["symbol", "date", "rank_gain_speed"]]
            df_cont = df_cont[["symbol", "date", "rank_cont_days"]]

            # 1日分にまとめる
            df_day = df_gain.merge(df_vol, on=["symbol", "date"], how="outer")
            df_day = df_day.merge(df_speed, on=["symbol", "date"], how="outer")
            df_day = df_day.merge(df_cont, on=["symbol", "date"], how="outer")

            df_all.append(df_day)

        except Exception as e:
            print(f"⚠ ランキング読込失敗: {f} → {e}")

        finally:
            conn.close()

    df = pd.concat(df_all, ignore_index=True)

    # 欠損を0埋め
    df = df.fillna(0)

    # ソート
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)

    return df
