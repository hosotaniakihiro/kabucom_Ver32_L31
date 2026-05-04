# ============================================================
# File   : trading/push/latest_price_cache.py
# Version: PRODUCTION-STABLE-REV1.0-PUSH-LATEST-PRICE-CACHE
# ------------------------------------------------------------
# Purpose:
#   PUSH受信データから最新価格を抽出し、
#   global_data.latest_price_map / latest_tick_map に保存する。
#
# Why:
#   EXIT 5秒監視では、5秒足OHLCVよりもまず最新価格が必要。
#   exit_price_source.py は global_data.latest_price_map を見るため、
#   PUSH受信時にここへ流す。
#
# Usage:
#   from trading.push.latest_price_cache import update_latest_price_from_push
#
#   update_latest_price_from_push(push_row)
#
# Notes:
#   - 注文は出さない
#   - DB書き込みもしない
#   - メモリキャッシュ更新だけ
#   - どのPUSH形式でも拾えるように列名/キー名を広めに吸収
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from typing import Any, Mapping, Optional

from global_state import global_data

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()


# ============================================================
# helpers
# ============================================================

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)

        x = float(v)

        if math.isnan(x) or math.isinf(x):
            return float(default)

        return x

    except Exception:
        return float(default)


def _normalize_symbol(v: Any) -> str:
    if v is None:
        return ""

    s = str(v).strip()

    if s.endswith(".0"):
        s = s[:-2]

    return s


def _get_any(obj: Any, *names: str, default: Any = None) -> Any:
    """
    dict / object / pandas.Series から候補名で値を取得する。
    """
    if obj is None:
        return default

    # dict / Mapping
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                v = obj.get(name)
                if v is not None:
                    return v

        lower_map = {str(k).lower(): k for k in obj.keys()}
        for name in names:
            real = lower_map.get(str(name).lower())
            if real is not None:
                v = obj.get(real)
                if v is not None:
                    return v

        return default

    # pandas.Series など
    try:
        keys = list(obj.index)  # type: ignore[attr-defined]
        lower_map = {str(k).lower(): k for k in keys}

        for name in names:
            if name in keys:
                v = obj[name]
                if v is not None:
                    return v

        for name in names:
            real = lower_map.get(str(name).lower())
            if real is not None:
                v = obj[real]
                if v is not None:
                    return v
    except Exception:
        pass

    # object attribute
    for name in names:
        try:
            v = getattr(obj, name, None)
            if v is not None:
                return v
        except Exception:
            pass

    return default


def _normalize_datetime(v: Any = None) -> str:
    """
    保存用に YYYY-MM-DD HH:MM:SS 文字列へ正規化する。
    """
    if v is None or str(v).strip() == "":
        return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        import pandas as pd

        ts = pd.to_datetime(v, errors="coerce")

        if pd.isna(ts):
            return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            py_dt = ts.to_pydatetime().replace(tzinfo=None)
            return py_dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ts)

    except Exception:
        try:
            if isinstance(v, dt.datetime):
                return v.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
            return str(v)
        except Exception:
            return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_maps() -> None:
    """
    global_data に価格キャッシュ用 dict を作る。
    """
    for attr in [
        "latest_price_map",
        "latest_prices",
        "current_price_map",
        "latest_tick_map",
        "latest_ticks",
        "push_latest_tick_map",
        "push_latest_price_map",
    ]:
        try:
            current = getattr(global_data, attr, None)
            if not isinstance(current, dict):
                setattr(global_data, attr, {})
        except Exception:
            pass


def extract_symbol_from_push(row: Any) -> str:
    return _normalize_symbol(
        _get_any(
            row,
            "symbol",
            "Symbol",
            "code",
            "Code",
            "銘柄コード",
            "issue_code",
            "IssueCode",
            default="",
        )
    )


def extract_price_from_push(row: Any) -> float:
    """
    PUSH行から現在価格を抽出する。
    """
    price = _safe_float(
        _get_any(
            row,
            "CurrentPrice",
            "current_price",
            "price",
            "last_price",
            "last",
            "close",
            "close_price",
            "Close",
            "現値",
            "現在値",
            default=0.0,
        ),
        0.0,
    )

    return price


def extract_volume_from_push(row: Any) -> float:
    return _safe_float(
        _get_any(
            row,
            "TradingVolume",
            "trading_volume",
            "volume",
            "Volume",
            "出来高",
            default=0.0,
        ),
        0.0,
    )


def extract_name_from_push(row: Any) -> str:
    v = _get_any(
        row,
        "symbolname",
        "symbol_name",
        "SymbolName",
        "name",
        "Name",
        "銘柄名",
        default="",
    )
    return str(v or "")


def extract_datetime_from_push(row: Any) -> str:
    v = _get_any(
        row,
        "datetime",
        "timestamp",
        "CurrentPriceTime",
        "current_price_time",
        "CurrentTime",
        "current_time",
        "time",
        "Time",
        "received_at",
        "updated_at",
        default=None,
    )
    return _normalize_datetime(v)


# ============================================================
# main update
# ============================================================

def update_latest_price_from_push(row: Any, *, source: str = "push") -> bool:
    """
    PUSH受信1件から global_data の最新価格キャッシュを更新する。

    Returns:
        True  = 更新した
        False = symbol/price が不正で更新しなかった
    """
    symbol = extract_symbol_from_push(row)
    price = extract_price_from_push(row)

    if not symbol:
        logger.debug("[PUSH PRICE CACHE] skip empty symbol row=%s", row)
        return False

    if price <= 0:
        logger.debug(
            "[PUSH PRICE CACHE] skip invalid price symbol=%s price=%s",
            symbol,
            price,
        )
        return False

    name = extract_name_from_push(row)
    volume = extract_volume_from_push(row)
    ts = extract_datetime_from_push(row)

    tick = {
        "symbol": symbol,
        "symbolname": name,
        "price": price,
        "current_price": price,
        "CurrentPrice": price,
        "close": price,
        "close_price": price,
        "volume": volume,
        "trading_volume": volume,
        "datetime": ts,
        "received_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
    }

    with _LOCK:
        _ensure_maps()

        try:
            global_data.latest_price_map[symbol] = price
            global_data.latest_prices[symbol] = price
            global_data.current_price_map[symbol] = price
            global_data.push_latest_price_map[symbol] = price

            global_data.latest_tick_map[symbol] = tick
            global_data.latest_ticks[symbol] = tick
            global_data.push_latest_tick_map[symbol] = tick

            # 診断用
            global_data.latest_price_cache_updated_at = dt.datetime.now()
            global_data.latest_price_cache_last_symbol = symbol
            global_data.latest_price_cache_last_price = price

        except Exception:
            logger.exception(
                "[PUSH PRICE CACHE] global_data update failed symbol=%s price=%s",
                symbol,
                price,
            )
            return False

    logger.debug(
        "[PUSH PRICE CACHE] updated symbol=%s price=%.4f volume=%.0f ts=%s",
        symbol,
        price,
        volume,
        ts,
    )

    return True


def update_latest_prices_from_push_rows(rows: Any, *, source: str = "push") -> int:
    """
    PUSH複数行をまとめて更新する。

    rows:
      - list[dict]
      - pandas.DataFrame
      - iterable
    """
    if rows is None:
        return 0

    updated = 0

    try:
        # pandas.DataFrame
        if hasattr(rows, "iterrows"):
            for _, row in rows.iterrows():
                if update_latest_price_from_push(row, source=source):
                    updated += 1
            return updated

        # list / tuple / iterable
        for row in rows:
            if update_latest_price_from_push(row, source=source):
                updated += 1

    except Exception:
        logger.exception("[PUSH PRICE CACHE] batch update failed")
        return updated

    return updated


def get_cached_latest_price(symbol: Any) -> float:
    """
    確認用。global_data.latest_price_map から現在価格を返す。
    """
    symbol = _normalize_symbol(symbol)

    if not symbol:
        return 0.0

    try:
        _ensure_maps()
        return _safe_float(global_data.latest_price_map.get(symbol), 0.0)
    except Exception:
        return 0.0


def get_cached_latest_tick(symbol: Any) -> dict[str, Any]:
    """
    確認用。global_data.latest_tick_map から最新tickを返す。
    """
    symbol = _normalize_symbol(symbol)

    if not symbol:
        return {}

    try:
        _ensure_maps()
        x = global_data.latest_tick_map.get(symbol)
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


__all__ = [
    "extract_symbol_from_push",
    "extract_price_from_push",
    "extract_volume_from_push",
    "extract_name_from_push",
    "extract_datetime_from_push",
    "update_latest_price_from_push",
    "update_latest_prices_from_push_rows",
    "get_cached_latest_price",
    "get_cached_latest_tick",
]