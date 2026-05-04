# ============================================================
# trading/handlers/order_strategy.py
# ------------------------------------------------------------
# ✔ ENTRY 可否確定後の「注文戦略」専用
# ✔ STOP / LIMIT / MARKET 切替
# ✔ ATR / 直近高安 / VWAP 対応
# ✔ 判断ロジックは一切持たない
# ============================================================

import logging
from typing import Dict, Any

from config import global_config

logger = logging.getLogger(__name__)


# ============================================================
# メイン入口
# ============================================================
def decide_order_type(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    ENTRY 注文戦略を決定する（唯一の入口）

    Returns:
        {
            "order_type": "STOP" | "LIMIT" | "MARKET",
            "side": "BUY" | "SELL",
            "price": float | None,        # LIMIT / MARKET
            "stop_price": float | None,   # STOP
            "limit_price": float | None,  # STOP-LIMIT 用（任意）
            "reason": str,
        }
    """

    side = row.get("entry_decision")
    if side not in ("BUY", "SELL"):
        return _fail_plan("invalid_side")

    # --------------------------------------------------------
    # 設定（config 由来）
    # --------------------------------------------------------
    default_type = global_config.DEFAULT_ENTRY_ORDER_TYPE  # "stop" / "limit" / "market"

    # STOP を基本にする設計
    if default_type == "stop":
        return build_stop_entry_plan(row)

    if default_type == "limit":
        return build_limit_entry_plan(row)

    # fallback
    return build_market_entry_plan(row)


# ============================================================
# STOP エントリー
# ============================================================
def build_stop_entry_plan(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    ブレイクアウト前提の STOP ENTRY
    """

    side = row["entry_decision"]
    price = row.get("close_price")
    atr = row.get("atr", 0) or 0
    vwap = row.get("vwap")

    if price is None:
        return _fail_plan("no_price")

    # --------------------------------------------------------
    # STOP 価格計算
    # --------------------------------------------------------
    # ATR ベース or パーセント（安全側）
    atr_offset = atr * global_config.STOP_ATR_MULTIPLIER
    pct_offset = price * global_config.STOP_PCT_OFFSET

    offset = max(atr_offset, pct_offset)

    if side == "BUY":
        stop_price = price + offset
        # VWAP より下に STOP を置かない
        if vwap and stop_price < vwap:
            stop_price = vwap
    else:
        stop_price = price - offset
        if vwap and stop_price > vwap:
            stop_price = vwap

    stop_price = _round_price(stop_price)

    return {
        "order_type": "STOP",
        "side": side,
        "price": None,
        "stop_price": stop_price,
        "limit_price": None,
        "reason": "stop_breakout",
    }


# ============================================================
# LIMIT エントリー
# ============================================================
def build_limit_entry_plan(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    押し目 / 戻り売り LIMIT ENTRY
    """

    side = row["entry_decision"]
    price = row.get("close_price")
    atr = row.get("atr", 0) or 0

    if price is None:
        return _fail_plan("no_price")

    # --------------------------------------------------------
    # LIMIT 価格
    # --------------------------------------------------------
    offset = max(atr * global_config.LIMIT_ATR_MULTIPLIER, price * 0.001)

    if side == "BUY":
        limit_price = price - offset
    else:
        limit_price = price + offset

    limit_price = _round_price(limit_price)

    return {
        "order_type": "LIMIT",
        "side": side,
        "price": limit_price,
        "stop_price": None,
        "limit_price": limit_price,
        "reason": "pullback_limit",
    }


# ============================================================
# MARKET エントリー
# ============================================================
def build_market_entry_plan(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    即時成行 ENTRY
    """

    side = row["entry_decision"]
    price = row.get("close_price")

    if price is None:
        return _fail_plan("no_price")

    return {
        "order_type": "MARKET",
        "side": side,
        "price": None,
        "stop_price": None,
        "limit_price": None,
        "reason": "market_entry",
    }


# ============================================================
# 共通ユーティリティ
# ============================================================
def _round_price(price: float) -> float:
    """
    価格丸め（呼値単位対応）
    ※ 将来 tick_size 対応可
    """
    try:
        return round(float(price), 1)
    except Exception:
        return price


def _fail_plan(reason: str) -> Dict[str, Any]:
    logger.warning("[ORDER STRATEGY FAIL] %s", reason)
    return {
        "order_type": None,
        "side": None,
        "price": None,
        "stop_price": None,
        "limit_price": None,
        "reason": reason,
    }
