import pandas as pd
from load_summary_1min import load_summary_1min


def load_summary_3min(db_path):
    df1 = load_summary_1min(db_path)

    df1 = df1.sort_values(["symbol", "datetime"])

    def make_3min(x):
        return pd.DataFrame({
            "symbol": x["symbol"].iloc[0],
            "datetime": x["datetime"].iloc[0],          # 最初の時間
            "open_price": x["open_price"].iloc[0],
            "high_price": x["high_price"].max(),
            "low_price": x["low_price"].min(),
            "close_price": x["close_price"].iloc[-1],
            "volume": x["volume"].sum(),
            "vwap": (x["vwap"] * x["volume"]).sum() / max(x["volume"].sum(), 1),
        })

    chunks = []
    for sym, g in df1.groupby("symbol"):
        for i in range(0, len(g), 3):
            chunk = g.iloc[i:i+3]
            if len(chunk) > 0:
                chunks.append(make_3min(chunk))

    df3 = pd.concat(chunks, ignore_index=True)
    df3 = df3.sort_values(["symbol", "datetime"]).reset_index(drop=True)
    return df3
