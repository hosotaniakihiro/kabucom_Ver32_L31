#should_exit_short_position.py

import pandas as pd
from datetime import datetime
from database import Session_summary
from database.models import Position


from kabu_api import execute_buy_order, is_order_filled
from OLD.discord_notifier import send_discord_notify


def should_exit_short_position(symbol, df_summary, entry_price, lowest_price_since_entry):
    reasons = []
    recent_data = df_summary[df_summary['symbol'] == symbol].sort_values(by='date', ascending=True)

    if recent_data.empty:
        return False, ["データがありません"]

    latest = recent_data.iloc[-1]
    prev = recent_data.iloc[-2] if len(recent_data) >= 2 else None

    # 0. 損切り: エントリー価格より2%以上の上昇
    if entry_price and latest['close_price'] >= entry_price * 1.02:
        reasons.append("損切り: エントリー価格から2%以上の上昇")
        return True, reasons

    # 1. 利確: 5%以上の利益
    if entry_price and latest['close_price'] <= entry_price * 0.95:
        reasons.append("利確: 空売りで+5%以上の利益")
        return True, reasons

    # 2. RSI反発（20以下から上昇）
    if 'rsi' in recent_data.columns and prev is not None:
        if prev['rsi'] < 20 and latest['rsi'] > prev['rsi']:
            reasons.append("RSI反発（売られすぎ）")
            return True, reasons

    # 3. ボリンジャーバンド -2σ反発
    if all(k in latest for k in ['close_price', 'bb_lower']) and prev is not None:
        if prev['close_price'] < prev['bb_lower'] and latest['close_price'] > latest['bb_lower']:
            reasons.append("ボリンジャーバンド反発（-2σ）")
            return True, reasons

    # 4. 出来高減 + 陽線（安値圏）
    if prev is not None:
        vol_mean = recent_data['volume'].iloc[-4:-1].mean()
        if latest['close_price'] > latest['open_price'] and latest['volume'] < vol_mean:
            price_range = recent_data['high_price'].max() - recent_data['low_price'].min()
            if price_range > 0:
                is_near_low = latest['close_price'] < recent_data['low_price'].min() + price_range * 0.2
                if is_near_low:
                    reasons.append("出来高減＋陽線（安値圏）")
                    return True, reasons

    # 5. 下ヒゲ陽線（リバーサル）
    lower_wick = min(latest['open_price'], latest['close_price']) - latest['low_price']
    if lower_wick > latest['close_price'] * 0.02 and latest['close_price'] > latest['open_price']:
        reasons.append("下ヒゲ陽線（反発兆候）")
        return True, reasons

    # 6. MA5上抜け（トレンド反転）
    if 'ma5' in latest and pd.notnull(latest['ma5']) and pd.notnull(latest['close_price']):
        if latest['close_price'] > latest['ma5']:
            if prev is not None and pd.notnull(prev['close_price']) and pd.notnull(prev['ma5']):
                if prev['close_price'] < prev['ma5']:
                    reasons.append("MA5上抜け（クロス）")
                    return True, reasons
            reasons.append("MA5上抜け")
            return True, reasons

    # 7. MA5がMA25を上抜け
    if 'ma25' in latest and pd.notnull(latest['ma5']) and pd.notnull(latest['ma25']):
        if latest['ma5'] > latest['ma25']:
            reasons.append("MA5がMA25を上抜け（GC）")
            return True, reasons

    # 8. VWAP上抜け
    if 'vwap' in latest and pd.notnull(latest['vwap']):
        if latest['close_price'] > latest['vwap']:
            reasons.append("VWAP上抜け")
            return True, reasons

    # 9. 陽線包み足（リバーサル）
    if prev is not None:
        if prev['close_price'] < prev['open_price'] and latest['close_price'] > latest['open_price']:
            if latest['open_price'] < prev['close_price'] and latest['close_price'] > prev['open_price']:
                reasons.append("陽線包み足（反転）")
                return True, reasons

    # 10. 陽線＋出来高急増
    if prev is not None and 'volume' in latest and 'volume' in prev:
        if latest['close_price'] > latest['open_price'] and latest['volume'] > prev['volume'] * 1.5:
            reasons.append("陽線＋出来高急増（踏み上げ警戒）")
            return True, reasons

    # ✅ 11. MACD ゴールデンクロス（上抜け）
    if 'macd' in latest and 'macdsignal' in latest and prev is not None:
        if pd.notnull(latest['macd']) and pd.notnull(latest['macdsignal']) and pd.notnull(prev['macd']) and pd.notnull(prev['macdsignal']):
            if prev['macd'] <= prev['macdsignal'] and latest['macd'] > latest['macdsignal']:
                reasons.append("MACDゴールデンクロス（トレンド反転）")
                return True, reasons

    return False, reasons


def monitor_and_exit_short_positions(df_summary):
    """空売りポジションの返済監視と処理"""
    session = SummarySession()
    positions = session.query(Position).filter_by(status='OPEN', side='SHORT').all()

    for pos in positions:
        df_pos = df_summary[df_summary['symbol'] == pos.symbol]
        if df_pos.empty:
            continue

        should_exit, reasons = should_exit_short_position(
            symbol=pos.symbol,
            df_summary=df_pos,
            entry_price=pos.entry_price,
            lowest_price_since_entry=pos.lowest_price
        )

        if should_exit:
            order_id = execute_buy_order(symbol=pos.symbol, qty=pos.shares, hold_id=pos.hold_id)
            if order_id and is_order_filled(order_id):
                pos.exit_price = df_pos.iloc[-1]['close_price']
                pos.exit_time = datetime.now()
                pos.status = 'CLOSED'
                pos.pnl = (pos.entry_price - pos.exit_price) * pos.shares
                send_discord_notify(f"📈 空売り返済完了: {pos.symbol} 理由: {', '.join(reasons)}")
            else:
                send_discord_notify(f"⚠️ 空売り返済注文失敗または未約定: {pos.symbol} 理由: {', '.join(reasons)}")

    session.commit()
    session.close()
def should_exit_on_tick(symbol, tick_price, pos_side, df_summary):
    """
    ティックデータ用の返済判定:
      - BUY_CREDIT: 現在値がMA5を下回ったら返済売り
      - SELL_CREDIT: 現在値がMA5を上回ったら返済買い
    """
    if df_summary.empty:
        return False, ["サマリーデータなし"]

    recent = df_summary[df_summary["symbol"] == symbol].sort_values("date")
    if recent.empty or "ma5" not in recent.columns:
        return False, ["5MA未計算"]

    latest_ma5 = recent.iloc[-1]["ma5"]
    if pd.isnull(latest_ma5):
        return False, ["MA5が未計算"]

    reasons = []
    if pos_side == "BUY_CREDIT" and tick_price < latest_ma5:
        reasons.append(f"5MA割れ (現在値={tick_price}, MA5={latest_ma5})")
        return True, reasons
    elif pos_side == "SELL_CREDIT" and tick_price > latest_ma5:
        reasons.append(f"5MA上抜け (現在値={tick_price}, MA5={latest_ma5})")
        return True, reasons

    return False, []
