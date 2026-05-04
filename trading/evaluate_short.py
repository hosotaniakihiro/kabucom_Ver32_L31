import pandas as pd
import logging
from configparser import ConfigParser
from colorama import init

from global_state import global_data
from trading.signals.conditions_short import conditions_short
from trading.signals.runner import run_condition
# entry_signal_collector 廃止互換
def check_entry_conditions_short(*args, **kwargs):
    return False
# === 初期化 ===
init(autoreset=True)
logger = logging.getLogger(__name__)

# === スコア設定の読み込み ===
score_config = ConfigParser()
score_config.read("score_config.ini", encoding="utf-8")

short_scoring: dict[str, float] = {}
if score_config.has_section("short_scoring"):
    for key, value in score_config.items("short_scoring"):
        try:
            short_scoring[key] = float(value)
        except ValueError:
            short_scoring[key] = 0.0


def evaluate_short_signals(
    symbol: str,
    df_summary: pd.DataFrame,
    alert_data: dict | None = None,
) -> tuple[float, list[str], bool]:
    """
    SELLシグナルを評価し、スコア・理由・有効フラグを返す

    Parameters
    ----------
    symbol : str
        銘柄コード
    df_summary : pd.DataFrame
        サマリーデータ（複数バー）
    alert_data : dict, optional
        シグナル状態を保持する辞書

    Returns
    -------
    tuple
        (score: float, reasons: list[str], is_valid: bool)
    """
    score, reasons, valid = 0.0, [], False

    # === 初期サマリー未完了なら無効 ===
    if not global_data.initial_summary_done:
        return 0.0, ["📉 初期サマリー未完了 → 評価スキップ"], False

    # === alert_data 初期化 ===
    if alert_data is None:
        alert_data = global_data.alert_data
    if symbol not in alert_data:
        alert_data[symbol] = {}
    prev_state = alert_data[symbol].get("prev_state", {})

    # === DataFrame整形 ===
    df_summary = df_summary.copy()
    if "date" in df_summary.columns:
        df_summary["date"] = pd.to_datetime(df_summary["date"], errors="coerce")

    # 直近20本（昇順にして最後が最新）
    recent_data = (
        df_summary[df_summary["symbol"] == symbol]
        .sort_values(by="date", ascending=True)
        .tail(20)
    )

    if recent_data.empty:
        alert_data[symbol]["prev_state"] = prev_state
        return 0.0, ["📉 データなし → 評価不可"], False

    if len(recent_data) < 6:
        reasons.append(f"⚠️ データ不足 ({len(recent_data)}本) → 精度低下の可能性あり")

    curr = recent_data.iloc[-1].to_dict()
    prev = recent_data.iloc[-2].to_dict() if len(recent_data) > 1 else {}

    # === 各条件の評価 ===
    for cond in conditions_short:
        pts, reason, flag = run_condition(
            cond, curr, prev, recent_data, short_scoring, prev_state, symbol, "SELL"
        )
        logger.debug(
            f"[SELL] {symbol} cond={cond.__name__}, pts={pts}, reason={reason}, flag={flag}"
        )

        if pts != 0:
            score += pts
            if reason:
                reasons.append(reason)

    # === エントリー条件判定 ===
    if not check_entry_conditions_short(recent_data, side="SELL"):
        reasons.append("📉 売り条件未成立")
        valid = False
    else:
        valid = score > 0

    # === 理由が空なら補足 ===
    if not reasons:
        reasons.append("📉 売り条件未成立")

    logger.debug(
        f"[SELL] {symbol} FINAL SCORE={score}, REASONS={reasons}, VALID={valid}"
    )

    return score, reasons, valid
