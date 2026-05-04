# ============================================================
# build_yahoo_to_summary_db.py
# ============================================================

import sys
import logging
import pandas as pd
import datetime as dt
import yfinance as yf
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

from database import Session_summary
from database.models import (
    StockSummary1Min,
    StockSummary3Min,
    StockSummary5Min,
)

from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.utils.utils_time import get_yahoo_border_time
from trading.ranking.yahoo_symbol_selector import build_yahoo_target_symbols


YAHOO_DAYS = 3


def to_naive(s):
    try:
        return pd.to_datetime(s, utc=True).dt.tz_convert(None)
    except Exception:
        return pd.to_datetime(s, errors="coerce")


def fetch_yahoo_1m(symbol, days):
    try:
        df = yf.Ticker(f"{symbol}.T").history(
            interval="1m",
            period=f"{days}d",
            auto_adjust=False,
            actions=False,
        )
        if df.empty:
            return pd.DataFrame()

        df = df.reset_index().rename(columns={
            "Datetime": "datetime",
            "Open": "open_price",
            "High": "high_price",
            "Low": "low_price",
            "Close": "close_price",
            "Volume": "volume",
        })

        df["datetime"] = to_naive(df["datetime"])
        df["symbol"] = symbol
        df["date"] = df["datetime"].dt.date
        df["time"] = df["datetime"].dt.time
        return df

    except Exception as e:
        logger.warning(f"Yahoo skip {symbol}: {e}")
        return pd.DataFrame()


def resample_tf(df, tf):
    if df.empty:
        return df
    d = df.set_index("datetime")
    out = d.resample(f"{tf}min").agg({
        "open_price": "first",
        "high_price": "max",
        "low_price": "min",
        "close_price": "last",
        "volume": "sum",
        "symbol": "last",
        "date": "last",
    }).dropna()
    out = out.reset_index()
    out["time"] = out["datetime"].dt.time
    return out


def upsert_summary(session, model, r, tf):
    border = get_yahoo_border_time()
    dt_bar = pd.to_datetime(
        r["datetime"] if "datetime" in r else f"{r['date']} {r['time']}",
        errors="coerce"
    )
    if pd.isna(dt_bar) or dt_bar > border:
        return

    rec = session.query(model).filter_by(
        symbol=r["symbol"],
        date=r["date"],
        time=r["time"]
    ).first()

    if not rec:
        rec = model(symbol=r["symbol"], date=r["date"], time=r["time"])
        session.add(rec)

    t = r["time"]
    if tf == 1:
        s = dt.datetime.combine(r["date"], t)
        e = s + dt.timedelta(minutes=1)
    else:
        e = dt.datetime.combine(r["date"], t)
        s = e - dt.timedelta(minutes=tf)

    rec.start_time = s.time()
    rec.end_time = e.time()
    rec.time_range = f"{s:%H:%M} - {e:%H:%M}"

    rec.open_price = r["open_price"]
    rec.high_price = r["high_price"]
    rec.low_price = r["low_price"]
    rec.close_price = r["close_price"]
    rec.volume = r["volume"]

    for c in r.index:
        if hasattr(rec, c):
            v = r[c]
            setattr(rec, c, None if pd.isna(v) else v)

    rec.last_update = dt.datetime.now()


def main():
    symbols = build_yahoo_target_symbols()
    print(f"📘 Yahoo対象銘柄数: {len(symbols)}")

    session = Session_summary()

    for s in symbols:
        print(f"📥 Yahoo処理: {s}")
        df1 = fetch_yahoo_1m(s, YAHOO_DAYS)
        if df1.empty:
            continue

        df1 = add_all_indicators(df1)
        df3 = add_all_indicators(resample_tf(df1, 3))
        df5 = add_all_indicators(resample_tf(df1, 5))

        for _, r in df1.iterrows():
            upsert_summary(session, StockSummary1Min, r, 1)
        for _, r in df3.iterrows():
            upsert_summary(session, StockSummary3Min, r, 3)
        for _, r in df5.iterrows():
            upsert_summary(session, StockSummary5Min, r, 5)

        session.commit()

    session.close()
    print("✅ Yahoo → Summary DB 完了")


if __name__ == "__main__":
    main()
