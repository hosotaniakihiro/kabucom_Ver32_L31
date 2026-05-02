# ============================================================
# File   : trading/exit/exit_price_source.py
# Version: V1.0-SPLIT-PRICE-SOURCE
# ------------------------------------------------------------
# 【概要】
#   EXIT用の現在値・5秒足取得。
#
# 【優先順位】
#   1. GC.monitor.get_five_sec_bar(symbol)
#   2. GC.monitor.get_5sec_bar(symbol)
#   3. GC.monitor.get_latest_5sec_bar(symbol)
#   4. GC.push.get_tick(symbol)
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

from core.global_context.context import global_context as GC
from trading.exit.exit_utils import dict_get_any, safe_float

logger = logging.getLogger(__name__)


def get_five_sec_bar_safe(symbol: str) -> Dict[str, Any]:
    try:
        if not hasattr(GC, "monitor") or GC.monitor is None:
            return {}

        if hasattr(GC.monitor, "get_five_sec_bar"):
            bar = GC.monitor.get_five_sec_bar(symbol)
            if isinstance(bar, dict):
                return bar

        if hasattr(GC.monitor, "get_5sec_bar"):
            bar = GC.monitor.get_5sec_bar(symbol)
            if isinstance(bar, dict):
                return bar

        if hasattr(GC.monitor, "get_latest_5sec_bar"):
            bar = GC.monitor.get_latest_5sec_bar(symbol)
            if isinstance(bar, dict):
                return bar

    except Exception:
        logger.debug("[5SEC BAR] unavailable symbol=%s", symbol, exc_info=True)

    return {}


def extract_price_from_5sec_bar(bar: Dict[str, Any]) -> float:
    if not isinstance(bar, dict) or not bar:
        return 0.0

    return safe_float(
        dict_get_any(
            bar,
            "close",
            "Close",
            "price",
            "current_price",
            "last_price",
            "last",
            default=0.0,
        ),
        0.0,
    )


def get_push_price_safe(symbol: str) -> float:
    try:
        if not hasattr(GC, "push") or GC.push is None:
            return 0.0

        tick = GC.push.get_tick(symbol)
        if not tick:
            return 0.0

        if isinstance(tick, dict):
            return safe_float(
                dict_get_any(
                    tick,
                    "price",
                    "current_price",
                    "last_price",
                    "last",
                    "close",
                    default=0.0,
                ),
                0.0,
            )

    except Exception:
        logger.debug("[PUSH PRICE] unavailable symbol=%s", symbol, exc_info=True)

    return 0.0


def get_latest_exit_price(symbol: str) -> Tuple[float, Dict[str, Any]]:
    """
    戻り値:
      price, bar5s

    bar5s:
      取得できない場合は {}
    """

    bar5s = get_five_sec_bar_safe(symbol)

    price = 0.0
    if bar5s:
        price = extract_price_from_5sec_bar(bar5s)

    if not price:
        price = get_push_price_safe(symbol)

    return price, bar5s


__all__ = [
    "get_five_sec_bar_safe",
    "extract_price_from_5sec_bar",
    "get_push_price_safe",
    "get_latest_exit_price",
]