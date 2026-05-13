# ============================================================
# File   : trading/exit/exit_price_source.py
# Version: V2.2-PRODUCTION-EXIT-PRICE-FAST-FALLBACK
# ------------------------------------------------------------
# EXIT用の現在値・5秒足取得。
#
# 重要修正:
#   - EXIT判定を kabu board API の同期タイムアウトで詰まらせない
#   - 価格取得優先順:
#       1) 5秒足
#       2) PUSH最新tick
#       3) global_data上の最新価格/tick
#       4) 保有ポジション上の current_price 等
#       5) kabu board API fallback
#   - kabu board fallback は短い timeout / symbol別cache / symbol別cooldown / 全体circuit breaker
#   - board timeout は stack trace を出さず warning 1行に抑制
#
# 背景:
#   kabu station API /board が localhost:18080 で read timeout になると、
#   EXIT判定ループが遅延し、結果としてイグジット発火が遅れる/止まる。
#   board は最後の保険に留め、通常は5秒足/PUSHを使う。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import time
from typing import Any, Dict, Tuple

import requests

from core.global_context.context import global_context as GC
from global_state import global_data

logger = logging.getLogger(__name__)

KABU_API_URL = "http://localhost:18080/kabusapi"

# board API は最後の保険。EXITループを止めないため短くする。
# requests の timeout は (connect timeout, read timeout)
KABU_BOARD_TIMEOUT = (0.25, 0.75)

# board価格キャッシュ。boardを連打しない。
KABU_BOARD_CACHE_TTL_SEC = 3.0

# board失敗後、同一銘柄は一定時間 board を呼ばない。
KABU_BOARD_FAIL_COOLDOWN_SEC = 15.0

# board失敗が連続した場合、全体を短時間止める。
KABU_BOARD_GLOBAL_COOLDOWN_SEC = 10.0
KABU_BOARD_GLOBAL_FAIL_THRESHOLD = 5

_board_price_cache: Dict[str, Tuple[float, float]] = {}
_board_fail_until_by_symbol: Dict[str, float] = {}
_board_global_fail_count = 0
_board_global_disabled_until = 0.0
_board_session: requests.Session | None = None


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


def dict_get_any(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d:
            v = d.get(k)
            if v is not None:
                return v
    try:
        lower_map = {str(k).lower(): k for k in d.keys()}
        for k in keys:
            real_key = lower_map.get(str(k).lower())
            if real_key is not None:
                v = d.get(real_key)
                if v is not None:
                    return v
    except Exception:
        pass
    return default


def _is_valid_price(v: Any) -> bool:
    x = safe_float(v, 0.0)
    return bool(x > 0)


def _extract_datetime_any(d: Dict[str, Any]) -> Any:
    if not isinstance(d, dict):
        return None
    return dict_get_any(
        d,
        "datetime", "timestamp", "time", "Time", "CurrentTime", "current_time",
        "received_at", "updated_at", "created_at",
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
        return ts.to_pydatetime().replace(tzinfo=None)
    except Exception:
        try:
            if isinstance(v, dt.datetime):
                return v.replace(tzinfo=None)
            return dt.datetime.fromisoformat(str(v).replace("Z", ""))
        except Exception:
            return None


def _is_not_too_stale(ts: dt.datetime | None, *, max_age_seconds: float = 120.0) -> bool:
    if ts is None:
        return True
    try:
        age = abs((dt.datetime.now() - ts).total_seconds())
        return age <= float(max_age_seconds)
    except Exception:
        return True


def get_five_sec_bar_safe(symbol: str) -> Dict[str, Any]:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return {}
    try:
        monitor = getattr(GC, "monitor", None)
        if monitor is None:
            return {}
        for name in [
            "get_five_sec_bar", "get_5sec_bar", "get_latest_5sec_bar",
            "get_latest_five_sec_bar", "get_bar_5s", "get_5s_bar",
        ]:
            fn = getattr(monitor, name, None)
            if not callable(fn):
                continue
            try:
                bar = fn(symbol)
                if isinstance(bar, dict) and bar:
                    return bar
            except TypeError:
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
            "close", "Close", "price", "current_price", "CurrentPrice",
            "last_price", "last", "Last", "close_price",
            default=0.0,
        ),
        0.0,
    )


def _extract_price_from_tick(tick: Any) -> float:
    if tick is None:
        return 0.0
    if isinstance(tick, dict):
        return safe_float(
            dict_get_any(
                tick,
                "price", "current_price", "CurrentPrice", "last_price", "last", "Last",
                "close", "close_price", "Close", "currentPrice",
                default=0.0,
            ),
            0.0,
        )
    for attr in ["price", "current_price", "CurrentPrice", "last_price", "last", "close", "close_price"]:
        try:
            v = getattr(tick, attr, None)
            if _is_valid_price(v):
                return safe_float(v)
        except Exception:
            pass
    return 0.0


def get_push_price_safe(symbol: str) -> float:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return 0.0
    try:
        push = getattr(GC, "push", None)
        if push is None:
            return 0.0
        for name in ["get_tick", "get_latest_tick", "get_price", "get_latest_price", "latest_price"]:
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


def _get_from_mapping_attr(attr_name: str, symbol: str) -> float:
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
                logger.debug("[EXIT PRICE] stale global_data.%s symbol=%s ts=%s", attr_name, symbol, ts)
                return 0.0
        return _extract_price_from_tick(item)
    except Exception:
        logger.debug("[EXIT PRICE] global_data.%s unavailable symbol=%s", attr_name, symbol, exc_info=True)
        return 0.0


def get_global_data_price_safe(symbol: str) -> float:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return 0.0
    for attr in [
        "latest_price_map", "latest_prices", "current_price_map", "current_prices",
        "push_price_map", "push_prices", "push_latest_price_map", "push_latest_prices",
        "tick_map", "ticks", "latest_tick_map", "latest_ticks",
        "push_tick_map", "push_ticks", "push_latest_tick_map", "push_latest_ticks",
        "symbol_price_map", "price_map",
    ]:
        price = _get_from_mapping_attr(attr, symbol)
        if price > 0:
            return price
    return 0.0


def get_position_price_fallback(symbol: str) -> float:
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
                "current_price", "CurrentPrice", "price", "last_price", "exit_price", "close",
                default=0.0,
            ),
            0.0,
        )
    except Exception:
        logger.debug("[EXIT PRICE] position fallback unavailable symbol=%s", symbol, exc_info=True)
        return 0.0


def _get_board_session() -> requests.Session:
    global _board_session
    if _board_session is None:
        _board_session = requests.Session()
    return _board_session


def _get_cached_board_price(symbol: str) -> float:
    item = _board_price_cache.get(symbol)
    if not item:
        return 0.0
    price, saved_at = item
    if (time.monotonic() - saved_at) <= KABU_BOARD_CACHE_TTL_SEC and price > 0:
        return float(price)
    return 0.0


def _remember_board_success(symbol: str, price: float) -> None:
    global _board_global_fail_count, _board_global_disabled_until
    now = time.monotonic()
    _board_price_cache[symbol] = (float(price), now)
    _board_fail_until_by_symbol.pop(symbol, None)
    _board_global_fail_count = 0
    _board_global_disabled_until = 0.0


def _remember_board_failure(symbol: str, reason: str) -> None:
    global _board_global_fail_count, _board_global_disabled_until
    now = time.monotonic()
    _board_fail_until_by_symbol[symbol] = now + KABU_BOARD_FAIL_COOLDOWN_SEC
    _board_global_fail_count += 1
    if _board_global_fail_count >= KABU_BOARD_GLOBAL_FAIL_THRESHOLD:
        _board_global_disabled_until = now + KABU_BOARD_GLOBAL_COOLDOWN_SEC
        logger.warning(
            "[EXIT PRICE] kabu board global cooldown start fails=%s cooldown=%.1fs last_symbol=%s reason=%s",
            _board_global_fail_count,
            KABU_BOARD_GLOBAL_COOLDOWN_SEC,
            symbol,
            reason,
        )


def _can_call_board(symbol: str) -> bool:
    now = time.monotonic()
    if _board_global_disabled_until > now:
        return False
    if _board_fail_until_by_symbol.get(symbol, 0.0) > now:
        return False
    return True


def get_kabu_board_price_safe(symbol: str, *, exchange: int = 1) -> float:
    """kabuステーション /board から現在値を取得する。PUSH未登録の保有銘柄向け最終fallback。

    注意:
        EXITループを止めないため、ここでは timeout/connection 系の例外で stack trace を出さない。
        連続失敗時は symbol別/全体の cooldown で board API を抑制する。
    """
    symbol = normalize_symbol(symbol)
    if not symbol:
        return 0.0

    cached = _get_cached_board_price(symbol)
    if cached > 0:
        return cached

    if not _can_call_board(symbol):
        return 0.0

    try:
        from token_manager import get_valid_token
        token = get_valid_token()
        if not token:
            _remember_board_failure(symbol, "token_unavailable")
            logger.warning("[EXIT PRICE] kabu board token unavailable symbol=%s", symbol)
            return 0.0

        url = f"{KABU_API_URL}/board/{symbol}@{int(exchange or 1)}"
        headers = {"X-API-KEY": token}
        started = time.monotonic()
        res = _get_board_session().get(url, headers=headers, timeout=KABU_BOARD_TIMEOUT)
        elapsed = time.monotonic() - started

        if res.status_code != 200:
            _remember_board_failure(symbol, f"http_{res.status_code}")
            logger.warning(
                "[EXIT PRICE] kabu board HTTP error symbol=%s status=%s elapsed=%.3fs text=%s",
                symbol,
                res.status_code,
                elapsed,
                (res.text or "")[:200],
            )
            return 0.0

        data = res.json()
        price = safe_float(
            dict_get_any(
                data,
                "CurrentPrice", "current_price", "price", "Price", "LastPrice", "last_price",
                default=0.0,
            ),
            0.0,
        )
        if price > 0:
            _remember_board_success(symbol, price)
            logger.info("[EXIT PRICE] using kabu board price symbol=%s price=%.4f elapsed=%.3fs", symbol, price, elapsed)
            return price

        _remember_board_failure(symbol, "price_empty")
        logger.warning(
            "[EXIT PRICE] kabu board price empty symbol=%s elapsed=%.3fs data_keys=%s",
            symbol,
            elapsed,
            list(data.keys()) if isinstance(data, dict) else type(data),
        )
        return 0.0

    except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout) as e:
        _remember_board_failure(symbol, "timeout")
        logger.warning("[EXIT PRICE] kabu board timeout symbol=%s timeout=%s err=%s", symbol, KABU_BOARD_TIMEOUT, e)
        return 0.0
    except requests.exceptions.RequestException as e:
        _remember_board_failure(symbol, "request_exception")
        logger.warning("[EXIT PRICE] kabu board request failed symbol=%s err=%s", symbol, e)
        return 0.0
    except Exception:
        _remember_board_failure(symbol, "unexpected")
        logger.exception("[EXIT PRICE] kabu board unexpected failed symbol=%s", symbol)
        return 0.0


def get_latest_exit_price(symbol: str) -> Tuple[float, Dict[str, Any]]:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return 0.0, {}

    # 1) 最優先: 5秒足。EXIT判定の主ソース。
    bar5s = get_five_sec_bar_safe(symbol)
    if bar5s:
        price = extract_price_from_5sec_bar(bar5s)
        if price > 0:
            return price, bar5s

    # 2) PUSH最新tick。
    price = get_push_price_safe(symbol)
    if price > 0:
        return price, bar5s or {}

    # 3) global_data 上の最新価格/tick。
    price = get_global_data_price_safe(symbol)
    if price > 0:
        return price, bar5s or {}

    # 4) 保有ポジション上の current_price 等。
    # board API が遅い環境では、ここを board より先にすることで EXIT loop の遅延を避ける。
    price = get_position_price_fallback(symbol)
    if price > 0:
        logger.warning("[EXIT PRICE] using position fallback price symbol=%s price=%.4f", symbol, price)
        return price, bar5s or {}

    # 5) 最後の保険: kabu board API。
    # timeout/cache/cooldown付きなので、ここで長時間詰まらない。
    price = get_kabu_board_price_safe(symbol, exchange=1)
    if price > 0:
        return price, bar5s or {}

    logger.warning("[EXIT PRICE] unavailable symbol=%s sources=5sec,push,global_data,position,kabu_board", symbol)
    return 0.0, bar5s or {}


__all__ = [
    "safe_float",
    "normalize_symbol",
    "dict_get_any",
    "get_five_sec_bar_safe",
    "extract_price_from_5sec_bar",
    "get_push_price_safe",
    "get_global_data_price_safe",
    "get_kabu_board_price_safe",
    "get_position_price_fallback",
    "get_latest_exit_price",
]
