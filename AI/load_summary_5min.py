import sqlite3
import pandas as pd
from load_summary_1min import load_summary_1min


def load_summary_5min(db_path):

    # ① stock_summary_5min が存在する場合はそれを使う
    conn = sqlite3.connect(db_path)
    try:
#        df5 = pd.read_sql("SELECT * FROM stock_summary_5min", conn)
        df5 = pd.read_sql("SELECT * FROM stock_summary", conn)
        conn.close()

        # datetime抽出（time_range or time）
        if "time_range" in df5.columns:
            df5["time"] = df5["time_range"].apply(
                lambda x: x.split(" - ")[0] if isinstance(x, str) else None
            )
        elif "time" in df5.columns:
            df5["time"] = df5["time"]

        df5["datetime"] = pd.to_datetime(df5["date"] + " " + df5["time"],
                                         errors="coerce")
        df5 = df5.dropna(subset=["datetime"])
        df5 = df5.sort_values(["symbol", "datetime"]).reset_index(drop=True)

        return df5

    except Exception:
        conn.close()

    # ② 無い場合は1分足から生成
    df1 = load_summary_1min(db_path)
    df1 = df1.sort_values(["symbol", "datetime"])

    def make_5min(x):
        return pd.DataFrame({
            "symbol": x["symbol"].iloc[0],
            "datetime": x["datetime"].iloc[0],
            "open_price": x["open_price"].iloc[0],
            "high_price": x["high_price"].max(),
            "low_price": x["low_price"].min(),
            "close_price": x["close_price"].iloc[-1],
            "volume": x["volume"].sum(),
            "vwap": (x["vwap"] * x["volume"]).sum() / max(x["volume"].sum(), 1),
        })

    chunks = []
    for sym, g in df1.groupby("symbol"):
        for i in range(0, len(g), 5):
            chunk = g.iloc[i:i+5]
            chunks.append(make_5min(chunk))

    df5 = pd.concat(chunks, ignore_index=True)
    df5 = df5.sort_values(["symbol", "datetime"]).reset_index(drop=True)

    return df5
