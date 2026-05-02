# ============================================================
# File: ops/self_diagnosis.py
# ------------------------------------------------------------
# 殿様イナゴ 自己診断モジュール
#
# ✔ 日次・週次の状態チェック専用
# ✔ トレードロジックには一切介入しない
# ✔ 「壊れ始め」を数値で検知する
# ✔ ログ／アラート用途のみ
# ============================================================

from __future__ import annotations

from typing import Dict, List


# ============================================================
# 固定しきい値（変更禁止）
# ============================================================

# 勝率下限
MIN_WIN_RATE = 0.55

# 平均保持時間（秒）
MAX_AVG_HOLD_SECONDS = 160

# 損切比率上限
MAX_STOP_LOSS_RATIO = 0.20   # EXIT全体の20%超で警告

# トレード数下限（統計的に意味が出る最低数）
MIN_TRADES_FOR_CHECK = 10


# ============================================================
# メイン API
# ============================================================

def diagnose_trades(trades: List[Dict]) -> List[str]:
    """
    トレード結果から自己診断メッセージを生成する

    Parameters
    ----------
    trades : list of dict
    各要素は以下のキーを持つ想定
      - pnl : float
          トレード損益（+/-）
      - hold_seconds : int
          保持時間（秒）
      - exit_reason : str
          "TP_HALF" / "TP_FULL" / "STOP" / "TIME" / "MOMENTUM"

    Returns
    -------
    list of str
        警告・注意メッセージ（空なら正常）
    """

    messages: List[str] = []

    if not trades or len(trades) < MIN_TRADES_FOR_CHECK:
        return messages  # データ不足は診断しない

    total = len(trades)

    # --------------------------------------------------------
    # 勝率
    # --------------------------------------------------------
    wins = sum(1 for t in trades if t.get("pnl", 0.0) > 0)
    win_rate = wins / total

    if win_rate < MIN_WIN_RATE:
        messages.append(
            f"⚠ 勝率低下: {win_rate:.2%} (< {MIN_WIN_RATE:.0%})"
        )

    # --------------------------------------------------------
    # 平均保持時間
    # --------------------------------------------------------
    avg_hold = sum(
        int(t.get("hold_seconds", 0)) for t in trades
    ) / total

    if avg_hold > MAX_AVG_HOLD_SECONDS:
        messages.append(
            f"⚠ 平均HOLD時間が長い: {avg_hold:.1f}s"
        )

    # --------------------------------------------------------
    # EXIT 理由の偏り
    # --------------------------------------------------------
    stop_count = sum(
        1 for t in trades if t.get("exit_reason") == "STOP"
    )

    stop_ratio = stop_count / total

    if stop_ratio > MAX_STOP_LOSS_RATIO:
        messages.append(
            f"⚠ 損切比率が高い: {stop_ratio:.1%}"
        )

    # --------------------------------------------------------
    # 正常判定
    # --------------------------------------------------------
    if not messages:
        messages.append("OK: 殿様イナゴ状態は正常です")

    return messages


# ============================================================
# 補助（ログ用整形）
# ============================================================

def format_diagnosis(messages: List[str]) -> str:
    """
    診断メッセージをログ出力向けに整形
    """
    return " | ".join(messages)