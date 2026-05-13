# ============================================================
# File   : trading/exit/exit_price_source.py
# Version: V2.3-PRODUCTION-EXIT-PRICE-SUMMARY-BROKER-FALLBACK
# ------------------------------------------------------------
# EXIT用の現在値・5秒足取得。
#
# 修正ポイント:
#   - kabu board API が「レジスト数エラー」でもEXIT判定を止めない
#   - 価格取得fallbackを強化
#       1) 5秒足
#       2) PUSH最新tick
#       3) global_data上の最新価格/tick
#       4) 保有ポジション上の current_price / price / avg_price
#       5) kabu Station positions API の CurrentPrice / Price
#       6) summary最新close/close_price/current_price
#       7) kabu board API fallback
#   - board は最後の保険。失敗時はcooldownして連打しない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import time
from typing import Any, Dict, Iterable, Tuple

import requests

from core.global_context.context import global_context as GC
from global_state import global_data

logger = logging.getLogger(__name__)

KABU_API_URL = "http://localhost:18080/kabusapi"

# board API は最後の保険。EXITループを止めないため短くする。
KABU_BOARD_TIMEOUT = (0.25, 0.75)
KABU_BOARD_CACHE_TTL_SEC = 3.0
KABU_BOARD_FAIL_COOLDOWN_SEC = 15.0
KABU_BOARD_GLOBAL_COOLDOWN_SEC = 10.0
KABU_BOARD_GLOBAL_FAIL_THRESHOLD = 5

_board_price_cache: Dict[str, Tuple[float, float]] = {}
_board_fail_until_by_symbol: Dict[str, float] = {}
_board_global_fail_count = 0
_board_global_disabled_until = 0.0
_board_session: requests.Session | None = None


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
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
            if v not in (None, ""):
                return v
    try:
        lower_map = {str(k).lower(): k for k in d.keys()}
        for k in keys:
            real_key = lower_map.get(str(k).lower())
            if real_key is not None:
                v = d.get(real_key)
                if v not in (None, ""):
                    return v
    except Exception:
        pass
    return default


def _is_valid_price(v: Any) -> bool:
    return bool(safe_float(v, 0.0) > 0)


def _extract_datetime_any(d: Dict[str, Any]) -> Any:
    if not isinstance(d, dict):
        return None
    return dict_get_any(
        d,
        "datetime", "timestamp", "time", "Time", "CurrentTime", "current_time",
        "received_at", "updated_at", "created_at", "last_tick_at", "LastUpdateTime",
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
            return dt.datetime.fromisoformat(str(v).replace("Z", "").replace("T", " "))
        except Exception:
            return None


def _is_not_too_stale(ts: dt.datetime | None, *, max_age_seconds: float = 180.0) -> bool:
    if ts is None:
        return True
    try:
        age = abs((dt.datetime.now() - ts).total_seconds())
        return age <= float(max_age_seconds)
    except Exception:
        return True


def _extract_price_from_tick(tick: Any) -> float:
    if tick is None:
        return 0.0
    if isinstance(tick, dict):
        return safe_float(
            dict_get_any(
                tick,
                "price", "current_price", "CurrentPrice", "last_price", "last", "Last",
                "close", "close_price", "Close", "currentPrice", "PresentPrice", "AvgPrice", "avg_price",
                default=0.0,
            ),
            0.0,
        )
    for attr in [
        "price", "current_price", "CurrentPrice", "last_price", "last",
        "close", "close_price", "PresentPrice", "AvgPrice", "avg_price",
    ]:
        try:
            v = getattr(tick, attr, None)
            if _is_valid_price(v):
                return safe_float(v)
        except Exception:
            pass
    return 0.0


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


def _get_from_mapping_attr(owner: Any, attr_name: str, symbol: str) -> float:
    try:
        m = getattr(owner, attr_name, None)
        if not isinstance(m, dict):
            return 0.0
        item = m.get(symbol) or m.get(str(symbol))
        if item is None:
            return 0.0
        if isinstance(item, dict):
            ts = _normalize_dt(_extract_datetime_any(item))
            if not _is_not_too_stale(ts):
                logger.debug("[EXIT PRICE] stale %s.%s symbol=%s ts=%s", type(owner).__name__, attr_name, symbol, ts)
                return 0.0
        return _extract_price_from_tick(item)
    except Exception:
        logger.debug("[EXIT PRICE] %s unavailable symbol=%s", attr_name, symbol, exc_info=True)
        return 0.0


def get_global_data_price_safe(symbol: str) -> float:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return 0.0
    owners = [global_data]
    try:
        owners.extend([GC, getattr(GC, "monitor", None), getattr(GC, "push", None)])
    except Exception:
        pass
    attrs = [
        "latest_price_map", "latest_prices", "current_price_map", "current_prices",
        "push_price_map", "push_prices", "push_latest_price_map", "push_latest_prices",
        "tick_map", "ticks", "latest_tick_map", "latest_ticks",
        "push_tick_map", "push_ticks", "push_latest_tick_map", "push_latest_ticks",
        "symbol_price_map", "price_map", "last_price_map", "last_prices",
    ]
    for owner in owners:
        if owner is None:
            continue
        for attr in attrs:
            price = _get_from_mapping_attr(owner, attr, symbol)
            if price > 0:
                return price
    return 0.0


def _iter_position_containers() -> Iterable[Any]:
    """内部/GCにある建玉コンテナ候補を広く拾う。"""
    owners = [global_data]
    try:
        owners.extend([GC, getattr(GC, "positions", None), getattr(GC, "position", None), getattr(GC, "monitor", None)])
    except Exception:
        pass
    attrs = [
        "open_positions", "positions", "position_map", "open_position_map",
        "credit_positions", "broker_positions", "kabu_positions",
        "position_state_map", "hold_positions", "holdings",
    ]
    for owner in owners:
        if owner is None:
            continue
        for attr in attrs:
            try:
                v = getattr(owner, attr, None)
                if v:
                    yield v
            except Exception:
                pass
        # getter系も試す
        for name in ["get_open_positions", "get_positions", "snapshot", "get_snapshot"]:
            fn = getattr(owner, name, None)
            if callable(fn):
                try:
                    v = fn()
                    if v:
                        yield v
                except Exception:
                    pass


def _find_position_item(container: Any, symbol: str) -> Any:
    if container is None:
        return None
    if isinstance(container, dict):
        item = container.get(symbol) or container.get(str(symbol))
        if item is not None:
            return item
        # list風dict / id keyed dict を走査
        for v in container.values():
            if isinstance(v, dict) and normalize_symbol(dict_get_any(v, "symbol", "Symbol", "code", "Code", default="")) == symbol:
                return v
        return None
    if isinstance(container, (list, tuple)):
        for v in container:
            if isinstance(v, dict) and normalize_symbol(dict_get_any(v, "symbol", "Symbol", "code", "Code", default="")) == symbol:
                return v
            try:
                if normalize_symbol(getattr(v, "symbol", "")) == symbol:
                    return v
            except Exception:
                pass
    return None


def get_position_price_fallback(symbol: str) -> float:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return 0.0
    for container in _iter_position_containers():
        try:
            pos = _find_position_item(container, symbol)
            price = _extract_price_from_tick(pos)
            if price > 0:
                logger.warning("[EXIT PRICE] using internal position price symbol=%s price=%.4f", symbol, price)
                return price
        except Exception:
            logger.debug("[EXIT PRICE] position container fallback failed symbol=%s", symbol, exc_info=True)
    return 0.0


def get_broker_position_price_safe(symbol: str) -> float:
    """kabu Station positions API のCurrentPrice/Priceを使う。

    board API は登録数制限に引っかかることがあるが、positions API は建玉取得なので
    EXIT監視対象にはこちらの方が安全なfallbackになる。
    """
    symbol = normalize_symbol(symbol)
    if not symbol:
        return 0.0
    try:
        from trading.position.kabu_position_reader import read_kabu_open_positions
        positions = read_kabu_open_positions()
        pos = positions.get(symbol) or positions.get(str(symbol)) if isinstance(positions, dict) else None
        if not isinstance(pos, dict):
            return 0.0
        price = safe_float(
            dict_get_any(
                pos,
                "current_price", "CurrentPrice", "price", "Price", "last_price", "close", "avg_price", "entry_price",
                default=0.0,
            ),
            0.0,
        )
        if price > 0:
            logger.warning("[EXIT PRICE] using broker position price symbol=%s price=%.4f", symbol, price)
            return price
    except Exception:
        logger.debug("[EXIT PRICE] broker position price unavailable symbol=%s", symbol, exc_info=True)
    return 0.0


def _extract_price_from_df_like(df: Any, symbol: str) -> float:
    try:
        import pandas as pd
        if df is None or not hasattr(df, "empty") or df.empty:
            return 0.0
        x = df.copy()
        if "symbol" in x.columns:
            x["__symbol_norm"] = x["symbol"].astype(str).str.replace(r"\.0$", "", regex=True)
            x = x[x["__symbol_norm"] == str(symbol)]
        if x.empty:
            return 0.0
        if "datetime" in x.columns:
            x["__dt"] = pd.to_datetime(x["datetime"], errors="coerce")
            x = x.sort_values("__dt")
            last_dt = x["__dt"].dropna().max()
            if last_dt is not None and not pd.isna(last_dt):
                try:
                    if not _is_not_too_stale(last_dt.to_pydatetime().replace(tzinfo=None), max_age_seconds=300.0):
                        logger.debug("[EXIT PRICE] summary stale symbol=%s dt=%s", symbol, last_dt)
                except Exception:
                    pass
        row = x.iloc[-1].to_dict()
        price = safe_float(
            dict_get_any(row, "current_price", "price", "close", "close_price", "CurrentPrice", default=0.0),
            0.0,
        )
        return price if price > 0 else 0.0
    except Exception:
        logger.debug("[EXIT PRICE] df-like summary extraction failed symbol=%s", symbol, exc_info=True)
        return 0.0


def _call_summary_getter(owner: Any, name: str, symbol: str) -> float:
    fn = getattr(owner, name, None)
    if not callable(fn):
        return 0.0
    # 呼び方が環境で違うため、複数パターンを安全に試す
    call_patterns = [
        ((1,), {"source": "push"}),
        ((), {"tf": 1, "source": "push"}),
        ((), {"interval": 1, "source": "push"}),
        ((1,), {}),
        ((), {"tf": 1}),
        ((), {"interval": 1}),
        ((), {}),
    ]
    for args, kwargs in call_patterns:
        try:
            ret = fn(*args, **kwargs)
            price = _extract_price_from_df_like(ret, symbol)
            if price > 0:
                return price
        except TypeError:
            continue
        except Exception:
            continue
    return 0.0


def get_summary_close_price_safe(symbol: str) -> float:
    symbol = normalize_symbol(symbol)
    if not symbol:
        return 0.0
    owners = [global_data, GC]
    names = [
        "get_merged_summary", "get_summary", "get_latest_summary", "get_summary_df",
        "get_push_summary", "get_push_summary_df", "get_completed_summary",
    ]
    for owner in owners:
        if owner is None:
            continue
        for name in names:
            price = _call_summary_getter(owner, name, symbol)
            if price > 0:
                logger.warning("[EXIT PRICE] using summary latest close symbol=%s price=%.4f source=%s.%s", symbol, price, type(owner).__name__, name)
                return price
    # DataFrame属性として保持されているケース
    for owner in owners:
        if owner is None:
            continue
        for attr in [
            "merged_summary_1min", "summary_1min", "push_summary_1min", "df_summary_1min",
            "stock_summary_1min", "summary_df", "push_summary_df",
        ]:
            try:
                price = _extract_price_from_df_like(getattr(owner, attr, None), symbol)
                if price > 0:
                    logger.warning("[EXIT PRICE] using summary attr latest close symbol=%s price=%.4f attr=%s", symbol, price, attr)
                    return price
            except Exception:
                pass
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

    # 1) 最優先: 5秒足。
    bar5s = get_five_sec_bar_safe(symbol)
    if bar5s:
        price = extract_price_from_5sec_bar(bar5s)
        if price > 0:
            return price, bar5s

    # 2) PUSH最新tick。
    price = get_push_price_safe(symbol)
    if price > 0:
        return price, bar5s or {}

    # 3) global_data / GC 上の最新価格/tick。
    price = get_global_data_price_safe(symbol)
    if price > 0:
        return price, bar5s or {}

    # 4) 内部建玉上のcurrent_price/price/avg_price。
    price = get_position_price_fallback(symbol)
    if price > 0:
        return price, bar5s or {}

    # 5) kabu Station positions API。boardより先に使う。
    price = get_broker_position_price_safe(symbol)
    if price > 0:
        return price, bar5s or {}

    # 6) summary 最新 close。PUSH/boardが取れない場合の最後の実用fallback。
    price = get_summary_close_price_safe(symbol)
    if price > 0:
        return price, bar5s or {}

    # 7) 最後の保険: kabu board API。
    price = get_kabu_board_price_safe(symbol, exchange=1)
    if price > 0:
        return price, bar5s or {}

    logger.warning("[EXIT PRICE] unavailable symbol=%s sources=5sec,push,global_data,position,broker_position,summary,kabu_board", symbol)
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
    "get_broker_position_price_safe",
    "get_summary_close_price_safe",
    "get_kabu_board_price_safe",
    "get_latest_exit_price",
]
