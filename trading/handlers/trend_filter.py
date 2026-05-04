# trading/handlers/trend_filter.py
import logging
import sqlite3
import pandas as pd
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# =========================================================
# DBから直近1分サマリーを取得
# =========================================================
def get_latest_summary(symbol: str, base_path: str = "y:/stock_price_data/") -> pd.Series | None:
    """
    最新の summaryYYYYMMDD.db から指定銘柄の1分足サマリーデータを取得。
    """
    today = datetime.now().strftime("%Y%m%d")
    db_path = Path(base_path) / f"summary{today}.db"
    if not db_path.exists():
        logger.warning(f"⚠️ サマリーDBが存在しません: {db_path}")
        return None

    try:
        conn = sqlite3.connect(db_path)
        query = f"""
            SELECT *
            FROM stock_summary_1min
            WHERE symbol = ?
            ORDER BY date DESC, time_range DESC
            LIMIT 1
        """
        df = pd.read_sql_query(query, conn, params=(symbol,))
        conn.close()
        if df.empty:
            logger.warning(f"⚠️ サマリーデータなし: {symbol}")
            return None
        return df.iloc[-1]
    except Exception as e:
        logger.error(f"❌ get_latest_summary エラー: {e}", exc_info=True)
        return None


# =========================================================
# 超短期下落トレンド判定ロジック
# =========================================================
def is_short_term_downtrend(latest_row: pd.Series) -> bool:
    """
    超短期（1分足）下落トレンドを検出して True を返す。
    下記のうち2条件以上で下落傾向とみなす。
    """
    if latest_row is None or latest_row.empty:
        return False

    try:
        ma5 = latest_row.get("ma5")
        ma25 = latest_row.get("ma25")
        macd = latest_row.get("macd")
        signal = latest_row.get("signal")
        rsi = latest_row.get("rsi")
        rci = latest_row.get("rci")
        vwap = latest_row.get("vwap")
        close = latest_row.get("close_price")

        conds = [
            ma5 is not None and ma25 is not None and ma5 < ma25,       # 短期MAが下
            macd is not None and signal is not None and macd < signal, # MACDデッドクロス
            rsi is not None and rsi < 45,                              # RSI弱気
            rci is not None and rci < 0,                               # RCI下降
            vwap is not None and close is not None and close < vwap,   # VWAP割れ
        ]

        down_count = sum(1 for c in conds if c)
        if down_count >= 2:
            logger.info(f"⚠️ 短期下落トレンド判定: 条件 {down_count}/5 該当 → BUYスキップ")
            return True
    except Exception as e:
        logger.error(f"❌ is_short_term_downtrend エラー: {e}", exc_info=True)

    return False


def detect_reversal_to_uptrend(df_1min: pd.DataFrame) -> bool:
    """
    短期（1分足）のリバーサル（下落→上昇転換）を検出。
    直近2本のデータを比較し、上昇転換なら True。
    """
    if df_1min is None or len(df_1min) < 2:
        return False

    latest = df_1min.iloc[-1]
    prev = df_1min.iloc[-2]

    try:
        # --- 条件1: MACD ゴールデンクロス
        macd_cross = (
            latest.get("macd") is not None
            and prev.get("macd") is not None
            and latest["macd"] > latest["signal"]
            and prev["macd"] <= prev["signal"]
        )

        # --- 条件2: RSI反転上昇
        rsi_rebound = (
            latest.get("rsi") is not None
            and prev.get("rsi") is not None
            and latest["rsi"] > prev["rsi"]
            and prev["rsi"] < 45
        )

        # --- 条件3: 終値がVWAPを上抜け
        vwap_break = (
            latest.get("close_price") is not None
            and latest.get("vwap") is not None
            and prev.get("close_price") is not None
            and prev["close_price"] < prev["vwap"]
            and latest["close_price"] > latest["vwap"]
        )

        # --- 条件4: MA5がMA25を上抜け
        ma_cross = (
            latest.get("ma5") is not None
            and latest.get("ma25") is not None
            and prev.get("ma5") is not None
            and prev.get("ma25") is not None
            and prev["ma5"] <= prev["ma25"]
            and latest["ma5"] > latest["ma25"]
        )

        score = sum([macd_cross, rsi_rebound, vwap_break, ma_cross])
        if score >= 2:
            logger.info(f"✨ 上昇転換シグナル検出: 条件 {score}/4 該当 → BUY許可")
            return True
        return False

    except Exception as e:
        logger.error(f"❌ detect_reversal_to_uptrend エラー: {e}", exc_info=True)
        return False


def get_recent_summary(symbol: str, n: int = 3, base_path: str = "y:/stock_price_data/") -> pd.DataFrame | None:
    """
    指定銘柄の直近n件（デフォルト3件）の1分サマリーデータを取得。
    detect_reversal_to_uptrend で使用。
    """
    from datetime import datetime
    from pathlib import Path
    import sqlite3

    today = datetime.now().strftime("%Y%m%d")
    db_path = Path(base_path) / f"summary{today}.db"
    if not db_path.exists():
        logger.warning(f"⚠️ サマリーDBなし: {db_path}")
        return None

    try:
        conn = sqlite3.connect(db_path)
        query = f"""
            SELECT *
            FROM stock_summary_1min
            WHERE symbol = ?
            ORDER BY date DESC, time_range DESC
            LIMIT {n}
        """
        df = pd.read_sql_query(query, conn, params=(symbol,))
        conn.close()
        if df.empty:
            return None
        return df.sort_values(["date", "time_range"]).reset_index(drop=True)
    except Exception as e:
        logger.error(f"❌ get_recent_summary エラー: {e}", exc_info=True)
        return None
