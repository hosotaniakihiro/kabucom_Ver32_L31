# ============================================================
# File   : trading/exit/exit_price_source.py
# Version: V2.0-PRODUCTION-SAFE-PUSH-FIRST-EXIT-PRICE-SOURCE
# ------------------------------------------------------------
# 【概要】
#   EXIT用の現在値・5秒足取得。
#
# 目的:
#   - 5秒足があれば使う
#   - 5秒足がなければ PUSH 最新価格を使う
#   - GC.push が無い場合も global_data 側の価格キャッシュを探す
#   - 価格が取れない場合は安全に 0.0 を返して EXIT しない
#
# 優先順位:
#   1. GC.monitor.get_five_sec_bar(symbol)
#   2. GC.monitor.get_5sec_bar(symbol)
#   3. GC.monitor.get_latest_5sec_bar(symbol)
#   4. GC.push.get_tick(symbol)
#   5. GC.push.get_latest_tick(symbol)
#   6. GC.push.get_price(symbol)
#   7. global_data latest price maps
#   8. global_data push/tick maps
#
# 注意:
#   - このファイルは価格取得専用
#   - 注文は出さない
#   - DB更新もしない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Dict, Tuple

from core.global_context.context import global_context as GC
from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# basic helpers
# ============================================================

def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)

        x = float(v)

        if math.isnan(x) or math.isinf(x):
            return float(default)

        return x

    except Exception:
        return float(default)


def normalize_symbol(v: Any) -> str:
    if v is None:
        return ""

    s = str(v).strip()

    if s.endswith(".0"):
        s = s[:-2]

    return s


def dict_get_any(
    d: Dict[str, Any],
    *keys: str,
    default: Any = None,
) -> Any:
    if not isinstance(d, dict):
        return default

    for k in keys:
        if k in d:
            v = d.get(k)
            if v is not None:
                return v

    # 大文字小文字差異も吸収
    lower_map = {str(k).lower(): k for k in d.keys()}

    for k in keys:
        real_key = lower_map.get(str(k).lower())
        if real_key is not None:
            v = d.get(real_key)
            if v is not None:
                return v

    return default


def _is_valid_price(v: Any) -> bool:
    x = safe_float(v, 0.0)
    return bool(x > 0)


def _extract_datetime_any(d: Dict[str, Any]) -> Any:
    if not isinstance(d, dict):
        return None

    return dict_get_any(
        d,
        "datetime",
        "timestamp",
        "time",
        "Time",
        "CurrentTime",
        "current_time",
        "received_at",
        "updated_at",
        "created_at",
        default=None,
    )


def _normalize_dt(v: Any) -> dt.datetime | None:
    if v is None:
        return None

    try:
        import pandas as pd

        ts = pd.to_datetime(v, errors="coerce")

        if pd.isna(ts):
            return None

        try:
            return ts.to_pydatetime().replace(tzinfo=None)
        except Exception:
            return dt.datetime.fromisoformat(str(ts).replace("Z", ""))

    except Exception:
        try:
            if isinstance(v, dt.datetime):
                return v.replace(tzinfo=None)
            return dt.datetime.fromisoformat(str(v).replace("Z", ""))
        except Exception:
            return None


def _is_not_too_stale(
    ts: dt.datetime | None,
    *,
    max_age_seconds: float = 120.0,
) -> bool:
    """
    価格タイムスタンプが古すぎないか判定。

    注意:
      - ts が取れない場合は True にする。
        古さを判定できないだけで、価格自体は使える可能性があるため。
    """
    if ts is None:
        return True

    try:
        now = dt.datetime.now()
        age = abs((now - ts).total_seconds())
        return age <= float(max_age_seconds)

    except Exception:
        return True


# ============================================================
# 5sec bar
# ============================================================

def get_five_sec_bar_safe(symbol: str) -> Dict[str, Any]:
    """
    GC.monitor から5秒足相当の bar を取得する。

    取得できない場合:
        {}
    """
    symbol = normalize_symbol(symbol)

    if not symbol:
        return {}

    try:
        monitor = getattr(GC, "monitor", None)

        if monitor is None:
            return {}

        method_names = [
            "get_five_sec_bar",
            "get_5sec_bar",
            "get_latest_5sec_bar",
            "get_latest_five_sec_bar",
            "get_bar_5s",
            "get_5s_bar",
        ]

        for name in method_names:
            fn = getattr(monitor, name, None)

            if not callable(fn):
                continue

            try:
                bar = fn(symbol)

                if isinstance(bar, dict) and bar:
                    return bar

            except TypeError:
                # 引数なし形式の実装がある場合の保険
                try:
                    bar = fn()
                    if isinstance(bar, dict):
                        maybe = bar.get(symbol) or bar.get(str(symbol))
                        if isinstance(maybe, dict) and maybe:
                            return maybe
                except Exception:
                    pass

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
            "CurrentPrice",
            "last_price",
            "last",
            "Last",
            "close_price",
            default=0.0,
        ),
        0.0,
    )


# ============================================================
# GC.push price
# ============================================================

def _extract_price_from_tick(tick: Any) -> float:
    if tick is None:
        return 0.0

    if isinstance(tick, dict):
        return safe_float(
            dict_get_any(
                tick,
                "price",
                "current_price",
                "CurrentPrice",
                "last_price",
                "last",
                "Last",
                "close",
                "close_price",
                "Close",
                default=0.0,
            ),
            0.0,
        )

    # object attribute fallback
    for attr in [
        "price",
        "current_price",
        "CurrentPrice",
        "last_price",
        "last",
        "close",
        "close_price",
    ]:
        try:
            v = getattr(tick, attr, None)
            if _is_valid_price(v):
                return safe_float(v)
        except Exception:
            pass

    return 0.0


def get_push_price_safe(symbol: str) -> float:
    """
    GC.push から最新価格を取得する。
    """
    symbol = normalize_symbol(symbol)

    if not symbol:
        return 0.0

    try:
        push = getattr(GC, "push", None)

        if push is None:
            return 0.0

        method_names = [
            "get_tick",
            "get_latest_tick",
            "get_price",
            "get_latest_price",
            "latest_price",
        ]

        for name in method_names:
            fn = getattr(push, name, None)

            if not callable(fn):
                continue

            try:
                ret = fn(symbol)
                price = _extract_price_from_tick(ret)

                if price > 0:
                    return price

            except TypeError:
                try:
                    ret = fn()
                    if isinstance(ret, dict):
                        item = ret.get(symbol) or ret.get(str(symbol))
                        price = _extract_price_from_tick(item)

                        if price > 0:
                            return price
                except Exception:
                    pass

    except Exception:
        logger.debug("[PUSH PRICE] unavailable symbol=%s", symbol, exc_info=True)

    return 0.0


# ============================================================
# global_data fallback
# ============================================================

def _get_from_mapping_attr(
    attr_name: str,
    symbol: str,
) -> float:
    try:
        m = getattr(global_data, attr_name, None)

        if not isinstance(m, dict):
            return 0.0

        item = m.get(symbol) or m.get(str(symbol))

        if item is None:
            return 0.0

        if isinstance(item, dict):
            ts = _normalize_dt(_extract_datetime_any(item))

            if not _is_not_too_stale(ts):
                logger.debug(
                    "[EXIT PRICE] stale global_data.%s symbol=%s ts=%s",
                    attr_name,
                    symbol,
                    ts,
                )
                return 0.0

        return _extract_price_from_tick(item)

    except Exception:
        logger.debug(
            "[EXIT PRICE] global_data.%s unavailable symbol=%s",
            attr_name,
            symbol,
            exc_info=True,
        )
        return 0.0


def get_global_data_price_safe(symbol: str) -> float:
    """
    global_data 内の価格キャッシュから現在価格を探す。

    プロジェクト内で名前が変わっても拾えるように候補を広めにする。
    """
    symbol = normalize_symbol(symbol)

    if not symbol:
        return 0.0

    candidate_attrs = [
        "latest_price_map",
        "latest_prices",
        "current_price_map",
        "current_prices",
        "push_price_map",
        "push_prices",
        "push_latest_price_map",
        "push_latest_prices",
        "tick_map",
        "ticks",
        "latest_tick_map",
        "latest_ticks",
        "push_tick_map",
        "push_ticks",
        "push_latest_tick_map",
        "push_latest_ticks",
        "symbol_price_map",
        "price_map",
    ]

    for attr in candidate_attrs:
        price = _get_from_mapping_attr(attr, symbol)

        if price > 0:
            return price

    return 0.0


# ============================================================
# fallback from open position
# ============================================================

def get_position_price_fallback(symbol: str) -> float:
    """
    最終保険として position 内の price / current_price を見る。
    これは現在価格としては弱いので、最後にだけ使う。
    """
    symbol = normalize_symbol(symbol)

    if not symbol:
        return 0.0

    try:
        positions = getattr(global_data, "open_positions", None)

        if not isinstance(positions, dict):
            return 0.0

        pos = positions.get(symbol) or positions.get(str(symbol))

        if not isinstance(pos, dict):
            return 0.0

        return safe_float(
            dict_get_any(
                pos,
                "current_price",
                "price",
                "last_price",
                "exit_price",
                default=0.0,
            ),
            0.0,
        )

    except Exception:
        logger.debug(
            "[EXIT PRICE] position fallback unavailable symbol=%s",
            symbol,
            exc_info=True,
        )
        return 0.0


# ============================================================
# main
# ============================================================

def get_latest_exit_price(symbol: str) -> Tuple[float, Dict[str, Any]]:
    """
    EXIT判定用の最新価格を返す。

    Returns:
        price, bar5s

    bar5s:
        5秒足が取得できた場合は dict
        取得できない場合は {}
    """
    symbol = normalize_symbol(symbol)

    if not symbol:
        return 0.0, {}

    # --------------------------------------------------------
    # 1. 5秒足
    # --------------------------------------------------------
    bar5s = get_five_sec_bar_safe(symbol)

    if bar5s:
        price = extract_price_from_5sec_bar(bar5s)

        if price > 0:
            return price, bar5s

    # --------------------------------------------------------
    # 2. GC.push
    # --------------------------------------------------------
    price = get_push_price_safe(symbol)

    if price > 0:
        return price, bar5s or {}

    # --------------------------------------------------------
    # 3. global_data price maps
    # --------------------------------------------------------
    price = get_global_data_price_safe(symbol)

    if price > 0:
        return price, bar5s or {}

    # --------------------------------------------------------
    # 4. position fallback
    # --------------------------------------------------------
    price = get_position_price_fallback(symbol)

    if price > 0:
        logger.warning(
            "[EXIT PRICE] using position fallback price symbol=%s price=%.4f",
            symbol,
            price,
        )
        return price, bar5s or {}

    # --------------------------------------------------------
    # unavailable
    # --------------------------------------------------------
    logger.debug("[EXIT PRICE] unavailable symbol=%s", symbol)

    return 0.0, bar5s or {}


__all__ = [
    "safe_float",
    "normalize_symbol",
    "dict_get_any",
    "get_five_sec_bar_safe",
    "extract_price_from_5sec_bar",
    "get_push_price_safe",
    "get_global_data_price_safe",
    "get_position_price_fallback",
    "get_latest_exit_price",
]