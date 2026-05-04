# ============================================================
# summary_5s_builder.py（5秒足生成スレッド・完全修正版）
# ------------------------------------------------------------
# ✔ 全銘柄を対象に5秒足を生成
# ✔ volume は tick差分で正確に集計
# ✔ 5秒足は global_data.latest_5s_bar[symbol] に保存
# ✔ exit_scalp_handler は全銘柄で判定を実行
# ============================================================

import time
import pandas as pd
from global_state import global_data
from exit_handler_scalp import exit_scalp_handler


def start_5s_builder():
    """
    全銘柄に対し push_df から 5秒足を生成し、
    EXIT判定（exit_scalp_handler）を実行するスレッド。
    """
    print("🚀 5秒足生成スレッド開始")

    last_ts = None

    while True:

        df = global_data.get_push_df()
        if df is None or df.empty:
            time.sleep(0.1)
            continue

        # 最新tickの時刻
        ts = df["datetime"].iloc[-1]

        if last_ts is None:
            last_ts = ts

        # ===== 5秒経過判定 =====
        if (ts - last_ts).total_seconds() >= 5:

            # ■ push_df を銘柄ごとに処理
            for symbol, d in df.groupby("symbol"):

                # 5秒区間のデータ抽出
                d5 = d[(d["datetime"] >= last_ts) & (d["datetime"] <= ts)]
                if d5.empty:
                    continue

                # volume は tick差分の合計
                # TradingVolume は累計なので diff() で1tickごとに変化量を取得
                vol = (
                    d5["volume"]
                    .diff()
                    .clip(lower=0)
                    .fillna(0)
                    .sum()
                )

                bar_5s = {
                    "open":  d5["price"].iloc[0],
                    "high":  d5["price"].max(),
                    "low":   d5["price"].min(),
                    "close": d5["price"].iloc[-1],
                    "volume": vol,
                    "start_ts": last_ts,
                    "end_ts": ts,
                }

                # --------------------------------------------------
                # ★ 最新の 5秒足を global_data に保存（超重要）
                # --------------------------------------------------
                global_data.latest_5s_bar[symbol] = bar_5s

                # --------------------------------------------------
                # EXIT判定を実行
                # --------------------------------------------------
                try:
                    summary_1m = global_data.global_dataframe_summary_1min
                    exit_scalp_handler(
                        symbol,
                        bar_5s["close"],
                        summary_1m,
                        bar_5s
                    )
                except Exception as e:
                    print(f"exit_scalp_handler error ({symbol}): {e}")

            # 次の区間へ
            last_ts = ts

        time.sleep(0.05)
