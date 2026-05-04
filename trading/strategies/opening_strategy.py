# trading/strategies/opening_strategy.py

import datetime as dt
import pandas as pd
from kabu_api.buy_sell import execute_buy_at_best_ask, execute_short_at_best_bid
from database.crud import save_trade_history
from global_state import global_data


def run_opening_strategy(df_1m, symbol):
    baseline_vol = global_data.opening_baseline_vol.get(symbol)
    signal, baseline_vol = check_opening_1min_strategy(df_1m, baseline_vol)
    global_data.opening_baseline_vol[symbol] = baseline_vol

    symbolname = global_data.symbol_name_map.get(symbol, "")

    if signal == "BUY_ENTRY":
        print(f"🚀 Opening 信用新規買い: {symbol}")
        try:
            res = execute_buy_at_best_ask(symbol, global_data.unit_size)
            if res and "OrderId" in res:
                save_trade_history(
                    symbol=symbol,
                    symbolname=symbolname,
                    side="BUY_CREDIT",
                    action="ENTRY",
                    qty=global_data.unit_size,
                    price=res.get("Price", 0),   # APIレスポンスに含まれる場合
                    order_id=res["OrderId"],
                    reason="opening_entry"
                )
                sync_positions_from_kabus(global_data.token_value)
        except Exception as e:
            print(f"❌ BUY発注失敗: {symbol} {e}")

    elif signal == "SELL_ENTRY":
        print(f"🚀 Opening 信用新規売り: {symbol}")
        try:
            res = execute_short_at_best_bid(symbol, global_data.unit_size)
            if res and "OrderId" in res:
                save_trade_history(
                    symbol=symbol,
                    symbolname=symbolname,
                    side="SELL_CREDIT",
                    action="ENTRY",
                    qty=global_data.unit_size,
                    price=res.get("Price", 0),
                    order_id=res["OrderId"],
                    reason="opening_entry"
                )
                sync_positions_from_kabus(global_data.token_value)
        except Exception as e:
            print(f"❌ SELL発注失敗: {symbol} {e}")

def check_opening_1min_strategy(df_1m, symbol):
    """
    寄り付き1分足戦略
    - 1分目の出来高を基準に、2分目以降の出来高急増を検知してエントリー
    """
    if df_1m.empty or len(df_1m) < 2:
        return

    baseline_volume = df_1m.iloc[0]["volume"]  # 9:00〜9:01の出来高
    latest = df_1m.iloc[-1]

    if latest["volume"] > baseline_volume * 2:  # 例: 2倍以上の急増
        side = "BUY_CREDIT" if latest["price"] > latest["vwap"] else "SELL_CREDIT"
        qty = 100  # 仮で100株
        if side == "BUY_CREDIT":
            order_id = execute_buy_at_best_ask(symbol, qty)
        else:
            order_id = execute_short_at_best_bid(symbol, qty)

        if order_id:
            save_trade_history(
                symbol=symbol,
                symbolname=global_data.symbol_name_map.get(symbol, ""),
                side=side,
                action="ENTRY",
                qty=qty,
                price=latest["price"],
                order_id=order_id,
                reason="opening_strategy_volume_spike"
            )
            print(f"✅ Opening戦略エントリー: {symbol} {side} {qty}株")