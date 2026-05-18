# ============================================================
# File   : trading/entry/entry_budget.py
# Version: PRODUCTION-ENTRY-BUDGET-CONFIG-V4-TIERED-1500-7000
# ------------------------------------------------------------
# 目的:
#   エントリー1回あたりの予算・最低株数・価格帯上限/下限を一元管理する。
#
# V4仕様:
#   - エントリー対象価格帯を 1,500円〜7,000円 に拡張
#   - 1,500円〜2,999円: MAX 500,000円
#   - 3,000円〜7,000円: MAX 700,000円
#   - 7,001円以上: 対象外
#
# 優先順:
#   config.global_config -> ENV -> setting.ini -> default
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRY_ONESHOT_YEN = 700_000.0
DEFAULT_ENTRY_LOW_PRICE_ONESHOT_YEN = 500_000.0
DEFAULT_ENTRY_HIGH_PRICE_ONESHOT_YEN = 700_000.0
DEFAULT_ORDER_LOT_SIZE = 100
DEFAULT_ENTRY_MIN_PRICE = 1_500.0
DEFAULT_ENTRY_TIER_SPLIT_PRICE = 3_000.0
DEFAULT_ENTRY_MAX_PRICE = 7_000.0


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

    try:
        from core.startup.settings_ini_loader import get_setting
        v = get_setting(key, None)
        if v is not None and str(v).strip() != "":
            return v
    except Exception:
        pass

    return default


def get_order_lot_size(default: int = DEFAULT_ORDER_LOT_SIZE) -> int:
    v = _safe_int(_cfg("ORDER_LOT_SIZE", default), default)
    return v if v > 0 else int(default)


def get_entry_min_price(default: float = DEFAULT_ENTRY_MIN_PRICE) -> float:
    v = _safe_float(_cfg("ENTRY_MIN_PRICE", default), default)
    return max(0.0, float(v))


def get_entry_tier_split_price(default: float = DEFAULT_ENTRY_TIER_SPLIT_PRICE) -> float:
    v = _safe_float(_cfg("ENTRY_TIER_SPLIT_PRICE", default), default)
    return max(0.0, float(v))


def get_entry_max_price(default: float = DEFAULT_ENTRY_MAX_PRICE) -> float:
    v = _safe_float(_cfg("ENTRY_MAX_PRICE", default), default)
    return max(0.0, float(v))


def get_entry_low_price_oneshot_yen(default: float = DEFAULT_ENTRY_LOW_PRICE_ONESHOT_YEN) -> float:
    v = _safe_float(_cfg("ENTRY_LOW_PRICE_ONESHOT_YEN", default), default)
    return v if v > 0 else float(default)


def get_entry_high_price_oneshot_yen(default: float = DEFAULT_ENTRY_HIGH_PRICE_ONESHOT_YEN) -> float:
    v = _safe_float(_cfg("ENTRY_HIGH_PRICE_ONESHOT_YEN", default), default)
    return v if v > 0 else float(default)


def get_max_entry_oneshot_yen(default: float = DEFAULT_MAX_ENTRY_ONESHOT_YEN) -> float:
    """
    価格が未指定の既定予算。
    互換性のため残す。数量計算では get_entry_oneshot_yen_for_price(price) を優先する。
    """
    v = _safe_float(_cfg("MAX_ENTRY_ONESHOT_YEN", default), default)
    return v if v > 0 else float(default)


def get_entry_oneshot_yen_for_price(price: Any) -> float:
    p = _safe_float(price, 0.0)
    split = get_entry_tier_split_price()
    low_budget = get_entry_low_price_oneshot_yen()
    high_budget = get_entry_high_price_oneshot_yen()

    if p > 0 and split > 0 and p < split:
        return float(low_budget)
    return float(high_budget)


def is_affordability_filter_enabled(default: bool = True) -> bool:
    v = _cfg("ENTRY_AFFORDABILITY_FILTER_ENABLED", "1" if default else "0")
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "ok"}:
        return True
    if s in {"0", "false", "no", "n", "off", "ng"}:
        return False
    return bool(default)


def get_max_affordable_price_for_min_lot(*, budget_yen: float | None = None, lot_size: int | None = None) -> float:
    budget = get_max_entry_oneshot_yen() if budget_yen is None else _safe_float(budget_yen, DEFAULT_MAX_ENTRY_ONESHOT_YEN)
    lot = get_order_lot_size() if lot_size is None else _safe_int(lot_size, DEFAULT_ORDER_LOT_SIZE)
    if budget <= 0 or lot <= 0:
        return 0.0
    return float(budget) / float(lot)


def get_effective_entry_max_price() -> float:
    return get_entry_max_price()


def can_afford_min_lot(price: Any) -> tuple[bool, dict[str, Any]]:
    p = _safe_float(price, 0.0)
    lot = get_order_lot_size()
    min_price = get_entry_min_price()
    split_price = get_entry_tier_split_price()
    configured_max_price = get_entry_max_price()
    budget = get_entry_oneshot_yen_for_price(p)
    affordable_max_price = get_max_affordable_price_for_min_lot(budget_yen=budget, lot_size=lot)
    effective_max_price = min(configured_max_price, affordable_max_price) if configured_max_price > 0 else affordable_max_price
    min_notional = p * lot if p > 0 else 0.0

    diag = {
        "enabled": is_affordability_filter_enabled(default=True),
        "price": p,
        "budget_yen": budget,
        "low_price_budget_yen": get_entry_low_price_oneshot_yen(),
        "high_price_budget_yen": get_entry_high_price_oneshot_yen(),
        "lot_size": lot,
        "min_price": min_price,
        "tier_split_price": split_price,
        "configured_max_price": configured_max_price,
        "affordable_max_price": affordable_max_price,
        "max_price": effective_max_price,
        "min_notional": min_notional,
        "source": "entry_budget_tiered_cfg_global_env_setting_ini_default",
    }

    if not is_affordability_filter_enabled(default=True):
        diag["enabled"] = False
        diag["reason"] = "filter_disabled"
        return True, diag

    if p <= 0:
        diag["reason"] = "price_missing_allow"
        return True, diag

    if min_price > 0 and p < min_price:
        diag["reason"] = "price_below_entry_min_price"
        return False, diag

    if configured_max_price > 0 and p > configured_max_price:
        diag["reason"] = "price_over_entry_max_price"
        return False, diag

    if effective_max_price > 0 and p > effective_max_price:
        diag["reason"] = "price_over_budget_for_min_lot"
        return False, diag

    diag["reason"] = "ok"
    return True, diag


def log_entry_budget_config(prefix: str = "[ENTRY BUDGET]") -> None:
    try:
        lot = get_order_lot_size()
        min_price = get_entry_min_price()
        split_price = get_entry_tier_split_price()
        max_price = get_entry_max_price()
        low_budget = get_entry_low_price_oneshot_yen()
        high_budget = get_entry_high_price_oneshot_yen()
        logger.warning(
            "%s tiered min_price=%.0f split_price=%.0f max_price=%.0f low_budget=%.0f high_budget=%.0f lot_size=%s affordability_filter=%s source=global_config_env_setting_ini_default",
            prefix, min_price, split_price, max_price, low_budget, high_budget, lot,
            is_affordability_filter_enabled(default=True),
        )
    except Exception:
        logger.exception("%s config log failed", prefix)
