# should_exit_position.py

import pandas as pd
from database.models import Position
from datetime import datetime, timedelta
from utils_market import is_market_open
from utils.alerts_util import send_discord_notify

INACTIVE_THRESHOLD = 0.005
INACTIVE_TIME = timedelta(minutes=15)
def should_exit_position(symbol, pos_side, df_summary, entry_price, highest_price_since_entry=None, lowest_price_since_entry=None):
    """
    ロング/ショート共通のEXIT判定ロジック
    pos_side: "BUY_CREDIT"（信用買い） or "SELL_CREDIT"（信用売り）
    entry_price: 建値
    highest_price_since_entry: エントリー後の最高値（ロング用）
    lowest_price_since_entry: エントリー後の最安値（ショート用）
    """
    import pandas as pd

    reasons = []
    recent = df_summary[df_summary['symbol'] == symbol].sort_values("date")
    if recent.empty:
        return False, ["データがありません"]

    latest = recent.iloc[-1]
    prev = recent.iloc[-2] if len(recent) >= 2 else None

    # === ストップ高/安チェック ===
    from global_state import global_data
    limit_up = global_data.limit_up_map.get(symbol)
    limit_down = global_data.limit_down_map.get(symbol)

    if pos_side == "BUY_CREDIT" and limit_up and latest["close_price"] >= limit_up:
        return True, [f"ストップ高到達（{limit_up}円）"]

    if pos_side == "SELL_CREDIT" and limit_down and latest["close_price"] <= limit_down:
        return True, [f"ストップ安到達（{limit_down}円）"]

    # ===============================
    # 0. 損切り・利確
    # ===============================
    if pos_side == "BUY_CREDIT":
        if entry_price and latest['close_price'] <= entry_price * 0.98:
            return True, ["損切り: -2%以上下落"]
        if entry_price and latest['close_price'] >= entry_price * 1.05:
            return True, ["利確: +5%以上の利益"]

    elif pos_side == "SELL_CREDIT":
        if entry_price and latest['close_price'] >= entry_price * 1.02:
            return True, ["損切り: エントリー価格から+2%以上の上昇"]
        if entry_price and latest['close_price'] <= entry_price * 0.95:
            return True, ["利確: 空売りで+5%以上の利益"]

    # ===============================
    # 1. RSI反落 / 反発
    # ===============================
    if 'rsi' in recent.columns and prev is not None:
        if pos_side == "BUY_CREDIT":
            if prev['rsi'] > 80 and latest['rsi'] < prev['rsi']:
                return True, ["RSI過熱反落"]
        elif pos_side == "SELL_CREDIT":
            if prev['rsi'] < 20 and latest['rsi'] > prev['rsi']:
                return True, ["RSI反発（売られすぎ）"]

    # ===============================
    # 2. ボリンジャーバンド
    # ===============================
    if all(k in latest for k in ['close_price', 'bb_upper', 'bb_lower']) and prev is not None:
        if pos_side == "BUY_CREDIT":
            if prev['close_price'] > prev['bb_upper'] and latest['close_price'] < latest['bb_upper']:
                return True, ["ボリバン+2σ反落"]
        elif pos_side == "SELL_CREDIT":
            if prev['close_price'] < prev['bb_lower'] and latest['close_price'] > latest['bb_lower']:
                return True, ["ボリバン-2σ反発"]

    # ===============================
    # 3. 出来高シグナル
    # ===============================
    if prev is not None and 'volume' in latest and 'open_price' in latest:
        # 出来高ピークアウト + 高値圏陰線（ロング）
        if pos_side == "BUY_CREDIT":
            if latest['close_price'] < latest['open_price']:
                vol_mean = recent['volume'].iloc[-4:-1].mean()
                if latest['volume'] < vol_mean:
                    price_range = recent['high_price'].max() - recent['low_price'].min()
                    if price_range > 0 and latest['close_price'] > recent['high_price'].max() - price_range * 0.2:
                        return True, ["出来高減＋陰線（高値圏）"]

        # 出来高減 + 陽線（安値圏）ショート返済
        if pos_side == "SELL_CREDIT":
            vol_mean = recent['volume'].iloc[-4:-1].mean()
            if latest['close_price'] > latest['open_price'] and latest['volume'] < vol_mean:
                price_range = recent['high_price'].max() - recent['low_price'].min()
                if price_range > 0 and latest['close_price'] < recent['low_price'].min() + price_range * 0.2:
                    return True, ["出来高減＋陽線（安値圏）"]

    # 出来高急減
    if 'volume' in recent.columns and len(recent) >= 4:
        recent_volumes = recent['volume'].iloc[-4:-1]
        if latest['volume'] < recent_volumes.mean() * 0.5:
            return True, ["出来高急減（直近平均の半分以下）"]

    # ===============================
    # 4. ローソク足シグナル
    # ===============================
    # 上ヒゲ陰線（ロングのEXIT）
    upper_wick = latest['high_price'] - max(latest['open_price'], latest['close_price'])
    if pos_side == "BUY_CREDIT" and upper_wick > latest['close_price'] * 0.02 and latest['close_price'] < latest['open_price']:
        return True, ["上ヒゲ陰線（高値圏）"]

    # 下ヒゲ陽線（ショートのEXIT）
    lower_wick = min(latest['open_price'], latest['close_price']) - latest['low_price']
    if pos_side == "SELL_CREDIT" and lower_wick > latest['close_price'] * 0.02 and latest['close_price'] > latest['open_price']:
        return True, ["下ヒゲ陽線（反発兆候）"]

    # 包み足
    if prev is not None:
        if pos_side == "BUY_CREDIT":
            if prev['close_price'] > prev['open_price'] and latest['close_price'] < latest['open_price']:
                if latest['open_price'] > prev['close_price'] and latest['close_price'] < prev['open_price']:
                    return True, ["陰線包み足（リバーサル）"]
        elif pos_side == "SELL_CREDIT":
            if prev['close_price'] < prev['open_price'] and latest['close_price'] > latest['open_price']:
                if latest['open_price'] < prev['close_price'] and latest['close_price'] > prev['open_price']:
                    return True, ["陽線包み足（反転）"]

    # 陰線＋出来高急増（ロングEXIT）
    if pos_side == "BUY_CREDIT" and prev is not None:
        if latest['close_price'] < latest['open_price'] and latest['volume'] > prev['volume'] * 1.5:
            return True, ["陰線＋出来高急増"]

    # 陽線＋出来高急増（ショートEXIT）
    if pos_side == "SELL_CREDIT" and prev is not None:
        if latest['close_price'] > latest['open_price'] and latest['volume'] > prev['volume'] * 1.5:
            return True, ["陽線＋出来高急増（踏み上げ警戒）"]

    # ===============================
    # 5. 移動平均線
    # ===============================
    ma5_trend = recent.sort_values("date").tail(3)['ma5']
    if len(ma5_trend) == 3 and all(pd.notnull(ma5_trend)):
        if pos_side == "BUY_CREDIT":
            if ma5_trend.iloc[0] > ma5_trend.iloc[1] > ma5_trend.iloc[2]:
                return True, ["MA5が3本連続で下降"]
        elif pos_side == "SELL_CREDIT":
            if ma5_trend.iloc[0] < ma5_trend.iloc[1] < ma5_trend.iloc[2]:
                return True, ["MA5が3本連続で上昇"]

    # MA5とMA25
    if 'ma25' in latest and pd.notnull(latest['ma5']) and pd.notnull(latest['ma25']):
        if pos_side == "BUY_CREDIT" and latest['ma5'] < latest['ma25']:
            return True, ["MA5がMA25を下回っている"]
        elif pos_side == "SELL_CREDIT" and latest['ma5'] > latest['ma25']:
            return True, ["MA5がMA25を上抜けている"]

    # MA5割れ / 上抜け
    if 'ma5' in latest and pd.notnull(latest['ma5']) and pd.notnull(latest['close_price']):
        if pos_side == "BUY_CREDIT" and latest['close_price'] < latest['ma5']:
            return True, ["5MA割れ"]
        elif pos_side == "SELL_CREDIT" and latest['close_price'] > latest['ma5']:
            return True, ["5MA上抜け"]

    # ===============================
    # 6. MACD
    # ===============================
    if 'macd' in latest and 'signal' in latest and prev is not None:
        if pd.notnull(latest['macd']) and pd.notnull(latest['signal']):
            if pos_side == "BUY_CREDIT":
                if prev['macd'] >= prev['signal'] and latest['macd'] < latest['signal']:
                    return True, ["MACDデッドクロス"]
            elif pos_side == "SELL_CREDIT":
                if prev['macd'] <= prev['signal'] and latest['macd'] > latest['signal']:
                    return True, ["MACDゴールデンクロス"]

    # ===============================
    # 7. トレーリング反落
    # ===============================
    if pos_side == "BUY_CREDIT" and highest_price_since_entry:
        if latest['close_price'] < highest_price_since_entry * 0.98:
            return True, ["トレーリング反落（最高値-2%）"]

    if pos_side == "SELL_CREDIT" and lowest_price_since_entry:
        if latest['close_price'] > lowest_price_since_entry * 1.02:
            return True, ["トレーリング反発（最安値+2%）"]

    # ===============================
    # 8. VWAP
    # ===============================
    if 'vwap' in latest and pd.notnull(latest['vwap']):
        if pos_side == "BUY_CREDIT" and latest['close_price'] < latest['vwap']:
            return True, ["VWAP割れ"]
        elif pos_side == "SELL_CREDIT" and latest['close_price'] > latest['vwap']:
            return True, ["VWAP上抜け"]

    return False, reasons

def process_exit(pos, df_pos, exit_check_func):
    should_exit, reasons = exit_check_func(
        symbol=pos.symbol,
        pos_side=pos.side,       # BUY_CREDIT / SELL_CREDIT
        df_summary=df_pos,
        entry_price=pos.avg_price,
        highest_price_since_entry=getattr(pos, "highest_price", None),
        lowest_price_since_entry=getattr(pos, "lowest_price", None),
    )

    if not should_exit:
        return

    latest_price = df_pos.iloc[-1]['close_price']

    # ✅ handle_exit に統一
    handle_exit(
        symbol=pos.symbol,
        price=latest_price,
        qty=pos.qty,
        position_id=pos.id,
        symbolname=pos.symbolname,
        reasons=reasons
    )

    # ✅ 通知
    send_discord_notify(
        f"💸 EXIT完了: {pos.symbol} {pos.side} {pos.qty}株 "
        f"@ {latest_price} 理由: {', '.join(reasons)}"
    )

def monitor_and_exit_positions(df_summary):
    """
    ロング/ショート共通のポジション監視 & 自動EXIT
    - df_summary: サマリーデータ (DataFrame, symbolごとにOHLCV+指標含む)
    """
    session = Session_position()
    positions = session.query(Position).filter_by(status="OPEN").all()

    for pos in positions:
        df_pos = df_summary[df_summary["symbol"] == pos.symbol]
        if df_pos.empty:
            continue

        should_exit, reasons = should_exit_position(
            symbol=pos.symbol,
            pos_side=pos.side,                  # BUY_CREDIT or SELL_CREDIT
            df_summary=df_pos,
            entry_price=pos.avg_price,
            highest_price_since_entry=None,     # 最高値は別途管理するならここに渡す
            lowest_price_since_entry=None       # 最安値も同様
        )

        if should_exit:
            latest_price = df_pos.iloc[-1]['close_price']

            # EXIT処理 → Positionクローズ + TradeHistory保存
            handle_exit(
                symbol=pos.symbol,
                price=latest_price,
                qty=pos.qty,
                position_id=pos.id,
                symbolname=pos.symbolname,
                reasons=reasons
            )

            # 通知
            send_discord_notify(
                f"📌 EXIT発動: {pos.symbol} {pos.side} {pos.qty}株 "
                f"@ {latest_price} 理由: {', '.join(reasons)}"
            )

    session.close()

def should_exit_step_trailing_tick(symbol, current_price, pos):
    entry = pos.avg_price
    side = pos.side

    if side == "BUY_CREDIT":
        profit_pct = (current_price - entry) / entry * 100
        if profit_pct < -0.5:
            return True, ["建値-0.5%損切り"], 0, entry * 0.995
        if profit_pct >= 0.5:
            step_level = int((profit_pct - 0.5) // 0.5)
            stop_price = entry * (1 + (0.5 + step_level * 0.5) / 100)
            if current_price < stop_price:
                return True, [f"階段トレーリング {step_level}段目"], step_level, stop_price

    elif side == "SELL_CREDIT":
        profit_pct = (entry - current_price) / entry * 100
        if profit_pct < -0.5:
            return True, ["建値+0.5%損切り"], 0, entry * 1.005
        if profit_pct >= 0.5:
            step_level = int((profit_pct - 0.5) // 0.5)
            stop_price = entry * (1 - (0.5 + step_level * 0.5) / 100)
            if current_price > stop_price:
                return True, [f"階段トレーリング {step_level}段目"], step_level, stop_price

    return False, [], None, None

def should_exit_step_trailing_summary(symbol, latest_row, pos):
    entry = pos.avg_price
    side = pos.side
    close_price = latest_row.get("close_price")
    vwap = latest_row.get("vwap")
    ma5 = latest_row.get("ma5")
    rsi = latest_row.get("rsi")

    reasons = []

    if side == "BUY_CREDIT":
        if close_price <= entry * 0.995:
            reasons.append("建値-0.5%損切り")
        if close_price < entry:
            reasons.append("同値撤退")
        if vwap and close_price < vwap:
            reasons.append("VWAP割れ")
        if ma5 and close_price < ma5:
            reasons.append("MA5割れ")
        if rsi and rsi > 80:
            reasons.append("RSI過熱反落")

    elif side == "SELL_CREDIT":
        if close_price >= entry * 1.005:
            reasons.append("建値+0.5%損切り")
        if close_price > entry:
            reasons.append("同値撤退")
        if vwap and close_price > vwap:
            reasons.append("VWAP上抜け")
        if ma5 and close_price > ma5:
            reasons.append("MA5上抜け")
        if rsi and rsi < 20:
            reasons.append("RSI反発（売り不利）")

    return (len(reasons) > 0), reasons, None, close_price

def should_exit_summary_trailing(symbol: str, latest_price: float, df_summary: pd.DataFrame):
    """
    サマリー用のEXIT判定
    - VWAP割れ
    - MA5割れ
    - RSI過熱反落（80超→下降）
    """
    reasons = []

    # symbolで絞り込み
    recent_data = df_summary[df_summary['symbol'] == symbol].sort_values(by='date')
    if recent_data.empty:
        return False, reasons

    latest = recent_data.iloc[-1]
    prev = recent_data.iloc[-2] if len(recent_data) >= 2 else None

    # 1️⃣ VWAP割れ
    if 'vwap' in latest and pd.notnull(latest['vwap']):
        if latest_price < latest['vwap']:
            reasons.append("VWAP割れ")

    # 2️⃣ MA5割れ
    if 'ma5' in latest and pd.notnull(latest['ma5']):
        if latest_price < latest['ma5']:
            reasons.append("MA5割れ")

    # 3️⃣ RSI過熱反落
    if prev is not None and 'rsi' in latest and pd.notnull(latest['rsi']):
        if prev['rsi'] > 80 and latest['rsi'] < prev['rsi']:
            reasons.append("RSI過熱反落")

    return (len(reasons) > 0), reasons

def check_inactive_positions(current_price_map: dict, token):
    """
    エントリー後15分経過して ±0.5% 以内なら強制EXIT
    current_price_map: {symbol: 現在値} を渡す
    """
    if not is_market_open():
        return

    session = Session_position()
    try:
        positions = session.query(Position).filter_by(status="OPEN").all()
        now = datetime.utcnow()

        for pos in positions:
            if not pos.open_time:
                continue

            elapsed = now - pos.open_time
            if elapsed < INACTIVE_TIME:
                continue  # まだ15分未満

            avg_price = pos.avg_price
            cur_price = current_price_map.get(pos.symbol)
            if not cur_price:
                continue

            # 価格変動率チェック
            lower = avg_price * (1 - INACTIVE_THRESHOLD)
            upper = avg_price * (1 + INACTIVE_THRESHOLD)

            if lower <= cur_price <= upper:
                print(f"⏳ ダラダラ相場検出 → 強制EXIT: {pos.symbol} {cur_price} (15分停滞)")

                # EXIT理由を指定して発注
                handle_exit(
                    symbol=pos.symbol,
                    price=cur_price,
                    qty=pos.qty,
                    position_id=pos.id,
                    symbolname=pos.symbolname,
                    reasons=["15分停滞（±0.5%以内）"]
                )

    finally:
        session.close()
