# ============================================================
# File   : backtest/run_backtest.py
# ------------------------------------------------------------
# ✔ 戦略バックテスト本体（単一の評価軸）
# ✔ Optuna / 手動検証 / 日次バッチ すべてから呼ばれる
# ✔ trading / AI 実行系とは完全分離
# ✔ 評価関数（score）と search_space をここに集約
# ============================================================

from dataclasses import dataclass
from typing import Dict, Any, List, Tuple
import math
import logging

logger = logging.getLogger(__name__)


# ============================================================
# 探索対象パラメータ定義（戦略仕様）
# ============================================================
SEARCH_SPACE = {
    "AI_THRESHOLD": (0.55, 0.85),        # 即益AIの最低確率
    "MIN_VOLUME_SPEED": (3000, 12000),   # 出来高スピード下限
    "FAST_RET_MIN": (0.1, 0.5),          # 早期利確/勢い閾値（例）
}


# ============================================================
# バックテスト結果コンテナ
# ============================================================
@dataclass
class BacktestResult:
    total_pnl: float
    max_drawdown: float
    trade_count: int
    score: float


# ============================================================
# 評価関数（最重要）
# ============================================================
def evaluate_result(
    *,
    total_pnl: float,
    max_drawdown: float,
    trade_count: int,
) -> float:
    """
    戦略評価関数
    - 大きいほど良い
    - 利益 > ドローダウン > 取引数 の順で重視
    """
    score = (
        total_pnl
        - max_drawdown * 2.0
        - trade_count * 0.01
    )
    return float(score)


# ============================================================
# メイン：バックテスト実行
# ============================================================
def backtest(
    *,
    AI_THRESHOLD: float,
    MIN_VOLUME_SPEED: int,
    FAST_RET_MIN: float,
) -> Dict[str, Any]:
    """
    パラメータを受け取り、バックテストを実行して評価する。

    NOTE:
    - 実データの読み込み・約定ロジックはここに実装
    - Optuna はこの関数を何度も呼ぶだけ
    """

    # --------------------------------------------------------
    # ここから下は「例示用の骨組み」
    # 実際にはあなたのバックテスト実装に置き換える
    # --------------------------------------------------------

    # ---- 初期化 ----
    equity_curve: List[float] = []
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    trades = 0

    # --------------------------------------------------------
    # 仮想トレードループ（ダミー）
    # ※ 実装時はここを実データで置き換える
    # --------------------------------------------------------
    for i in range(1000):
        # 例：条件を満たすとトレード発生
        if (i % 10 == 0) and (AI_THRESHOLD >= 0.6) and (MIN_VOLUME_SPEED <= 8000):
            trades += 1

            # ダミー損益モデル（例）
            pnl = 1.0 if (i % 3 != 0) else -0.8

            equity += pnl
            equity_curve.append(equity)

            # ドローダウン計算
            peak = max(peak, equity)
            dd = peak - equity
            max_dd = max(max_dd, dd)

    total_pnl = equity
    max_drawdown = max_dd
    trade_count = trades

    # --------------------------------------------------------
    # 評価
    # --------------------------------------------------------
    score = evaluate_result(
        total_pnl=total_pnl,
        max_drawdown=max_drawdown,
        trade_count=trade_count,
    )

    result = BacktestResult(
        total_pnl=total_pnl,
        max_drawdown=max_drawdown,
        trade_count=trade_count,
        score=score,
    )

    return {
        "total_pnl": result.total_pnl,
        "max_drawdown": result.max_drawdown,
        "trade_count": result.trade_count,
        "score": result.score,
    }


# ============================================================
# 手動実行（デバッグ用）
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    res = backtest(
        AI_THRESHOLD=0.7,
        MIN_VOLUME_SPEED=6000,
        FAST_RET_MIN=0.3,
    )

    print("=== BAC
