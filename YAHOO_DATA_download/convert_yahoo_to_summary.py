import os
import sqlite3
import pandas as pd
import numpy as np
import datetime as dt

from sqlalchemy.orm import Session
from sqlalchemy import create_engine

# ★ Base_summary を必ず import（重要）
from database.models import (
    Base_summary,
    StockSummary1Min,
    StockSummary3Min,
    StockSummary5Min,
)

# ============================================================
# 🔵 過去2日 + 当日の1分足を読み込み（multi-day対応）
# ============================================================

def load_multi_day_1min(symbol, target_date_str,
                        base_dir=r"Y:\y_stock_data_price"):
    """
    前々日・前日・当日の1分足を結合して返す
    指標計算用
    """

    target_date = dt.datetime.strptime(target_date_str, "%Y-%m-%d").date()

    days = [
        target_date - dt.timedelta(days=2),
        target_date - dt.timedelta(days=1),
        target_date
    ]

    df_list = []

    for d in days:
        fn = os.path.join(base_dir, f"summary{d.strftime('%Y%m%d')}.db")
        if not os.path.exists(fn):
            continue

        try:
            conn = sqlite3.connect(fn)
            df = pd.read_sql(f"SELECT * FROM stock_summary WHERE symbol='{symbol}'", conn)
            conn.close()
            if not df.empty:
                df_list.append(df)
        except:
            pass

    if df_list:
        df_all = pd.concat(df_list).sort_values("time_range")
        df_all.reset_index(drop=True, inplace=True)
        return df_all

    return pd.DataFrame()


# ============================================================
# 🔵 指標計算（Ver23 StockSummaryBase 完全対応）
# ============================================================

def calc_indicators(df):
    df = df.copy()

    # ==== MA ====
    df["ma5"] = df["close_price"].rolling(5).mean()
    df["ma25"] = df["close_price"].rolling(25).mean()
    df["ma75"] = df["close_price"].rolling(75).mean()

    # ==== EMA / MACD ====
    df["ema12"] = df["close_price"].ewm(span=12, adjust=False).mean()
    df["ema26"] = df["close_price"].ewm(span=26, adjust=False).mean()
    df["macd"] = df["ema12"] - df["ema26"]
    df["signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["hist"] = df["macd"] - df["signal"]

    # ==== RSI ====
    try:
        import talib
        df["rsi"] = talib.RSI(df["close_price"], timeperiod=14)
    except:
        df["rsi"] = np.nan

    # ==== RCI（長さずれ完全修正済）====
    period = 9
    rci_vals = []

    for i in range(len(df)):
        if i < period - 1:
            rci_vals.append(np.nan)
            continue

        window = df["close_price"].iloc[i - period + 1:i + 1]
        rank_price = window.rank().to_numpy()
        rank_time = np.arange(1, period + 1)

        d = rank_time - rank_price
        rci = (1 - (6 * np.sum(d ** 2)) / (period * (period ** 2 - 1))) * 100
        rci_vals.append(rci)

    df["rci"] = rci_vals

    # ==== VWAP ====
    df["vwap"] = (df["close_price"] * df["volume"]).cumsum() / df["volume"].cumsum()

    # ==== ボリンジャーバンド ====
    df["bb_mid"] = df["close_price"].rolling(20).mean()
    df["bb_width"] = df["close_price"].rolling(20).std()

    df["bb_upper"] = df["bb_mid"] + df["bb_width"] * 1
    df["bb_lower"] = df["bb_mid"] - df["bb_width"] * 1

    df["bb_upper2"] = df["bb_mid"] + df["bb_width"] * 2
    df["bb_lower2"] = df["bb_mid"] - df["bb_width"] * 2

    df["bb_upper3"] = df["bb_mid"] + df["bb_width"] * 3
    df["bb_lower3"] = df["bb_mid"] - df["bb_width"] * 3

    # ==== ATR ====
    df["tr"] = np.maximum.reduce([
        df["high_price"] - df["low_price"],
        (df["high_price"] - df["close_price"].shift()).abs(),
        (df["low_price"] - df["close_price"].shift()).abs()
    ])
    df["atr"] = df["tr"].rolling(14).mean()
    df["atr_ma20"] = df["atr"].rolling(20).mean()
    df["atr_ratio"] = df["atr"] / df["close_price"]
    df["atr_pct"] = df["atr_ratio"] * 100

    # ==== 出来高 ====
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]
    df["vol_surge"] = df["vol_ratio"]
    df["vol_chg"] = df["volume"].pct_change()

    # ==== ギャップ ====
    prev = df["close_price"].shift()
    df["gap_up"] = df["open_price"] - prev
    df["gap_down"] = prev - df["open_price"]
    df["gap_pct"] = df["gap_up"] / prev * 100

    # ==== 方向性 ====
    df["dir_up"] = (df["close_price"] > df["open_price"]).astype(int)
    df["dir_down"] = (df["close_price"] < df["open_price"]).astype(int)

    return df


# ============================================================
# 🔵 OHLCV → 3分足 / 5分足へ変換
# ============================================================

def resample_ohlcv(df, interval):
    df = df.copy()
    df["dt"] = pd.to_datetime(df["time_range"])
    df["bucket"] = df["dt"].dt.floor(f"{interval}min")

    grouped = df.groupby("bucket")

    out = pd.DataFrame({
        "open_price": grouped["open_price"].first(),
        "high_price": grouped["high_price"].max(),
        "low_price": grouped["low_price"].min(),
        "close_price": grouped["close_price"].last(),
        "volume": grouped["volume"].sum(),
        "date": grouped["dt"].first().dt.date.astype(str)
    })

    out = out.reset_index().rename(columns={"bucket": "start"})
    out["end"] = out["start"] + pd.Timedelta(minutes=interval)

    out["time_range"] = (
        out["start"].dt.strftime("%Y-%m-%d %H:%M")
        + " - " +
        out["end"].dt.strftime("%H:%M")
    )

    return out.drop(columns=["start", "end"])


# ============================================================
# 🔵 SQLAlchemy UPSERT（Ver23完全対応）
# ============================================================

def insert_rows(session, model, df, symbol, name):
    df = df.copy()
    df["symbol"] = symbol
    df["symbolname"] = name
    df["last_update"] = dt.datetime.now()

    df = calc_indicators(df)

    for _, r in df.iterrows():
        session.merge(model(
            symbol=symbol,
            symbolname=name,
            date=pd.to_datetime(r["date"]).date(),  # ← 100% 必須
            time_range=r["time_range"],
            start_time=None,
            end_time=None,
            time=None,
            open_price=r["open_price"],
            high_price=r["high_price"],
            low_price=r["low_price"],
            close_price=r["close_price"],
            volume=r["volume"],
            vwap=r.get("vwap"),
            ma5=r.get("ma5"),
            ma25=r.get("ma25"),
            ma75=r.get("ma75"),
            ema12=r.get("ema12"),
            ema26=r.get("ema26"),
            macd=r.get("macd"),
            signal=r.get("signal"),
            hist=r.get("hist"),
            rsi=r.get("rsi"),
            rci=r.get("rci"),
            bb_mid=r.get("bb_mid"),
            bb_upper=r.get("bb_upper"),
            bb_lower=r.get("bb_lower"),
            bb_upper2=r.get("bb_upper2"),
            bb_lower2=r.get("bb_lower2"),
            bb_upper3=r.get("bb_upper3"),
            bb_lower3=r.get("bb_lower3"),
            bb_width=r.get("bb_width"),
            vol_ma20=r.get("vol_ma20"),
            vol_ratio=r.get("vol_ratio"),
            vol_surge=r.get("vol_surge"),
            vol_chg=r.get("vol_chg"),
            atr=r.get("atr"),
            atr_ma20=r.get("atr_ma20"),
            atr_ratio=r.get("atr_ratio"),
            atr_pct=r.get("atr_pct"),
            gap_up=r.get("gap_up"),
            gap_down=r.get("gap_down"),
            gap_pct=r.get("gap_pct"),
            dir_up=r.get("dir_up"),
            dir_down=r.get("dir_down"),
            score_buy=None,
            score_sell=None,
            buy_reasons=None,
            sell_reasons=None,
            small_pullback=None,
            small_rebound=None,
            ma_alignment=None,
            ma_alignment_down=None,
            engulf_bull=None,
            engulf_bear=None,
            bb_expand=None,
            bb_expand_down=None,
            last_update=r["last_update"]
        ))

    session.commit()


# ============================================================
# 🔵 メイン処理（完全版）
# ============================================================

def convert_all():
    Y1M_DIR = r"Y:\y_stock_data_price"
    TARGET_DIR = r"Y:\stock_data_price"

    os.makedirs(TARGET_DIR, exist_ok=True)

    files = sorted([
        f for f in os.listdir(Y1M_DIR)
        if f.startswith("summary")
        and f.endswith(".db")
        and len(f) == len("summaryYYYYMMDD.db")
    ])

    for fn in files:
        print(f"\n📌 {fn} 処理開始")

        src_db = os.path.join(Y1M_DIR, fn)
        conn = sqlite3.connect(src_db)

        # ===== stock_summary あるかチェック =====
        cur = conn.cursor()
        cur.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='stock_summary'
        """)
        if cur.fetchone() is None:
            print(f"⚠ {fn}: stock_summary が存在しないためスキップ")
            conn.close()
            continue

        df1 = pd.read_sql("SELECT * FROM stock_summary", conn)
        conn.close()

        if df1.empty:
            print(f"⚠ {fn}: stock_summary が空 → スキップ")
            continue

        # ===== 出力DB =====
        dst_db = os.path.join(TARGET_DIR, fn)
        engine = create_engine(f"sqlite:///{dst_db}", echo=False)

        # ★ 必須：テーブル自動生成
        Base_summary.metadata.create_all(engine)

        session = Session(engine)

        symbols = df1["symbol"].unique()

        for sym in symbols:
            df_today = df1[df1["symbol"] == sym].copy()
            name = df_today["symbolname"].iloc[0]
            date_str = df_today["date"].iloc[0]

            df_multi = load_multi_day_1min(sym, date_str)
            if df_multi.empty:
                continue

            df1_day = df_multi[df_multi["date"] == date_str]

            with session.no_autoflush:
                insert_rows(session, StockSummary1Min, df1_day, sym, name)

                df3 = resample_ohlcv(df_multi, 3)
                df3_day = df3[df3["date"] == date_str]
                insert_rows(session, StockSummary3Min, df3_day, sym, name)

                df5 = resample_ohlcv(df_multi, 5)
                df5_day = df5[df5["date"] == date_str]
                insert_rows(session, StockSummary5Min, df5_day, sym, name)

        session.close()

        print(f"✔ 完了: {fn}")


if __name__ == "__main__":
    convert_all()
