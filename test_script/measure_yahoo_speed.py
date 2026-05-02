# ============================================================
# measure_yahoo_speed.py
# ------------------------------------------------------------
# Yahoo 1分足取得時間の単体計測用（営業日対応版）
# ・最新ランキング銘柄のみ取得
# ・今日が祝日なら直近営業日を取得
# ・過去日なので 20分遅延なし
# ============================================================

import time
import datetime as dt
import pandas as pd
import yfinance as yf
import jpholiday

from sqlalchemy import func
from database import Session_ranking
from database.models import RankingRaw1Min


# ============================================================
# 営業日判定
# ============================================================

def is_business_day(d: dt.date) -> bool:
    return d.weekday() < 5 and not jpholiday.is_holiday(d)


def get_latest_business_day(d: dt.date) -> dt.date:
    while not is_business_day(d):
        d -= dt.timedelta(days=1)
    return d


# ============================================================
# 最新ランキング銘柄取得
# ============================================================

def get_latest_ranking_symbols():
    symbols = []

    with Session_ranking() as s:

        latest_time = (
            s.query(func.max(RankingRaw1Min.snapshot_time))
            .scalar()
        )

        if not latest_time:
            return []

        rows = (
            s.query(RankingRaw1Min.symbol)
            .filter(RankingRaw1Min.snapshot_time == latest_time)
            .distinct()
            .all()
        )

        for r in rows:
            if r.symbol:
                symbols.append(str(r.symbol))

    return symbols


# ============================================================
# 単銘柄取得
# ============================================================

def fetch_single(symbol, start_dt, end_dt):

    df = yf.download(
        f"{symbol}.T",
        start=start_dt,
        end=end_dt,
        interval="1m",
        progress=False,
        threads=False,
    )

    if df.empty:
        return None

    df = df.reset_index()
    df["symbol"] = symbol

    return df


# ============================================================
# メイン
# ============================================================

def main():

    symbols = get_latest_ranking_symbols()

    if not symbols:
        print("❌ ranking symbols empty")
        return

    print(f"📊 ranking symbols: {len(symbols)}")

    # ---------------------------------------------
    # 営業日判定
    # ---------------------------------------------
    today = dt.date.today()
    target_day = get_latest_business_day(today)

    start_dt = dt.datetime.combine(target_day, dt.time(9, 0))
    end_dt = dt.datetime.combine(target_day, dt.time(15, 30))

    print(f"📅 target business day: {target_day}")
    print(f"⏱ range: {start_dt} → {end_dt}")

    # ---------------------------------------------
    # 計測開始
    # ---------------------------------------------
    start_time = time.perf_counter()

    results = []

    for sym in symbols:
        df = fetch_single(sym, start_dt, end_dt)
        if df is not None:
            results.append(df)

    elapsed = time.perf_counter() - start_time

    total_rows = sum(len(df) for df in results)

    print("======================================")
    print(f"⏱ elapsed time : {elapsed:.2f} sec")
    print(f"📈 symbols fetched : {len(results)}")
    print(f"📄 total rows : {total_rows}")
    print("======================================")


if __name__ == "__main__":
    main()