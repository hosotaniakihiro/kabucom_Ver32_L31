# tools/yahoo/yahoo_downloader.py

"""
Yahoo Finance 1分足ダウンローダー（暫定）
--------------------------------------
・yfinance 使用
・約20分遅延データ
"""

import logging
import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)


def download_yahoo_1min(symbol: str, period="1d") -> pd.DataFrame:
    """
    Yahoo から 1分足を取得
    symbol: "7203" → "7203.T"
    """

    ticker = f"{symbol}.T"

    try:
        df = yf.download(
            ticker,
            period=period,
            interval="1m",
            progress=False,
            auto_adjust=False,
            threads=False,
        )
    except Exception as e:
        logger.error(f"Yahoo download failed: {symbol}", exc_info=True)
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.reset_index()

    # 正規化
    df = df.rename(columns={
        "Datetime": "datetime",
        "Open": "open_price",
        "High": "high_price",
        "Low": "low_price",
        "Close": "close_price",
        "Volume": "volume",
    })

    df["symbol"] = symbol

    return df
