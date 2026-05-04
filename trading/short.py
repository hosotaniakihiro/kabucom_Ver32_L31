# trading/signals/short.py
import pandas as pd
import logging
from configparser import ConfigParser
from colorama import Fore, Style, init
from global_state import global_data
from trading.signals.conditions_short import check_entry_conditions
from trading.patterns import detect_bearish_patterns
# colorama 初期化
init(autoreset=True)

logger = logging.getLogger(__name__)

# ===== INIファイルからスコア設定をロード =====
score_config = ConfigParser()
score_config.read("score_config.ini", encoding="utf-8")

sell_scoring = {}
if score_config.has_section("sell_scoring"):
    for key, value in score_config.items("sell_scoring"):
        try:
            sell_scoring[key] = int(value)
        except ValueError:
            sell_scoring[key] = 0


def evaluate_short_signals(symbol, df_summary, ma5, ma25, ma75,
                           macd, signal, rsi, rci9,
                           bb_upper, bb_lower, bb_lower_3,
                           close_price, vwap, slowk, slowd,
                           alert_data):
    """
    売りシグナルを判定してスコアと理由を返す
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

    try:
        # 例: MA5がMA25を下抜け（デッドクロス）
        if pd.notnull(prev.get("ma5")) and pd.notnull(prev.get("ma25")) \
           and pd.notnull(curr.get("ma5")) and pd.notnull(curr.get("ma25")):
            if prev["ma5"] >= prev["ma25"] and curr["ma5"] < curr["ma25"]:
                points = sell_scoring.get("ma5_ma25_cross_down", -2)
                score += points
                reasons.append(f"{Fore.BLUE}MA5がMA25を下抜け（デッドクロス） ({points}){Style.RESET_ALL}")

        # 例: ボリンジャーバンド -2σ ブレイクダウン
        if pd.notnull(curr.get("close_price")) and pd.notnull(curr.get("bb_lower")):
            if curr["close_price"] < curr["bb_lower"]:
                points = sell_scoring.get("bb_lower_breakdown", -2)
                score += points
                reasons.append(f"{Fore.BLUE}ボリンジャーバンド-2σブレイクダウン ({points}){Style.RESET_ALL}")

        # 他の条件もここに追加...

    except Exception as e:
        logger.error(f"[{symbol}] evaluate_short_signals エラー: {e}")

    # --- ローソク足パターン検出 ---
    try:
        patterns = detect_bearish_patterns(recent_data)
        if isinstance(patterns, str):
            patterns = [patterns]
        for p in patterns:
            points = -2
            score += points
            reasons.append(f"🔴 {p} ({points})")
    except Exception as e:
        logger.warning(f"[{symbol}] ローソク足パターン検出エラー: {e}")

    # --- エントリー条件の最終フィルタ ---
    if not check_entry_conditions(recent_data, side="SELL"):
        reasons.append("📉 売り条件未成立 → スコア無効化")
        return 0, reasons

    return score, reasons
