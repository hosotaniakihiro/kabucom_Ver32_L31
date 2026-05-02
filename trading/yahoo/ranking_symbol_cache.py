# ============================================================
# File   : trading/yahoo/ranking_symbol_cache.py
# Version: PRODUCTION-STABLE-REV1.0-RANKING-SYMBOL-CACHE
# ------------------------------------------------------------
# 【概要】
#   ランキングに入った銘柄を symbol 単位で一意化して管理する。
#
# 【目的】
#   - 違うランキングに複数回入った銘柄を重複させない
#   - Yahoo ダウンロード対象を1銘柄1回にする
#   - global_data 上で高速参照する
#
# 【重要】
#   - DBが正本
#   - global_data は実行中キャッシュ
# ============================================================

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable

logger = logging.getLogger(__name__)

try:
    from core.global_context.context import global_data
except Exception:
    try:
        from global_state import global_data  # type: ignore
    except Exception:
        global_data = None


def normalize_symbol(symbol: object) -> str:
    if symbol is None:
        return ""

    s = str(symbol).strip()

    if s.endswith(".0"):
        s = s[:-2]

    return s


def normalize_symbols(symbols: Iterable[object]) -> set[str]:
    out: set[str] = set()

    for s in symbols or []:
        ns = normalize_symbol(s)
        if ns:
            out.add(ns)

    return out


def ensure_ranking_symbol_cache() -> None:
    if global_data is None:
        return

    if not hasattr(global_data, "ranking_today_symbols_raw"):
        global_data.ranking_today_symbols_raw = set()

    if not hasattr(global_data, "ranking_today_symbols_filtered"):
        global_data.ranking_today_symbols_filtered = set()

    if not hasattr(global_data, "yahoo_backfilled_symbols"):
        global_data.yahoo_backfilled_symbols = set()

    if not hasattr(global_data, "yahoo_backfill_pending_symbols"):
        global_data.yahoo_backfill_pending_symbols = set()

    if not hasattr(global_data, "yahoo_backfill_failed_symbols"):
        global_data.yahoo_backfill_failed_symbols = set()


def add_ranking_symbols(
    symbols: Iterable[object],
    *,
    filtered: bool = True,
) -> set[str]:
    """
    ランキングに出現した銘柄を global_data に追加する。
    setなので重複しない。
    """
    ensure_ranking_symbol_cache()

    normalized = normalize_symbols(symbols)

    if global_data is None:
        return normalized

    try:
        global_data.ranking_today_symbols_raw.update(normalized)

        if filtered:
            global_data.ranking_today_symbols_filtered.update(normalized)

        global_data.ranking_today_symbols_updated_at = datetime.now()

        logger.info(
            "[RANKING SYMBOL CACHE] added symbols=%s total_raw=%s total_filtered=%s",
            len(normalized),
            len(global_data.ranking_today_symbols_raw),
            len(global_data.ranking_today_symbols_filtered),
        )

    except Exception:
        logger.exception("[RANKING SYMBOL CACHE] add failed")

    return normalized


def set_ranking_symbols(
    symbols: Iterable[object],
    *,
    filtered: bool = True,
) -> set[str]:
    """
    DBから復元した一意化済み銘柄で cache を置き換える。
    """
    ensure_ranking_symbol_cache()

    normalized = normalize_symbols(symbols)

    if global_data is None:
        return normalized

    try:
        global_data.ranking_today_symbols_raw = set(normalized)

        if filtered:
            global_data.ranking_today_symbols_filtered = set(normalized)

        global_data.ranking_today_symbols_updated_at = datetime.now()

        logger.info(
            "[RANKING SYMBOL CACHE] set symbols=%s",
            len(normalized),
        )

    except Exception:
        logger.exception("[RANKING SYMBOL CACHE] set failed")

    return normalized


def get_ranking_symbols(*, filtered: bool = True) -> set[str]:
    ensure_ranking_symbol_cache()

    if global_data is None:
        return set()

    try:
        if filtered:
            return normalize_symbols(getattr(global_data, "ranking_today_symbols_filtered", set()))

        return normalize_symbols(getattr(global_data, "ranking_today_symbols_raw", set()))

    except Exception:
        logger.exception("[RANKING SYMBOL CACHE] get failed")
        return set()


def get_yahoo_backfilled_symbols() -> set[str]:
    ensure_ranking_symbol_cache()

    if global_data is None:
        return set()

    return normalize_symbols(getattr(global_data, "yahoo_backfilled_symbols", set()))


def get_yahoo_download_target_symbols() -> set[str]:
    """
    Yahooから当日分をまだ取得していない銘柄だけ返す。

    ここが重複防止の中心。
    """
    ranking_symbols = get_ranking_symbols(filtered=True)
    done_symbols = get_yahoo_backfilled_symbols()

    targets = ranking_symbols - done_symbols

    if global_data is not None:
        try:
            global_data.yahoo_backfill_pending_symbols = set(targets)
            global_data.yahoo_backfill_pending_updated_at = datetime.now()
        except Exception:
            pass

    logger.info(
        "[RANKING SYMBOL CACHE] yahoo targets ranking=%s done=%s targets=%s",
        len(ranking_symbols),
        len(done_symbols),
        len(targets),
    )

    return targets


def mark_yahoo_backfilled(symbols: Iterable[object]) -> None:
    """
    Yahoo当日分を取得済みにする。
    """
    ensure_ranking_symbol_cache()

    normalized = normalize_symbols(symbols)

    if global_data is None:
        return

    try:
        global_data.yahoo_backfilled_symbols.update(normalized)

        pending = normalize_symbols(getattr(global_data, "yahoo_backfill_pending_symbols", set()))
        pending -= normalized
        global_data.yahoo_backfill_pending_symbols = pending

        global_data.yahoo_backfill_updated_at = datetime.now()

        logger.info(
            "[RANKING SYMBOL CACHE] yahoo backfilled added=%s total_done=%s pending=%s",
            len(normalized),
            len(global_data.yahoo_backfilled_symbols),
            len(global_data.yahoo_backfill_pending_symbols),
        )

    except Exception:
        logger.exception("[RANKING SYMBOL CACHE] mark backfilled failed")


def mark_yahoo_backfill_failed(symbols: Iterable[object]) -> None:
    ensure_ranking_symbol_cache()

    normalized = normalize_symbols(symbols)

    if global_data is None:
        return

    try:
        global_data.yahoo_backfill_failed_symbols.update(normalized)
        global_data.yahoo_backfill_failed_updated_at = datetime.now()

        logger.warning(
            "[RANKING SYMBOL CACHE] yahoo backfill failed added=%s total_failed=%s",
            len(normalized),
            len(global_data.yahoo_backfill_failed_symbols),
        )

    except Exception:
        logger.exception("[RANKING SYMBOL CACHE] mark failed failed")


def clear_intraday_cache() -> None:
    """
    日付が変わったとき用。
    """
    ensure_ranking_symbol_cache()

    if global_data is None:
        return

    global_data.ranking_today_symbols_raw = set()
    global_data.ranking_today_symbols_filtered = set()
    global_data.yahoo_backfilled_symbols = set()
    global_data.yahoo_backfill_pending_symbols = set()
    global_data.yahoo_backfill_failed_symbols = set()
    global_data.ranking_today_symbols_updated_at = datetime.now()

    logger.info("[RANKING SYMBOL CACHE] cleared")