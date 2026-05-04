# trading/summary/evaluate_combined.py
import logging
import configparser
from OLD.evaluate_buy import evaluate_buy_signals
from trading.evaluate_short import evaluate_short_signals

logger = logging.getLogger(__name__)

# === 設定読み込み ===
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
DEFAULT_MODE = conf.get("trade", "mode", fallback="score")  # score / buy / sell
BUY_THRESHOLD = conf.getint("trade", "threshold", fallback=4)
SELL_THRESHOLD = conf.getint("trade", "sell_threshold", fallback=-4)


def evaluate_signals_combined(
    symbol,
    df_summary,
    ma5=None, ma25=None, ma75=None,
    macd=None, signal=None,
    rsi=None, rci=None,
    bb_upper=None, bb_lower=None,
    bb_upper_3=None, bb_lower_3=None,
    close_price=None, vwap=None,
    slowk=None, slowd=None,
    alert_data=None,
    mode=None,
):
    """
    買い・売り両方のシグナルを評価し、最終的な意思決定を返す。

    Parameters
    ----------
    mode : str
        "score" = スコア優先 (デフォルト)
        "buy"   = 買い優先
        "sell"  = 売り優先
    """

    if mode is None:
        mode = DEFAULT_MODE

    # === BUY 判定 ===
    buy_score, buy_reasons = evaluate_buy_signals(
        symbol=symbol,
        df_summary=df_summary,
        ma5=ma5, ma25=ma25, ma75=ma75,
        macd=macd, signal=signal,
        rsi=rsi, rci=rci,
        bb_upper=bb_upper, bb_lower=bb_lower,
        bb_upper_3=bb_upper_3, bb_lower_3=bb_lower_3,
        close_price=close_price, vwap=vwap,
        slowk=slowk, slowd=slowd,
        alert_data=alert_data,
    )

    # === SELL 判定 ===
    sell_score, sell_reasons = evaluate_short_signals(
        symbol=symbol,
        df_summary=df_summary,
        ma5=ma5, ma25=ma25, ma75=ma75,
        macd=macd, signal=signal,
        rsi=rsi, rci=rci,
        bb_upper=bb_upper, bb_lower=bb_lower,
        bb_upper_3=bb_upper_3, bb_lower_3=bb_lower_3,
        close_price=close_price, vwap=vwap,
        slowk=slowk, slowd=slowd,
        alert_data=alert_data,
    )

    # === 判定ロジック ===
    action, score, reasons = "SKIP", 0, []

    if buy_score >= BUY_THRESHOLD and sell_score <= SELL_THRESHOLD:
        if mode == "buy":
            action, score, reasons = "BUY", buy_score, buy_reasons
        elif mode == "sell":
            action, score, reasons = "SELL", sell_score, sell_reasons
        else:  # score優先
            if buy_score >= abs(sell_score):
                action, score, reasons = "BUY", buy_score, buy_reasons
            else:
                action, score, reasons = "SELL", sell_score, sell_reasons
    elif buy_score >= BUY_THRESHOLD:
        action, score, reasons = "BUY", buy_score, buy_reasons
    elif sell_score <= SELL_THRESHOLD:
        action, score, reasons = "SELL", sell_score, sell_reasons
    else:
        # どちらも閾値未達
        if abs(buy_score) >= abs(sell_score):
            score, reasons = buy_score, buy_reasons
        else:
            score, reasons = sell_score, sell_reasons
        action = "SKIP"

    logger.info(
        f"🔎 {symbol} 判定結果: {action} "
        f"(BUY={buy_score}, SELL={sell_score}, mode={mode})"
    )

    return {"action": action, "score": score, "reasons": reasons}
