# ============================================================
# scalp_debug_printer.py（Ver24-SCALP-PRO）
# ------------------------------------------------------------
# スキャ点火・EXIT 監視用のデバッグビュー
# ============================================================

import datetime as dt
from global_state import global_data


def _fmt(v):
    return f"{v:.3f}" if isinstance(v, float) else v


def print_scalp_debug(symbol):
    """
    指定銘柄についてスキャ情報を可視化する
    """

    symbol = str(symbol)

    # ---------------------------------------------------------
    # 最新5秒足
    # ---------------------------------------------------------
    bar_5s = global_data.latest_5s_bar.get(symbol)
    if not bar_5s:
        print(f"[{symbol}] 5秒足なし")
        return

    # ---------------------------------------------------------
    # orderflow
    # ---------------------------------------------------------
    of = global_data.orderflow.get(symbol, {})

    buy_cnt = of.get("buy_count_3s", 0)
    sell_cnt = of.get("sell_count_3s", 0)
    bid_size = of.get("best_bid_size", 0)
    ask_size = of.get("best_ask_size", 0)
    ask_thin = of.get("ask_thin_count", 0)

    # ---------------------------------------------------------
    # VWAP & 乖離
    # ---------------------------------------------------------
    summary_1m = global_data.global_dataframe_summary_1min
    gap = None
    vwap = None
    if summary_1m is not None:
        df1 = summary_1m[summary_1m["symbol"] == symbol]
        if not df1.empty:
            vwap = df1.iloc[-1].get("vwap")
            price = bar_5s["close"]
            if vwap:
                gap = (price - vwap) / vwap

    # ---------------------------------------------------------
    # ★ デバッグビュー出力
    # ---------------------------------------------------------
    print("===================================================")
    print(f"  🔍 SCALP DEBUG VIEW  {symbol}")
    print("===================================================")

    print("【5秒足】")
    print(f"  open={bar_5s['open']}  high={bar_5s['high']}  "
          f"low={bar_5s['low']}  close={bar_5s['close']}  vol={bar_5s['volume']}")

    print("\n【VWAP】")
    if vwap:
        print(f"  vwap={_fmt(vwap)}  乖離={_fmt(gap)}")
    else:
        print("  vwap=---")

    print("\n【ORDERFLOW】")
    print(f"  成行買い連続={buy_cnt}   成行売り連続={sell_cnt}")
    print(f"  bid_size={bid_size}   ask_size={ask_size}")
    print(f"  売り板撤退={ask_thin}")

    print("\n【勢い判定】")
    if bar_5s["close"] > bar_5s["open"]:
        print("  🔵 5秒陽線（BUY勢い）")
    elif bar_5s["close"] < bar_5s["open"]:
        print("  🔴 5秒陰線（SELL勢い）")
    else:
        print("  5秒足：寄せ")

    print("===================================================")
