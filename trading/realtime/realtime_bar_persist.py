# ============================================================
# realtime_bar_persist.py
# ------------------------------------------------------------
# 確定リアルタイムバー → indicator 計算 → summary DB 保存
# ============================================================

import pandas as pd
import datetime as dt
import logging

from database import Session_summary
from database.models import (
    StockSummary1Min,
    StockSummary3Min,
    StockSummary5Min,
)

from trading.summary.indicators.indicator_calculator import add_all_indicators

logger = logging.getLogger(__name__)

MODEL_MAP = {
    1: StockSummary1Min,
    3: StockSummary3Min,
    5: StockSummary5Min,
}


# ------------------------------------------------------------
def persist_confirmed_bar(symbol: str, tf: int, bar):
    """
    RealtimeBarBuilder から呼ばれるコールバック
    """

    Model = MODEL_MAP.get(tf)
    if not Model:
        return

    try:
        # --- DataFrame化（1本分） ---
        df = pd.DataFrame([{
            "symbol": symbol,
            "datetime": bar.start_dt,
            "date": bar.start_dt.date(),
            "time": bar.start_dt.time(),
            "open_price": bar.open_price,
            "high_price": bar.high_price,
            "low_price": bar.low_price,
            "close_price": bar.close_price,
            "volume": bar.volume,
        }])

        # --- 指標計算 ---
        df = add_all_indicators(df)
        r = df.iloc[0]

        session = Session_summary()

        rec = session.query(Model).filter_by(
            symbol=symbol,
            date=r["date"],
            time=r["time"]
        ).first()

        if not rec:
            session.close()
            return

        # --- OHLCV ---
        rec.open_price = r["open_price"]
        rec.high_price = r["high_price"]
        rec.low_price = r["low_price"]
        rec.close_price = r["close_price"]
        rec.volume = r["volume"]

        # --- 指標（DBに存在するものだけ） ---
        for col in df.columns:
            if hasattr(rec, col):
                setattr(rec, col, r[col])

        rec.last_update = dt.datetime.now()

        session.commit()
        session.close()

    except Exception as e:
        logger.error(f"❌ persist_confirmed_bar error: {e}", exc_info=True)
