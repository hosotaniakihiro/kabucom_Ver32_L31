import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

def load_yahoo_1min(symbols, days=5):
    """
    symbols: ["7203.T", "9984.T"]
    """
    dfs = []
    end = datetime.now()
    start = end - timedelta(days=days)

    for sym in symbols:
        try:
            df = yf.download(
                sym,
                interval="1m",
                start=start,
                end=end,
                progress=False
            )

            if df.empty:
                continue

            df = df.reset_index()
            df = df.rename(columns={
                "Datetime": "datetime",
                "Open": "open_price",
                "High": "high_price",
                "Low": "low_price",
                "Close": "close_price",
                "Volume": "volume",
            })

            df["symbol"] = sym.replace(".T", "")
            df = df[
                ["symbol", "datetime", "open_price",
                 "high_price", "low_price",
                 "close_price", "volume"]
            ]

            dfs.append(df)

        except Exception as e:
            print(f"[Yahoo ERROR] {sym}: {e}")

    if not dfs:
        return pd.DataFrame()

    return pd.concat(dfs, ignore_index=True)
