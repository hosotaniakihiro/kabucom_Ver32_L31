#trading/utils_trade.py

import configparser
import logging
from global_state import global_data

logger = logging.getLogger(__name__)

# === 設定読み込み ===
conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
TURNOVER_THRESHOLD = conf.getint("trade", "turnover_threshold", fallback=3_000_000)


def has_min_trading_value(symbol: str) -> bool:
    """
    サマリー(5分足)を参照して、直近の売買代金が閾値以上かを判定
    """
    df_summary_5min = global_data.global_dataframe_summary  # 5分足サマリー

    if df_summary_5min is None or df_summary_5min.empty:
        return False

    df_symbol = df_summary_5min[df_summary_5min["symbol"] == symbol]
    if df_symbol.empty:
        return False

    trading_value_5min = df_symbol.iloc[-1].get("trading_value", 0) or 0
    if trading_value_5min < TURNOVER_THRESHOLD:
        logger.info(
            f"⏭️ {symbol}: 売買代金 {trading_value_5min:,.0f} < {TURNOVER_THRESHOLD:,} → スキップ"
        )
        return False

    return True
