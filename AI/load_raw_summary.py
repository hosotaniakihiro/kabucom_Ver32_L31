import sqlite3
import pandas as pd
import re
import os


def _safe_read_table(conn, table_name):
    """テーブルが存在すれば読み込む。無ければ None を返す。"""
    try:
        return pd.read_sql(f"SELECT * FROM {table_name}", conn)
    except Exception:
        return None


def _extract_hhmm(x):
    """time または time_range から HH:MM を抽出"""
    if isinstance(x, str):
        # time_range: "09:00 - 09:01"
        if " - " in x:
            hhmm = x.split(" - ")[0]
            return hhmm

        # 0900 や 900
        if re.fullmatch(r"\d{3,4}", x):
            x = x.zfill(4)
            return x[:2] + ":" + x[2:]
    return None


def load_raw_summary(db_path):
    """
    summaryDB（どんな形式でもOK）から
    ★ 1分足相当の“生”データを返す。
    """

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"❌ {db_path} が存在しません")

    conn = sqlite3.connect(db_path)

    # ① stock_summary_1min があるか？
    df_1 = _safe_read_table(conn, "stock_summary_1min")

    # ② ない場合は stock_summary から作る
    if df_1 is None:
        df_raw = _safe_read_table(conn, "stock_summary")
        conn.close()

        if df_raw is None:
            raise ValueError("❌ stock_summary が存在しません")

        # symbol/date/time_range が必須
        # time_range が無い可能性もあるため time を確認
        if "time_range" in df_raw.columns:
            df_raw["time"] = df_raw["time_range"].apply(_extract_hhmm)
        elif "time" in df_raw.columns:
            df_raw["time"] = df_raw["time"].apply(_extract_hhmm)
        else:
            raise ValueError("❌ time も time_range も存在しません")

        df_raw = df_raw.dropna(subset=["time", "date"])

        df_raw["datetime"] = pd.to_datetime(df_raw["date"] + " " + df_raw["time"],
                                            errors="coerce")
        df_raw = df_raw.dropna(subset=["datetime"])

        # このまま“1分足相当”として返す
        return df_raw

    # ③ stock_summary_1min がある場合：そのまま使用
    conn.close()

    # time_range 形式なので正規化
    if "time_range" in df_1.columns:
        df_1["time"] = df_1["time_range"].apply(_extract_hhmm)
        df_1["datetime"] = pd.to_datetime(
            df_1["date"] + " " + df_1["time"],
            errors="coerce"
        )
        df_1 = df_1.dropna(subset=["datetime"])

    return df_1
