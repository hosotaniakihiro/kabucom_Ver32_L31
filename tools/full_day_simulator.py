# =============================================================
# full_day_simulator.py（Ver.FINAL — stream_data → OHLCV 完全対応）
# -------------------------------------------------------------
# ・pushYYYYMMDD.db（stream_data）を読み込み
# ・CurrentPrice / TradingVolume から 1分OHLCV を生成
# ・summary_controller.update_from_scheduled に渡す
# ・scoring_engine → BUY/SELL 判定
# ・本番完全再現のデイシミュレーション
# =============================================================

import os
import time
import sqlite3
import pandas as pd
import datetime as dt

from global_state import global_data
from trading.summary.summary_controller import summary_controller
from scoring.scoring_engine import apply_scoring
from trading.handlers.entry_handler import place_entry_buy, place_entry_sell


# =============================================================
# 🔍 PUSH DB 読み込み（stream_data）
# =============================================================
def load_stream_db(db_path, rewrite_to_today=True):
    print(f"📘 Loading stream DB: {db_path}")

    conn = sqlite3.connect(db_path)
    df = pd.read_sql("SELECT * FROM stream_data", conn)
    conn.close()

    # datetime を正規化
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])

    # 今日の日付に書き換え（再生用）
    if rewrite_to_today:
        today = dt.date.today()
        df["datetime"] = df["datetime"].apply(
            lambda x: dt.datetime.combine(today, x.time())
        )

    return df


# =============================================================
# ★ stream_data → 1分OHLCV 変換（あなた専用）
# =============================================================
def convert_stream_to_ohlcv(df_stream: pd.DataFrame, interval: int):
    """
    stream_data（秒足）→ 正しい OHLCV（1/3/5分）を生成
    """

    if df_stream is None or df_stream.empty:
        return pd.DataFrame()

    df = df_stream.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    df_sorted = df.sort_values("datetime")

    # === ★ interval で丸めたバーの開始/終了 ===
    t0 = df_sorted["datetime"].min()
    minute = (t0.minute // interval) * interval
    start_dt = t0.replace(minute=minute, second=0, microsecond=0)
    end_dt = start_dt + dt.timedelta(minutes=interval)

    # === time_range ===
    time_range = f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"

    # === OHLC ===
    open_price = df_sorted["price"].iloc[0]
    close_price = df_sorted["price"].iloc[-1]
    high_price = df_sorted["price"].max()
    low_price = df_sorted["price"].min()

    # === 出来高増加分 ===
    if "volume" in df_sorted.columns:
        dv = df_sorted["volume"].diff().fillna(0)
        volume = dv[dv > 0].sum()
    else:
        volume = 0

    # === VWAP ===
    if "trading_value" in df_sorted.columns and "volume" in df_sorted.columns:
        dv = df_sorted["volume"].diff().fillna(0)
        dtv = df_sorted["trading_value"].diff().fillna(0)
        if dv[dv > 0].sum() > 0:
            vwap = dtv[dv > 0].sum() / dv[dv > 0].sum()
        else:
            vwap = close_price
    else:
        vwap = close_price

    # === 出力（★ datetime を end_dt に統一） ===
    return pd.DataFrame([{
        "symbol": df_sorted["symbol"].iloc[0],
        "symbolname": df_sorted["symbolname"].iloc[0] if "symbolname" in df_sorted else "",
        "date": start_dt.date(),
        "time_range": time_range,
        "start_time": start_dt.strftime("%H:%M:%S"),
        "end_time": end_dt.strftime("%H:%M:%S"),
        "time": end_dt.strftime("%H:%M:%S"),
        "datetime": end_dt,   # ★ 追加 ★
        "open_price": open_price,
        "high_price": high_price,
        "low_price": low_price,
        "close_price": close_price,
        "volume": volume,
        "vwap": vwap,
    }])




# =============================================================
# BUY/SELL 用スコア取得
# =============================================================
def score_symbol(sym):
    df1 = global_data.get_merged_summary(1)
    if df1 is None or df1.empty:
        return 0

    df_sym = df1[df1["symbol"] == sym]
    if df_sym.empty:
        return 0

    df_last = df_sym.tail(1)
    df_scored = apply_scoring(df_last)

    row = df_scored.iloc[-1]
    return max(row.get("score_buy", 0), row.get("score_sell", 0))

# =============================================================
# 🔥 リアルタイム再生
# =============================================================
def replay_full_day(df_stream):

    symbols = sorted(df_stream["symbol"].unique().tolist())
    global_data.symbols = symbols
    print(f"🔧 symbols = {len(symbols)}銘柄セット完了")

    print("🚀 Full-day simulation start!")

    # ==== 09:00 → 15:00 の minute リストを作成 ====
    start_dt = df_stream["datetime"].min().replace(second=0, microsecond=0)
    end_dt = df_stream["datetime"].max().replace(second=0, microsecond=0)

    current_minute = start_dt

    # --- 3分 / 5分 足用のバッファ ---
    buffer_3min = []
    buffer_5min = []

    while current_minute <= end_dt:

        # ★ この minute の stream_data を抽出
        df_min = df_stream[
            (df_stream["datetime"] >= current_minute) &
            (df_stream["datetime"] < current_minute + dt.timedelta(minutes=1))
        ]

        if df_min.empty:
            current_minute += dt.timedelta(minutes=1)
            continue

        # ★ バッファへ追加
        buffer_3min.append(df_min)
        buffer_5min.append(df_min)

        if len(buffer_3min) > 3:
            buffer_3min.pop(0)

        if len(buffer_5min) > 5:
            buffer_5min.pop(0)

        # ================================
        # 1分足
        # ================================
        df_ohlcv_1min = convert_stream_to_ohlcv(df_min, 1)
        summary_controller.update_from_scheduled(1, df_ohlcv_1min)

        print(f"\n===============================")
        print(f"🕒 {current_minute.strftime('%H:%M')} サマリー生成")
        print("===============================")
        print(df_ohlcv_1min[["symbol", "open_price", "close_price", "volume"]])

        # ---------------- BUY / SELL 判定 ----------------
        for sym in df_ohlcv_1min["symbol"].unique():
            score = score_symbol(sym)

            if score >= 5:
                print(f"🔥 BUY signal: {sym} score={score}")
                place_entry_buy(sym)
            elif score <= -5:
                print(f"🔥 SELL signal: {sym} score={score}")
                place_entry_sell(sym)

        # ================================
        # 3分足（正しく集計）
        # ================================
        if current_minute.minute % 3 == 0:
            df_last3 = pd.concat(buffer_3min, ignore_index=True)

            start = df_last3["datetime"].min().floor("min")
            end = df_last3["datetime"].max().floor("min")

            df_ohlcv_3min = convert_stream_to_ohlcv(df_last3, 3)

            df_ohlcv_3min["start_time"] = start
            df_ohlcv_3min["end_time"] = end
            df_ohlcv_3min["datetime"] = end  # ★必須！

            summary_controller.update_from_scheduled(3, df_ohlcv_3min)

        # ================================
        # 5分足（正しく集計）
        # ================================
        if current_minute.minute % 5 == 0:
            df_last3 = pd.concat(buffer_3min, ignore_index=True)

            start = df_last3["datetime"].min().floor("min")
            end = df_last3["datetime"].max().floor("min")

            df_ohlcv_3min = convert_stream_to_ohlcv(df_last3, 5)

            df_ohlcv_3min["start_time"] = start
            df_ohlcv_3min["end_time"] = end
            df_ohlcv_3min["datetime"] = end  # ★必須！

            summary_controller.update_from_scheduled(5, df_ohlcv_3min)

        # ================================================
        current_minute += dt.timedelta(minutes=1)
        time.sleep(0.01)

    print("\n🎉 Full-day simulation finished!")

# =============================================================
# Main
# =============================================================
if __name__ == "__main__":
    date_str = "20251127"
    db_path = f"Y:/stock_price_data/push{date_str}.db"

    df_stream = load_stream_db(db_path)
    replay_full_day(df_stream)
