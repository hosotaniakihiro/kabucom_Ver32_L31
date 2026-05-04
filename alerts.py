# alerts.py
import configparser
from datetime import datetime
from sqlalchemy.orm import sessionmaker

# 通知
from utils.alerts_util import send_discord_notify
# 設定
from settings import ENTRY_BUDGET, UNIT_SIZE, THRESHOLD
# DB
from database.models import Position
# 単元計算
from utils_common import calculate_shares
# スコア評価
from evaluate_signals import evaluate_signals   # ✅ 統合された関数を利用
# エントリー/イグジット
from trade_manager import handle_entry, alert_data as tm_alert_data  # 既存の辞書を流用

threshold = THRESHOLD

# === 追加: 設定から VWAP 無効フラグ ===
_conf = configparser.ConfigParser()
_conf.read('settings.ini', encoding='utf-8')
DISABLE_VWAP = _conf.getboolean('Alerts', 'disable_vwap', fallback=True)
MODE = _conf.get("trade", "mode", fallback="score")  # score / buy / sell


def is_already_entered(symbol):
    """DBで未決済ポジションがあるか確認する"""
    Session = sessionmaker(bind=summary_engine)
    session = Session()
    try:
        exists = session.query(Position).filter_by(symbol=symbol, status='OPEN').first()
        return exists is not None
    finally:
        session.close()

def get_webhook_url():
    config = configparser.ConfigParser()
    config.read('settings.ini', encoding='utf-8')
    return config.get('Discord', 'webhook_url', fallback=None)

# ✅ 互換性のため alert_data は任意引数（未指定なら trade_manager 側の辞書を使う）
def check_alerts(token, symbol, symbol_name, close_price, ma5, ma25, macd, signal,
                 df_with_indicators, alert_data=None):
    alert_data = alert_data if alert_data is not None else tm_alert_data
    try:
        df_sym = df_with_indicators[df_with_indicators['symbol'] == symbol].copy()
        if df_sym.empty:
            return

        latest = df_sym.iloc[-1]
        ma75 = latest.get('ma75')
        vwap = latest.get('vwap') if not DISABLE_VWAP else None
        rsi = latest.get('rsi')
        rci = latest.get('rci9')
        bb_upper = latest.get('bb_upper')
        bb_lower = latest.get('bb_lower')
        slowk = latest.get('slowk')
        slowd = latest.get('slowd')

        score, reasons = evaluate_signals(
            symbol, df_sym, ma5, ma25, ma75,
            macd, signal, rsi, rci,
            bb_upper, bb_lower, close_price,
            vwap, slowk, slowd, alert_data
        )

        if score >= THRESHOLD:
            reason_text = " | ".join(reasons)
            send_discord_notify(
                f"🚨 {symbol_name}({symbol}) 買いアラート！スコア: {score}\n理由: {reason_text}",
                get_webhook_url()
            )
        elif score <= SELL_THRESHOLD:
            reason_text = " | ".join(reasons)
            send_discord_notify(
                f"🚨 {symbol_name}({symbol}) 売りアラート！スコア: {score}\n理由: {reason_text}",
                get_webhook_url()
            )

    except Exception as e:
        print(f"❌ アラート処理エラー: {symbol} - {e}")



def handle_entry(token, symbol, symbol_name, price, alert_key, msg):
    """ポジションをエントリーする際の処理"""
    global alert_data

    if is_already_entered(symbol):
        print(f"⏩ {symbol_name} ({symbol}) はすでにポジション保有中（handle_entry 内）")
        return

    qty = calculate_shares(price=price, budget=ENTRY_BUDGET, unit_size=UNIT_SIZE)
    if qty == 0:
        print(f"⚠️ {symbol_name} ({symbol}) 株価が高くて {price}円では購入不可（最低単元未満）")
        return

    order_result = execute_buy_order(token, symbol, price, qty)
    if order_result and 'order_id' in order_result:
        order_id = order_result['order_id']
        print(f"[ENTRY] {symbol_name} ({symbol}) にエントリー: {alert_key} - {msg} (注文ID: {order_id})")

        if symbol not in alert_data:
            alert_data[symbol] = {}

        alert_data[symbol]['entry_price'] = price
        alert_data[symbol]['entry_qty'] = qty
        alert_data[symbol]['entry_time'] = datetime.now()
        alert_data[symbol][alert_key] = msg
        send_discord_notify(msg, get_webhook_url())
    else:
        print(f"❌ エントリー注文失敗: {symbol_name} ({symbol})")


def evaluate_entry_score_and_signal(df, threshold=3):
    """
    バックテストなどで使用される、DataFrame全体を評価する関数。
    """
    df = df.copy()
    df['buy_signal'] = 0
    signals = []

    for symbol in df['symbol'].unique():
        df_symbol = df[df['symbol'] == symbol].sort_values('date')

        if len(df_symbol) < 2:
            continue

        if 'score' not in df_symbol.columns:
            continue

        prev_score = df_symbol.iloc[-2].get('score', 0)
        curr_score = df_symbol.iloc[-1].get('score', 0)

        if prev_score < threshold and curr_score >= threshold:
            idx = df_symbol.index[-1]
            df.loc[idx, 'buy_signal'] = 1
            signals.append((symbol, curr_score))

    results = {symbol: score for symbol, score in signals}
    return df, results
def process_alert(latest, buy_score, buy_reasons, sell_score, sell_reasons, token):
    """
    アラートを評価し、信用取引エントリーまたは返済を実行する
    - mode=score → スコア絶対値が大きい方を優先
    - mode=buy   → 買い優先
    - mode=sell  → 売り優先
    """
    symbol = latest.get("symbol")
    symbolname = latest.get("symbolname", "不明")
    price = latest.get("close_price")
    qty = calculate_shares(price=price, budget=ENTRY_BUDGET, unit_size=UNIT_SIZE)

    print(f"\n⚡ アラート検出: {symbolname}({symbol})")
    print(f"BUYスコア={buy_score}, SELLスコア={sell_score}, 価格={price}, 数量={qty}")

    # ===============================
    # ✅ 優先モード別の判定
    # ===============================
    action = None
    reasons = []

    if MODE == "buy":
        if buy_score >= THRESHOLD:
            action = "BUY_CREDIT"
            reasons = buy_reasons

    elif MODE == "sell":
        if sell_score <= SELL_THRESHOLD:
            action = "SELL_CREDIT"
            reasons = sell_reasons

    else:  # MODE == "score"
        if abs(buy_score) >= abs(sell_score):
            if buy_score >= THRESHOLD:
                action = "BUY_CREDIT"
                reasons = buy_reasons
        else:
            if sell_score <= SELL_THRESHOLD:
                action = "SELL_CREDIT"
                reasons = sell_reasons

    # ===============================
    # ✅ 実行 or スキップ
    # ===============================
    if action == "BUY_CREDIT":
        msg = f"信用買い建て: {symbolname}（{symbol}） スコア={buy_score}"
        handle_entry(token, symbol, symbolname, price, action, msg)
        print(f"✅ 買いエントリー実行: {msg} 理由={reasons}")

    elif action == "SELL_CREDIT":
        msg = f"信用売り建て: {symbolname}（{symbol}） スコア={sell_score}"
        handle_entry(token, symbol, symbolname, price, action, msg)
        print(f"✅ 売りエントリー実行: {msg} 理由={reasons}")

    else:
        print(f"⏩ {symbolname}({symbol}) スコア未達: BUY={buy_score}, SELL={sell_score}")

# ============================================================
# TONOSAMA ALERT
# ============================================================

# ============================================================
# TONOSAMA ALERT
# ============================================================

def notify_tonosama(symbol=None, message=None, **kwargs):
    try:
        text = f"[TONOSAMA] {symbol} {message}"
        print(text)
    except Exception:
        pass


# ============================================================
# IGNITION ALERT
# ============================================================

def notify_ignition(symbol=None, message=None, **kwargs):
    try:
        text = f"[IGNITION] {symbol} {message}"
        print(text)
    except Exception:
        pass


# ============================================================
# BREAKOUT ALERT
# ============================================================

def notify_breakout(symbol=None, message=None, **kwargs):
    try:
        text = f"[BREAKOUT] {symbol} {message}"
        print(text)
    except Exception:
        pass