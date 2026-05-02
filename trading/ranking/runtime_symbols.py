# ============================================================
# File   : trading/ranking/runtime_symbols.py
# Version: Ver2.1-PRODUCTION-RANKING-RUNTIME-SYMBOLS-YAHOO-30MIN
# ------------------------------------------------------------
# ✔ 当日ランキング銘柄の runtime cache
# ✔ global_data 共有
# ✔ filtered / raw 両対応
# ✔ symbol last_seen 管理
# ✔ Yahoo 用 30分アクティブ銘柄取得
# ✔ backfilled / failed 管理
# ✔ 日付切替対応
# ✔ scheduler安全
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Iterable, Any

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# optional global_data
# ------------------------------------------------------------
try:
    from core.global_context.context import global_data
except Exception:
    try:
        from global_state import global_data  # type: ignore
    except Exception:
        global_data = None


_lock = threading.RLock()

_current_trade_date: str | None = None

_ranking_today_symbols: set[str] = set()
_ranking_today_filtered_symbols: set[str] = set()

# symbol -> last seen datetime
_ranking_symbol_last_seen_map: dict[str, dt.datetime] = {}

# yahoo status cache
_yahoo_backfilled_symbols: set[str] = set()
_yahoo_backfill_failed_symbols: set[str] = set()


# ============================================================
# helpers
# ============================================================

def _normalize_trade_date(target_date: Any = None) -> str:
    if target_date is None:
        return dt.date.today().strftime("%Y%m%d")

    if isinstance(target_date, dt.datetime):
        return target_date.date().strftime("%Y%m%d")

    if isinstance(target_date, dt.date):
        return target_date.strftime("%Y%m%d")

    s = str(target_date).strip()
    if not s:
        return dt.date.today().strftime("%Y%m%d")

    return s.replace("-", "").replace("/", "")


def _normalize_symbol(symbol: Any) -> str:
    if symbol is None:
        return ""

    s = str(symbol).strip()
    if not s:
        return ""

    s = s.replace(".T", "").replace(".JP", "").strip()
    if s.endswith(".0"):
        s = s[:-2]

    return s.strip()


def normalize_symbols(symbols: Iterable[Any]) -> set[str]:
    out: set[str] = set()
    for s in symbols or []:
        ns = _normalize_symbol(s)
        if ns:
            out.add(ns)
    return out


def _safe_set_global_attr(name: str, value) -> None:
    try:
        if global_data is not None:
            setattr(global_data, name, value)
    except Exception:
        logger.debug("[RANKING RUNTIME SYMBOLS] setattr failed name=%s", name, exc_info=True)


def _sync_global_state() -> None:
    try:
        _safe_set_global_attr("ranking_today_symbols", sorted(_ranking_today_symbols))
        _safe_set_global_attr("ranking_today_filtered_symbols", sorted(_ranking_today_filtered_symbols))
        _safe_set_global_attr(
            "ranking_symbol_last_seen_map",
            dict(_ranking_symbol_last_seen_map),
        )
        _safe_set_global_attr("yahoo_backfilled_symbols", sorted(_yahoo_backfilled_symbols))
        _safe_set_global_attr("yahoo_backfill_failed_symbols", sorted(_yahoo_backfill_failed_symbols))
        _safe_set_global_attr("ranking_runtime_trade_date", _current_trade_date)
    except Exception:
        logger.debug("[RANKING RUNTIME SYMBOLS] sync global state failed", exc_info=True)


# ============================================================
# cache lifecycle
# ============================================================

def ensure_ranking_symbol_cache(target_date=None) -> None:
    global _current_trade_date

    trade_date = _normalize_trade_date(target_date)

    with _lock:
        if _current_trade_date == trade_date:
            return

        _ranking_today_symbols.clear()
        _ranking_today_filtered_symbols.clear()
        _ranking_symbol_last_seen_map.clear()
        _yahoo_backfilled_symbols.clear()
        _yahoo_backfill_failed_symbols.clear()

        _current_trade_date = trade_date
        _sync_global_state()

        logger.info(
            "[RANKING RUNTIME SYMBOLS] cache reset trade_date=%s",
            trade_date,
        )


def clear_intraday_cache(target_date=None) -> None:
    trade_date = _normalize_trade_date(target_date)

    with _lock:
        ensure_ranking_symbol_cache(target_date=trade_date)


# ============================================================
# add / update
# ============================================================

def add_ranking_symbols(
    symbols: Iterable[Any],
    *,
    filtered: bool = False,
    target_date=None,
    seen_at: dt.datetime | None = None,
) -> set[str]:
    ensure_ranking_symbol_cache(target_date=target_date)

    normalized = normalize_symbols(symbols)
    if not normalized:
        return set()

    now_dt = seen_at or dt.datetime.now()

    with _lock:
        for sym in normalized:
            _ranking_today_symbols.add(sym)
            _ranking_symbol_last_seen_map[sym] = now_dt

        if filtered:
            for sym in normalized:
                _ranking_today_filtered_symbols.add(sym)

        _sync_global_state()

    logger.info(
        "[RANKING RUNTIME SYMBOLS] added symbols=%d filtered=%s seen_at=%s",
        len(normalized),
        filtered,
        now_dt,
    )
    return set(normalized)


def add_filtered_ranking_symbols(
    symbols: Iterable[Any],
    *,
    target_date=None,
    seen_at: dt.datetime | None = None,
) -> set[str]:
    ensure_ranking_symbol_cache(target_date=target_date)

    normalized = normalize_symbols(symbols)
    if not normalized:
        return set()

    now_dt = seen_at or dt.datetime.now()

    with _lock:
        for sym in normalized:
            _ranking_today_symbols.add(sym)
            _ranking_today_filtered_symbols.add(sym)
            _ranking_symbol_last_seen_map[sym] = now_dt

        _sync_global_state()

    logger.info(
        "[RANKING RUNTIME SYMBOLS] added filtered symbols=%d seen_at=%s",
        len(normalized),
        now_dt,
    )
    return set(normalized)


def set_ranking_symbols(
    symbols: Iterable[Any],
    *,
    filtered: bool = True,
    target_date=None,
    seen_at: dt.datetime | None = None,
) -> set[str]:
    if filtered:
        return add_filtered_ranking_symbols(
            symbols,
            target_date=target_date,
            seen_at=seen_at,
        )
    return add_ranking_symbols(
        symbols,
        filtered=False,
        target_date=target_date,
        seen_at=seen_at,
    )


# ============================================================
# readers
# ============================================================

def get_ranking_symbols_all() -> set[str]:
    with _lock:
        return set(_ranking_today_symbols)


def get_ranking_symbols_filtered() -> set[str]:
    with _lock:
        if _ranking_today_filtered_symbols:
            return set(_ranking_today_filtered_symbols)
        return set(_ranking_today_symbols)


def get_ranking_symbol_cache_snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "trade_date": _current_trade_date,
            "raw_count": len(_ranking_today_symbols),
            "filtered_count": len(_ranking_today_filtered_symbols),
            "raw_symbols": sorted(_ranking_today_symbols),
            "filtered_symbols": sorted(_ranking_today_filtered_symbols),
            "last_seen_count": len(_ranking_symbol_last_seen_map),
            "yahoo_done_count": len(_yahoo_backfilled_symbols),
            "yahoo_failed_count": len(_yahoo_backfill_failed_symbols),
        }


# ============================================================
# yahoo active / prune
# ============================================================

def prune_stale_yahoo_symbols(
    *,
    timeout_minutes: int = 30,
    now_dt: dt.datetime | None = None,
) -> int:
    now_dt = now_dt or dt.datetime.now()
    cutoff = now_dt - dt.timedelta(minutes=timeout_minutes)

    removed = 0

    with _lock:
        stale = [
            sym
            for sym, seen_at in _ranking_symbol_last_seen_map.items()
            if seen_at < cutoff
        ]

        for sym in stale:
            # today履歴は消さない
            if sym in _ranking_today_filtered_symbols:
                _ranking_today_filtered_symbols.discard(sym)
                removed += 1

        _sync_global_state()

    if removed:
        logger.info(
            "[RANKING RUNTIME SYMBOLS] pruned stale yahoo symbols=%d cutoff=%s",
            removed,
            cutoff,
        )

    return removed


def get_yahoo_active_symbols(
    *,
    timeout_minutes: int = 30,
    now_dt: dt.datetime | None = None,
    filtered_only: bool = True,
    exclude_backfilled: bool = False,
    max_symbols: int | None = None,
) -> set[str]:
    now_dt = now_dt or dt.datetime.now()
    cutoff = now_dt - dt.timedelta(minutes=timeout_minutes)

    with _lock:
        base_symbols = (
            set(_ranking_today_filtered_symbols)
            if filtered_only and _ranking_today_filtered_symbols
            else set(_ranking_today_symbols)
        )

        active = {
            sym
            for sym in base_symbols
            if _ranking_symbol_last_seen_map.get(sym) is not None
            and _ranking_symbol_last_seen_map[sym] >= cutoff
        }

        if exclude_backfilled:
            active -= _yahoo_backfilled_symbols

    if max_symbols:
        try:
            n = int(max_symbols)
            if n > 0 and len(active) > n:
                active = set(sorted(active)[:n])
        except Exception:
            pass

    logger.info(
        "[RANKING RUNTIME SYMBOLS] yahoo active symbols=%d timeout_minutes=%d cutoff=%s exclude_backfilled=%s",
        len(active),
        timeout_minutes,
        cutoff,
        exclude_backfilled,
    )
    return active


# ============================================================
# yahoo status
# ============================================================

def mark_yahoo_backfilled(
    symbols: Iterable[Any],
    *,
    target_date=None,
) -> int:
    ensure_ranking_symbol_cache(target_date=target_date)

    normalized = normalize_symbols(symbols)
    if not normalized:
        return 0

    with _lock:
        for sym in normalized:
            _yahoo_backfilled_symbols.add(sym)
            _yahoo_backfill_failed_symbols.discard(sym)

        _sync_global_state()

    logger.info(
        "[RANKING RUNTIME SYMBOLS] yahoo backfilled added=%d total_done=%d pending=%d",
        len(normalized),
        len(_yahoo_backfilled_symbols),
        max(len(get_ranking_symbols_filtered()) - len(_yahoo_backfilled_symbols), 0),
    )
    return len(normalized)


def mark_yahoo_backfill_failed(
    symbols: Iterable[Any],
    *,
    target_date=None,
) -> int:
    ensure_ranking_symbol_cache(target_date=target_date)

    normalized = normalize_symbols(symbols)
    if not normalized:
        return 0

    with _lock:
        for sym in normalized:
            _yahoo_backfill_failed_symbols.add(sym)

        _sync_global_state()

    logger.warning(
        "[RANKING RUNTIME SYMBOLS] yahoo backfill failed added=%d total_failed=%d",
        len(normalized),
        len(_yahoo_backfill_failed_symbols),
    )
    return len(normalized)


# ============================================================
# logs
# ============================================================

def log_ranking_symbol_cache_snapshot(prefix: str = "[RANKING RUNTIME SYMBOLS]") -> None:
    snap = get_ranking_symbol_cache_snapshot()
    logger.info(
        "%s trade_date=%s raw=%d filtered=%d done=%d failed=%d",
        prefix,
        snap["trade_date"],
        snap["raw_count"],
        snap["filtered_count"],
        snap["yahoo_done_count"],
        snap["yahoo_failed_count"],
    )


__all__ = [
    "ensure_ranking_symbol_cache",
    "clear_intraday_cache",
    "normalize_symbols",
    "add_ranking_symbols",
    "add_filtered_ranking_symbols",
    "set_ranking_symbols",
    "get_ranking_symbols_all",
    "get_ranking_symbols_filtered",
    "get_ranking_symbol_cache_snapshot",
    "prune_stale_yahoo_symbols",
    "get_yahoo_active_symbols",
    "mark_yahoo_backfilled",
    "mark_yahoo_backfill_failed",
    "log_ranking_symbol_cache_snapshot",
]