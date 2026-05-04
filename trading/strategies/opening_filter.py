import datetime as dt
import pandas as pd
from global_state import global_data
from database.crud import get_latest_ranking

def filter_opening_candidates(df_push: pd.DataFrame, ranking_limit: int = 20):
    """
    寄り付き15分戦略の候補銘柄をスクリーニング
    - 9:00〜9:01 出来高を基準にする
    - 9:05〜9:10 の出来高が基準の2倍以上なら候補
    - ランキング上位（値上がり率/値下がり率）の銘柄に絞り込み
    """
    if df_push is None or df_push.empty:
        return []

    now = dt.datetime.now().strftime("%H:%M")
    if not ("09:00" <= now <= "09:15"):
        return []

    results = []

    # === symbolごとに1分足へリサンプリング ===
    df_1m = (
        df_push
        .groupby("symbol")
        .resample("1min", on="time")
        .agg({
            "price": "last",
            "volume": "sum",
            "vwap": "last"
        })
        .dropna()
        .reset_index()  # groupby+resampleするとMultiIndexになるので整形
    )

    for symbol in global_data.symbols:
        df_symbol = df_1m[df_1m["symbol"] == symbol]
        if df_symbol.empty or len(df_symbol) < 2:
            continue

        # 基準出来高（9:00〜9:01）
        baseline_vol = df_symbol.iloc[0]["volume"]

        # 最新の1分足
        latest = df_symbol.iloc[-1]

        if baseline_vol > 0 and latest["volume"] >= baseline_vol * 2:
            results.append({
                "symbol": symbol,
                "symbolname": global_data.symbol_name_map.get(symbol, ""),
                "price": latest["price"],
                "volume": latest["volume"],
                "vwap": latest["vwap"],
                "baseline_vol": baseline_vol
            })

    # === ランキングと突合 ===
    ranking_df = get_latest_ranking(limit=ranking_limit)
    if ranking_df is not None and not ranking_df.empty:
        ranking_symbols = set(ranking_df["symbol"].astype(str))
        results = [r for r in results if r["symbol"] in ranking_symbols]

    return results
