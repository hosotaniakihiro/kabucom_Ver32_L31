# ============================================================
# File   : AI/confidence/confidence_bias.py
# Ver1.0-FINAL-CONFIDENCE-BIAS-LEARNING
# ------------------------------------------------------------
# ✔ 実損益から confidence を補正
# ✔ モデル再学習なし
# ✔ 副作用ゼロ
# ============================================================

from __future__ import annotations

import datetime as dt
from config import global_config
from database import Session_trade
from database.models import ConfidenceBias


# ============================================================
# config helper
# ============================================================
def _cfg(key: str, default):
    try:
        return global_config.get(key, default)
    except Exception:
        return default


# ============================================================
# core
# ============================================================
def update_confidence_bias(
    *,
    symbol: str,
    realized_pnl: float,
):
    """
    トレード結果から confidence bias を更新
    """

    MAX_RISK = _cfg("MAX_ENTRY_RISK_YEN", 30_000)
    LEARNING_RATE = _cfg("CONFIDENCE_LEARNING_RATE", 0.05)

    try:
        realized_pnl = float(realized_pnl)
    except Exception:
        return

    # 正規化
    normalized = max(
        -1.0,
        min(1.0, realized_pnl / max(MAX_RISK, 1.0)),
    )

    adjustment = normalized * LEARNING_RATE

    session = Session_trade()
    try:
        row = session.get(ConfidenceBias, symbol)
        if row is None:
            row = ConfidenceBias(
                symbol=symbol,
                bias=0.0,
                trade_count=0,
            )
            session.add(row)

        row.bias = max(
            -0.3,
            min(0.3, row.bias + adjustment),
        )
        row.trade_count += 1
        row.updated_at = dt.datetime.utcnow()

        session.commit()

    finally:
        session.close()


def apply_confidence_bias(
    *,
    symbol: str,
    confidence: float,
) -> float:
    """
    現在の confidence に bias を適用
    """

    session = Session_trade()
    try:
        row = session.get(ConfidenceBias, symbol)
        if not row:
            return confidence

        return max(
            0.0,
            min(1.0, confidence * (1.0 + row.bias)),
        )
    finally:
        session.close()