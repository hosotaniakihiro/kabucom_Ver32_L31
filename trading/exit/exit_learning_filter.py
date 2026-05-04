# ============================================================
# trading/exit/exit_learning_filter.py
# Ver1.0.0-FINAL-EXIT-LEARNING-FILTER
# ------------------------------------------------------------
# ✔ 学習用に使える EXIT かどうかを判定
# ✔ EXIT ロジック・実行・DB から完全独立
# ✔ TradeExitStats / EntryEvent 両対応
# ✔ ルールは ADD ONLY
# ============================================================

from __future__ import annotations

from typing import Optional

from trading.exit.exit_context import ExitContext


# ============================================================
# 学習対象判定
# ============================================================

def is_valid_exit_for_learning(
    *,
    ctx: ExitContext,
    exit_reason: str,
    pnl: float,
    holding_seconds: int,
) -> bool:
    """
    この EXIT を AI 学習に使ってよいか？

    判断基準（最小・安全）：
    - EXIT_REASON が明確
    - holding_seconds > 0
    - MFE / MAE が計測されている
    - 強制終了（APIエラー等）を除外
    """

    if holding_seconds <= 0:
        return False

    if exit_reason in (
        "UNKNOWN",
        "API_ERROR",
        "FORCE_CLOSE",
    ):
        return False

    # MFE / MAE が意味を持たないケースを除外
    if ctx.mfe == 0 and ctx.mae == 0:
        return False

    # 即死（ノイズ）除外（例：約定直後エラー）
    if abs(pnl) < 1e-6:
        return False

    return True
