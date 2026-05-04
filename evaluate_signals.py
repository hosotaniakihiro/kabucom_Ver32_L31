# evaluate_signals.py
import pandas as pd
from configparser import ConfigParser
from colorama import init

# 既存の評価関数をインポート

from trading.evaluate_short import evaluate_short_signals

# colorama 初期化（ログ出力用カラー）
init(autoreset=True)

# 設定ファイルから mode を読み込む
config = ConfigParser()
config.read("settings.ini", encoding="utf-8")
TRADE_MODE = config.get("trade", "mode", fallback="buy").lower()  # "buy" or "short"


def evaluate_signals(
        symbol,
        df_summary,
        ma5, ma25, ma75,
        macd, signal,
        rsi, rci,
        bb_upper, bb_lower,
        bb_upper_3, bb_lower_3,
        close_price, vwap,
        slowk, slowd,
        alert_data,
        trade_mode="buy",
):
    """
    銘柄ごとのシグナル評価を統一的に実行する。

    Parameters
    ----------
    symbol : str
        銘柄コード
    df_summary : pd.DataFrame
        サマリーデータ
    ma5, ma25, ma75 : float
        移動平均
    macd, signal : float
        MACD とシグナル
    rsi, rci : float
        RSI, RCI
    bb_upper, bb_lower : float
        ボリンジャーバンド ±2σ
    bb_upper_3, bb_lower_3 : float
        ボリンジャーバンド ±3σ
    close_price, vwap : float
        終値と VWAP
    slowk, slowd : float
        ストキャスティクス
    alert_data : dict
        アラートデータ
    trade_mode : str
        "buy" → evaluate_buy_signals
        "short" → evaluate_short_signals
    """
    if trade_mode == "short":
        score, reasons = evaluate_short_signals(
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
    else:
        score, reasons = evaluate_buy_signals(
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

    return score, reasons
