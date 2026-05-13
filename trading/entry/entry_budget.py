# ============================================================
# File   : trading/entry/entry_budget.py
# Version: PRODUCTION-ENTRY-BUDGET-CONFIG-V1
# ------------------------------------------------------------
# 目的:
#   エントリー1回あたりの予算・最低株数・価格上限を一元管理する。
#
# 背景:
#   50万円 / 100株単位の場合、株価が5000円を超える銘柄は
#   最低100株でも50万円を超えるため、最終的に qty=0 で落ちる。
#   それをAI判定後に落とすとAI枠を無駄に消費する。
#
# 方針:
#   - MAX_ENTRY_ONESHOT_YEN を増額すれば、AI前価格上限も自動で変わる
#   - ORDER_LOT_SIZE を変更しても、同じ計算式で追随する
#   - ENVで一時上書きも可能
#   - config.global_config が読める場合はそこを優先
#
# 主な設定:
#   MAX_ENTRY_ONESHOT_YEN 既定 500000
#   ORDER_LOT_SIZE        既定 100
#   ENTRY_AFFORDABILITY_FILTER_ENABLED 既定 1
#
# 例:
#   予算 500,000 / 100株 -> 最大価格 5,000円
#   予算 1,000,000 / 100株 -> 最大価格 10,000円
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRY_ONESHOT_YEN = 500_000.0
DEFAULT_ORDER_LOT_SIZE = 100


def _safe_float(v: Any, default: float) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int) -> int:
    try:
        if v is None or v == "":
            return int(default)
        x = int(float(v))
        return x if x > 0 else int(default)
    except Exception:
        return int(default)


def _cfg(key: str, default: Any) -> Any:
    """
    config.global_config -> ENV -> default の順で読む。
    global_config がdict風/オブジェクト風どちらでも落ちない。
    """
    try:
        from config import global_config

        try:
            v = global_config.get(key, None)
            if v is not None and v != "":
                return v
        except Exception:
            pass

        try:
            if hasattr(global_config, key):
                v = getattr(global_config, key)
                if v is not None and v != "":
                    return v
        except Exception:
            pass
    except Exception:
        pass

    try:
        v = os.getenv(key)
        if v is not None and str(v).strip() != "":
            return v
    except Exception:
        pass

    return default


def get_max_entry_oneshot_yen(default: float = DEFAULT_MAX_ENTRY_ONESHOT_YEN) -> float:
    v = _safe_float(_cfg("MAX_ENTRY_ONESHOT_YEN", default), default)
    return v if v > 0 else float(default)


def get_order_lot_size(default: int = DEFAULT_ORDER_LOT_SIZE) -> int:
    v = _safe_int(_cfg("ORDER_LOT_SIZE", default), default)
    return v if v > 0 else int(default)


def is_affordability_filter_enabled(default: bool = True) -> bool:
    v = _cfg("ENTRY_AFFORDABILITY_FILTER_ENABLED", "1" if default else "0")
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "ok"}:
        return True
    if s in {"0", "false", "no", "n", "off", "ng"}:
        return False
    return bool(default)


def get_max_affordable_price_for_min_lot(
    *,
    budget_yen: float | None = None,
    lot_size: int | None = None,
) -> float:
    """
    最低1単元を買える最大価格を返す。

    例:
      budget=500000, lot=100 -> 5000
      budget=1000000, lot=100 -> 10000
    """
    budget = get_max_entry_oneshot_yen() if budget_yen is None else _safe_float(budget_yen, DEFAULT_MAX_ENTRY_ONESHOT_YEN)
    lot = get_order_lot_size() if lot_size is None else _safe_int(lot_size, DEFAULT_ORDER_LOT_SIZE)

    if budget <= 0 or lot <= 0:
        return 0.0

    return float(budget) / float(lot)


def can_afford_min_lot(price: Any) -> tuple[bool, dict[str, Any]]:
    """
    指定価格で最低1単元を買えるかを判定する。
    BUY/SELLとも新規建ての最低発注単位チェックとして使う。
    """
    p = _safe_float(price, 0.0)
    budget = get_max_entry_oneshot_yen()
    lot = get_order_lot_size()
    max_price = get_max_affordable_price_for_min_lot(budget_yen=budget, lot_size=lot)
    min_notional = p * lot if p > 0 else 0.0

    if not is_affordability_filter_enabled(default=True):
        return True, {
            "enabled": False,
            "price": p,
            "budget_yen": budget,
            "lot_size": lot,
            "max_price": max_price,
            "min_notional": min_notional,
        }

    ok = bool(p <= 0 or p <= max_price)
    return ok, {
        "enabled": True,
        "price": p,
        "budget_yen": budget,
        "lot_size": lot,
        "max_price": max_price,
        "min_notional": min_notional,
    }


def log_entry_budget_config(prefix: str = "[ENTRY BUDGET]") -> None:
    try:
        budget = get_max_entry_oneshot_yen()
        lot = get_order_lot_size()
        max_price = get_max_affordable_price_for_min_lot(budget_yen=budget, lot_size=lot)
        logger.warning(
            "%s max_oneshot_yen=%.0f lot_size=%s max_affordable_price=%.2f affordability_filter=%s",
            prefix,
            budget,
            lot,
            max_price,
            is_affordability_filter_enabled(default=True),
        )
    except Exception:
        logger.exception("%s config log failed", prefix)
