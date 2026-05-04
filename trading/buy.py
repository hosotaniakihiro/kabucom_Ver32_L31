# trading/signals/buy.py
import pandas as pd
import logging
from configparser import ConfigParser
from colorama import Fore, Style, init
from global_state import global_data
from trading.signals.conditions_buy import check_entry_conditions
from trading.patterns import detect_bullish_patterns
# colorama 初期化
init(autoreset=True)

logger = logging.getLogger(__name__)

# ===== INIファイルからスコア設定をロード =====
score_config = ConfigParser()
score_config.read("score_config.ini", encoding="utf-8")

buy_scoring = {}
if score_config.has_section("buy_scoring"):
    for key, value in score_config.items("buy_scoring"):
        try:
            buy_scoring[key] = int(value)
        except ValueError:
            buy_scoring[key] = 0


def evaluate_buy_signals(symbol, df_summary, ma5, ma25, ma75,
                         macd, signal, rsi, rci,
                         bb_upper, bb_lower, close_price,
                         vwap, slowk, slowd, alert_data):
    """
    買いシグナルを判定してスコアと理由を返す
    """

    score = 0
    reasons = []

    # 初期サマリー時はシグナル発火しない
    if not global_data.initial_summary_done:
        return score, reasons

    # --- シンボル状態の初期化 ---
    if symbol not in alert_data:
        alert_data[symbol] = {}
    if "prev_state" not in alert_data[symbol]:
        alert_data[symbol]["prev_state"] = {}

    prev_state = alert_data[symbol].get("prev_state", {})

    # --- DataFrame 整理 ---
    df_summary = df_summary.copy()
    df_summary["date"] = pd.to_datetime(df_summary["date"], errors="coerce")
    recent_data = df_summary[df_summary["symbol"] == symbol].sort_values(by="date", ascending=False)

    if len(recent_data) < 6:
        alert_data[symbol]["prev_state"] = {}
        return score, reasons

    curr = recent_data.iloc[0]
    prev = recent_data.iloc[1]

    # --- 条件チェック ---
    try:
        # 例: ゴールデンクロス直後の出来高増加
        if pd.notnull(prev.get("ma5")) and pd.notnull(prev.get("ma25")) \
           and pd.notnull(curr.get("ma5")) and pd.notnull(curr.get("ma25")):
            if prev["ma5"] <= prev["ma25"] and curr["ma5"] > curr["ma25"]:
                vol_mean = recent_data["volume"].iloc[1:5].mean()
                if curr["volume"] > vol_mean * 1.2:
                    points = buy_scoring.get("gc_volume_boost", 1)
                    score += points
                    reasons.append(f"{Fore.RED}ゴールデンクロス直後の出来高増加 (+{points}){Style.RESET_ALL}")

        # 他の条件もここに続く...

    except Exception as e:
        logger.error(f"[{symbol}] evaluate_buy_signals エラー: {e}")

    # --- ローソク足パターン検出 ---
    try:
        patterns = detect_bullish_patterns(recent_data)
        if isinstance(patterns, str):
            patterns = [patterns]
        for p in patterns:
            points = 2
            score += points
            reasons.append(f"🟢 {p} (+{points})")
    except Exception as e:
        logger.warning(f"[{symbol}] ローソク足パターン検出エラー: {e}")

    # --- エントリー条件の最終フィルタ ---
    if not check_entry_conditions(recent_data, side="BUY"):
        reasons.append("📉 買い条件未成立 → スコア無効化")
        return 0, reasons

    return score, reasons
