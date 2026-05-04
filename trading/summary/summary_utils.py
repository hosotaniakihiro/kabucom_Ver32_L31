# ============================================================
# summary_utils.py（PUSH → summary 1m/3m/5m 変換ユーティリティ）
# ------------------------------------------------------------
# PUSHの stream_data 行を summary フォーマットへ変換する。
#   - 1分足：PUSH 1ティックをそのまま1分足に変換
#   - 3分足 / 5分足：datetime から time_range を正しく生成
#
# "open/high/low/close" → 今回は全て close_price に寄せる。
# volume / vwap も PUSH由来をそのまま使用。
# ============================================================

import pandas as pd
import numpy as np
import datetime as dt
from database.models import StockSummary5Min as StockSummary

# ------------------------------------------------------------
# PUSH → summary行 変換
# ------------------------------------------------------------
def convert_push_to_summary_row(push_row, interval: int) -> dict:
    """
    stream_data の PUSH行（1ティック）から
    summary互換の1行(dict) を生成する。
    """
    try:
        # =======================================================
        # 必須：symbol / datetime
        # =======================================================
        symbol = str(push_row.get("Symbol") or push_row.get("symbol"))
        if not symbol:
            return None

        # datetimeが無ければ time を使う
        dt_val = push_row.get("time") or push_row.get("Time")
        if dt_val is None:
            return None

        dt_val = pd.to_datetime(dt_val, errors="coerce")
        if pd.isna(dt_val):
            return None

        date = dt_val.date()

        # =======================================================
        # time_range を interval に合わせて生成
        # =======================================================
        if interval == 1:
            time_range = dt_val.strftime("%H:%M")
        else:
            minute = (dt_val.minute // interval) * interval
            dt_bucket = dt_val.replace(minute=minute, second=0, microsecond=0)
            time_range = dt_bucket.strftime("%H:%M")

        # =======================================================
        # OHLCV（PUSHは終値しか無い）
        # =======================================================
        close_price = push_row.get("CurrentPrice")
        if close_price is None:
            return None

        # 1ティックしか無いので OHLC は close一致
        open_p = close_p = close_price
        high_p = low_p = close_price

        volume = push_row.get("TradingVolume")
        vwap = push_row.get("VWAP")

        # =======================================================
        # summary 互換 dict を返す
        # =======================================================
        return {
            "symbol": symbol,
            "symbolname": push_row.get("SymbolName"),
            "date": date,
            "datetime": dt_val,
            "time_range": time_range,

            # OHLCV
            "open_price": open_p,
            "high_price": high_p,
            "low_price": low_p,
            "close_price": close_p,
            "volume": volume,
            "vwap": vwap,

            # PUSH からはテクニカル指標なし
            "ma5": None,
            "ma25": None,
            "ma75": None,
            "macd": None,
            "signal": None,
            "rsi": None,
            "rci": None,
            "bb_upper": None,
            "bb_lower": None,
            "bb_upper_3": None,
            "bb_lower_3": None,
            "slowk": None,
            "slowd": None,
            "atr": None,

            # スコアも無し
            "buy_score": 0.0,
            "short_score": 0.0,
            "pattern_score": 0.0,
            "buy_reasons": "",
            "short_reasons": "",
            "pattern_reasons": "",
        }

    except Exception:
        return None
