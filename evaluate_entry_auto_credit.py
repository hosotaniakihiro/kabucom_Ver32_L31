# evaluate_entry_auto_credit.py
import logging
import configparser
from utils_common import format_float, calculate_shares
from kabu_api import execute_buy_credit, execute_sell_credit  # 信用取引用

from OLD.evaluate_buy import evaluate_buy_signals
from trading.evaluate_short import evaluate_short_signals

logger = logging.getLogger(__name__)

# ===== 設定ファイル読み込み =====
conf = configparser.ConfigParser()
conf.read("score_config.ini", encoding="utf-8")

BUY_THRESHOLD = conf.getint("trade", "threshold", fallback=4)
SELL_THRESHOLD = abs(conf.getint("trade", "sell_threshold", fallback=.-4))
ENTRY_BUDGET = conf.getint("trade", "entry_budget", fallback=500_000)  # 1銘柄50万円
DEFAULT_MODE = "auto"

# ===== 信用取引専用評価 & 発注 =====
def evaluate_entry_credit(
    symbol,
    df_summary,
    ma5, ma25, ma75,
    macd, signal,
    rsi, rci,
    bb_upper, bb_lower,
    close_price, vwap,
    slowk, slowd,
    alert_data=None,
    mode=None,
):
    """
    🔹 信用取引専用
    🔹 BUY/SELLスコアを計算し、理由を表示
    🔹 閾値を超えたら自動発注（発注前確認なし）
    """

    mode = mode or DEFAULT_MODE

    # ===== BUY判定 =====
    buy_score, buy_reasons = evaluate_buy_signals(
        symbol=symbol,
        df_summary=df_summary,
        ma5=ma5, ma25=ma25, ma75=ma75,
        macd=macd, signal=signal,
        rsi=rsi, rci=rci,
        bb_upper=bb_upper, bb_lower=bb_lower,
        close_price=close_price, vwap=vwap,
        slowk=slowk, slowd=slowd,
        alert_data=alert_data,
    )

    # ===== SELL判定 =====
    sell_score, sell_reasons = evaluate_short_signals(
        symbol=symbol,
        df_summary=df_summary,
        ma5=ma5, ma25=ma25, ma75=ma75,
        macd=macd, signal=signal,
        rsi=rsi, rci9=rci,
        bb_upper=bb_upper, bb_lower=bb_lower,
        bb_lower_3=None,
        close_price=close_price,
        vwap=vwap,
        slowk=slowk, slowd=slowd,
        alert_data=alert_data,
    )

    # ===== 表示整形 =====
    print("-" * 60)
    print(f"🔹 {symbol} | 価格: {format_float(close_price)}")
    print(f"  BUY  | スコア: {buy_score} | 理由: {', '.join(buy_reasons) if buy_reasons else 'なし'}")
    print(f"  SELL | スコア: {sell_score} | 理由: {', '.join(sell_reasons) if sell_reasons else 'なし'}")

    # ===== 判定ロジック =====
    action, score, reasons = "SKIP", 0, []

    if buy_score >= BUY_THRESHOLD and buy_score > sell_score:
        action, score, reasons = "BUY", buy_score, buy_reasons
    elif sell_score >= SELL_THRESHOLD and sell_score > buy_score:
        action, score, reasons = "SELL", sell_score, sell_reasons
    else:
        action, score, reasons = "SKIP", max(buy_score, sell_score), []

    logger.info(f"🔎 {symbol} 判定結果: {action} (BUY={buy_score}, SELL={sell_score}, mode={mode})")

    # ===== 自動発注 =====
    if mode == "auto" and action in ["BUY", "SELL"]:
        try:
            shares = calculate_shares(close_price)
            if action == "BUY":
                order_id = execute_buy_credit(symbol, shares, close_price)
                logger.info(f"[{symbol}] 信用買い注文完了: 注文ID {order_id}, 株数 {shares}, 約定価格 {close_price}")
            else:  # SELL
                order_id = execute_sell_credit(symbol, shares, close_price)
                logger.info(f"[{symbol}] 信用返済売り注文完了: 注文ID {order_id}, 株数 {shares}, 約定価格 {close_price}")
        except Exception as e:
            logger.error(f"[{symbol}] 自動注文エラー: {e}")

    return {"action": action, "score": score, "reasons": reasons}
