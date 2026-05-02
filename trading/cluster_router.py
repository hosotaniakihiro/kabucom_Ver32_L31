# ============================================================
# File   : trading/ai/cluster_router.py
# Version: FINAL-ROBUST-CLUSTER-ROUTER
# ------------------------------------------------------------
# ✔ 流動性 / ボラ / 価格帯で分類
# ✔ 将来拡張可能
# ✔ NaN耐性
# ============================================================

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def route_cluster(row: dict) -> str:

    try:
        volume = float(row.get("volume", 0) or 0)
        atr = float(row.get("atr", 0) or 0)
        price = float(row.get("close_price", 0) or 0)

    except Exception:
        return "unknown"

    # 高流動性
    if volume > 5_000_000:
        return "high_liquidity"

    # 高ボラ
    if atr > 10:
        return "high_volatility"

    # 低位株
    if price < 300:
        return "low_price"

    return "normal"