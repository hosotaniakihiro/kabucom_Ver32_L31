# ============================================================
# File   : AI/tools/exit_replay_engine.py
# Ver1.0-FINAL-EXIT-REPLAY-ENGINE
# ------------------------------------------------------------
# ✔ ENTRY → EXIT AI を過去データで完全再現
# ✔ ai_exit_gate をそのまま使用（本番と同一判断）
# ✔ DB非依存（純粋関数的に検証可能）
# ✔ バックテスト / 検証 / デバッグ用
# ============================================================

from dataclasses import dataclass
from typing import Iterable, Tuple, Dict, Optional
from datetime import datetime
import logging

from AI.exit_gate import ai_exit_check, ExitDecision

logger = logging.getLogger(__name__)


# ============================================================
# POSITION SNAPSHOT（最小契約）
# ============================================================

@dataclass
class ReplayPosition:
    symbol: str
    entry_time: datetime
    entry_price: float
    entry_features: Dict[str, float]

    # 動的に更新される
    elapsed_seconds: int = 0


# ============================================================
# MARKET FEATURE STREAM CONTRACT
# ============================================================
# Iterable[Tuple[datetime, Dict[str, float]]]
#
# 例：
# [
#   (ts1, {"price_from_entry": 0.3, "price_velocity": 0.02, ...}),
#   (ts2, {...}),
# ]
# ============================================================


# ============================================================
# EXIT REPLAY CORE
# ============================================================

def replay_exit(
    position: ReplayPosition,
    market_feature_stream: Iterable[Tuple[datetime, Dict[str, float]]],
) -> Optional[Dict]:
    """
    EXIT AI を過去データで再生する

    Args:
        position:
            ReplayPosition（ENTRY時スナップショット）
        market_feature_stream:
            (timestamp, market_features) の iterable

    Returns:
        dict or None:
            {
                "symbol": str,
                "exit_time": datetime,
                "exit_price": float | None,
                "reason": str,
                "confidence": float,
                "detail": dict,
            }
            ※ 最後まで EXIT しなければ None
    """

    for ts, features in market_feature_stream:
        # 経過秒数更新
        position.elapsed_seconds = int((ts - position.entry_time).total_seconds())
        if position.elapsed_seconds < 0:
            continue

        decision: ExitDecision = ai_exit_check(
            position=position,
            market_features=features,
        )

        if decision:
            logger.info(
                f"[EXIT REPLAY] symbol={position.symbol} "
                f"reason={decision.reason} conf={decision.confidence:.3f} "
                f"elapsed={position.elapsed_seconds}s"
            )

            return {
                "symbol": position.symbol,
                "exit_time": ts,
                # 価格は feature 側で price_from_entry 等から復元する想定
                "exit_price": None,
                "reason": decision.reason,
                "confidence": decision.confidence,
                "detail": decision.detail,
            }

    # 最後まで EXIT なし
    logger.info(
        f"[EXIT REPLAY] symbol={position.symbol} no exit triggered"
    )
    return None


# ============================================================
# EXAMPLE USAGE（バックテスト側で呼ぶ）
# ============================================================
#
# position = ReplayPosition(
#     symbol="8306",
#     entry_time=entry_ts,
#     entry_price=entry_price,
#     entry_features=entry_features,
# )
#
# result = replay_exit(position, feature_stream)
#
# if result:
#     print(result)
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
