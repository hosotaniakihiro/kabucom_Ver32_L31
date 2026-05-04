# ============================================================
# yahoo_overwrite_1min.py（Ver19-SAFE-CUTOFF）
# ------------------------------------------------------------
# ・Yahoo 1m（約20分遅れ）で stock_summary_1min を上書き
# ・直近20分は絶対に触らない（PUSH保護）
# ・INSERTは禁止、UPDATE のみ
# ・tz-aware → tz-naive 統一
# ============================================================

import datetime as dt
import pandas as pd
import yfinance as yf
import logging

from database import Session_summary
from database.models import StockSummary1Min
from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================
YAHOO_DELAY_MIN = 20


# ============================================================
# tz-aware → tz-naive
# ============================================================
def to_naive(s):
    try:
        return pd.to_datetime(s, utc=True).dt.tz_convert(None)
    except Exception:
        return pd.to_datetime(s, errors="coerce")


# ============================================================
# Yahoo 1m 取得
# ============================================================
def fetch_yahoo_1min(symbol: str, days=1) -> pd.DataFrame:
    try:
        ticker = yf.Ticker(f"{symbol}.T")
        df = ticker.history(interval="1m", period=f"{days}d")

        if df.empty:
            return pd.DataFrame()

        df = df.reset_index()

        df = df.rename(columns={
            "Datetime": "datetime",
            "Open": "open_price",
            "High": "high_price",
            "Low": "low_price",
            "Close": "close_price",
            "Volume": "volume",
        })

        df["datetime"] = to_naive(df["datetime"])
        df = df.dropna(subset=["datetime"])

        df["date"] = df["datetime"].dt.date
        df["time"] = df["datetime"].dt.time

        return df

    except Exception as e:
        logger.error(f"❌ Yahoo取得失敗 {symbol}: {e}")
        return pd.DataFrame()


# ============================================================
# Yahoo → stock_summary_1min 上書き
# ============================================================
def overwrite_1min_with_yahoo():
    symbols = global_data.symbols
    if not symbols:
        return

    now = dt.datetime.now()
    cutoff = now - dt.timedelta(minutes=YAHOO_DELAY_MIN)

    session = Session_summary()

    updated = 0
    skipped_recent = 0

    for symbol in symbols:

        df_y = fetch_yahoo_1min(symbol, days=1)
        if df_y.empty:
            continue

        # ★ 直近20分を完全に除外
        df_y = df_y[df_y["datetime"] <= cutoff]
        if df_y.empty:
            continue

        for _, r in df_y.iterrows():

            rec = session.query(StockSummary1Min).filter_by(
                symbol=symbol,
                date=r["date"],
                time=r["time"]
            ).first()

            # DBに存在しないバーは触らない（INSERT禁止）
            if not rec:
                continue

            # 念のため再チェック
            bar_dt = dt.datetime.combine(rec.date, rec.time)
            if bar_dt > cutoff:
                skipped_recent += 1
                continue

            # === 上書き ===
            rec.open_price = r["open_price"]
            rec.high_price = r["high_price"]
            rec.low_price = r["low_price"]
            rec.close_price = r["close_price"]
            rec.volume = r["volume"]
            rec.last_update = now

            updated += 1

    try:
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"❌ Yahoo上書き失敗: {e}")
        return

    logger.info(
        f"🟢 Yahoo 1m overwrite 完了 | updated={updated} "
        f"| skipped_recent={skipped_recent}"
    )
