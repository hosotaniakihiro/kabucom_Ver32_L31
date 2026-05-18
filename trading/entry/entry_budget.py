# ============================================================
# File   : trading/entry/entry_budget.py
# Version: PRODUCTION-ENTRY-BUDGET-CONFIG-V2-PRICE-RANGE-3000-7000
# ------------------------------------------------------------
# 目的:
#   エントリー1回あたりの予算・最低株数・価格帯上限/下限を一元管理する。
#
# 背景:
#   50万円 / 100株単位の場合、株価が5000円を超える銘柄は
#   最低100株でも50万円を超えるため、最終的に qty=0 で落ちる。
#   それをAI判定後に落とすとAI枠を無駄に消費する。
#
# 重要修正 V2:
#   - エントリー対象価格帯を明示管理する
#   - 既定は ENTRY_MIN_PRICE=3000 / ENTRY_MAX_PRICE=7000
#   - MAX_ENTRY_ONESHOT_YEN 既定を 700000 に統一
#   - can_afford_min_lot() で 3000円未満/7000円超をAI前に除外する
#
# 方針:
#   - MAX_ENTRY_ONESHOT_YEN を増額すれば、AI前価格上限も自動で変わる
#   - ENTRY_MAX_PRICE が設定されている場合は、予算上限と価格帯上限の小さい方を使う
#   - ENTRY_MIN_PRICE で低価格株を除外する
#   - ORDER_LOT_SIZE を変更しても、同じ計算式で追随する
#   - ENVで一時上書きも可能
#   - config.global_config が読める場合はそこを優先
#
# 主な設定:
#   MAX_ENTRY_ONESHOT_YEN 既定 700000
#   ORDER_LOT_SIZE        既定 100
#   ENTRY_MIN_PRICE       既定 3000
#   ENTRY_MAX_PRICE       既定 7000
#   ENTRY_AFFORDABILITY_FILTER_ENABLED 既定 1
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRY_ONESHOT_YEN = 700_000.0
DEFAULT_ORDER_LOT_SIZE = 100
DEFAULT_ENTRY_MIN_PRICE = 3_000.0
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


def get_entry_min_price(default: float = DEFAULT_ENTRY_MIN_PRICE) -> float:
    v = _safe_float(_cfg("ENTRY_MIN_PRICE", default), default)
    return max(0.0, float(v))


def get_entry_max_price(default: float = DEFAULT_ENTRY_MAX_PRICE) -> float:
    v = _safe_float(_cfg("ENTRY_MAX_PRICE", default), default)
    return max(0.0, float(v))


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
      budget=700000, lot=100 -> 7000
      budget=1000000, lot=100 -> 10000
    """
    budget = get_max_entry_oneshot_yen() if budget_yen is None else _safe_float(budget_yen, DEFAULT_MAX_ENTRY_ONESHOT_YEN)
    lot = get_order_lot_size() if lot_size is None else _safe_int(lot_size, DEFAULT_ORDER_LOT_SIZE)

    if budget <= 0 or lot <= 0:
        return 0.0

    return float(budget) / float(lot)


def get_effective_entry_max_price() -> float:
    """
    実際にAI前フィルタで使う価格上限。

    ENTRY_MAX_PRICE と 最低1単元を買える価格上限 の小さい方を使う。
    これにより、7000円以下を希望していても、予算が不足する場合は安全側に倒す。
    """
    configured_max = get_entry_max_price()
    affordable_max = get_max_affordable_price_for_min_lot()

    vals = [x for x in (configured_max, affordable_max) if x and x > 0]
    if not vals:
        return 0.0
    return float(min(vals))


def can_afford_min_lot(price: Any) -> tuple[bool, dict[str, Any]]:
    """
    指定価格でエントリー対象価格帯かつ最低1単元を買えるかを判定する。
    BUY/SELLとも新規建ての最低発注単位チェックとして使う。
    """
    p = _safe_float(price, 0.0)
    budget = get_max_entry_oneshot_yen()
    lot = get_order_lot_size()
    min_price = get_entry_min_price()
    configured_max_price = get_entry_max_price()
    affordable_max_price = get_max_affordable_price_for_min_lot(budget_yen=budget, lot_size=lot)
    effective_max_price = get_effective_entry_max_price()
    min_notional = p * lot if p > 0 else 0.0

    diag = {
        "enabled": is_affordability_filter_enabled(default=True),
        "price": p,
        "budget_yen": budget,
        "lot_size": lot,
        "min_price": min_price,
        "configured_max_price": configured_max_price,
        "affordable_max_price": affordable_max_price,
        "max_price": effective_max_price,
        "min_notional": min_notional,
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

    if effective_max_price > 0 and p > effective_max_price:
        if configured_max_price > 0 and p > configured_max_price:
            diag["reason"] = "price_over_entry_max_price"
        else:
            diag["reason"] = "price_over_budget_for_min_lot"
        return False, diag

    diag["reason"] = "ok"
    return True, diag


def log_entry_budget_config(prefix: str = "[ENTRY BUDGET]") -> None:
    try:
        budget = get_max_entry_oneshot_yen()
        lot = get_order_lot_size()
        affordable_max = get_max_affordable_price_for_min_lot(budget_yen=budget, lot_size=lot)
        min_price = get_entry_min_price()
        configured_max = get_entry_max_price()
        effective_max = get_effective_entry_max_price()
        logger.warning(
            "%s max_oneshot_yen=%.0f lot_size=%s min_price=%.2f configured_max_price=%.2f affordable_max_price=%.2f effective_max_price=%.2f affordability_filter=%s",
            prefix,
            budget,
            lot,
            min_price,
            configured_max,
            affordable_max,
            effective_max,
            is_affordability_filter_enabled(default=True),
        )
    except Exception:
        logger.exception("%s config log failed", prefix)
