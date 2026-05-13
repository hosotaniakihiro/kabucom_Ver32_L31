# ============================================================
# File   : trading/ranking/active_symbols/manager.py
# Version: Ver1.3-ACTIVE-SYMBOLS-DAILY-WATCHLIST-FIRST-SUPPLEMENT
# ------------------------------------------------------------
# 目的:
#   PUSH監視候補を原則100銘柄にする。
#
# 重要修正:
#   - 寄前SBIが96銘柄など100未満の場合でも、保有銘柄を加えた後、
#     不足分を安全な補充候補から追加する。
#   - 補充候補は daily_watchlist を最優先する。
#   - これまでの不足時補充は allowed_universe に縛られていたため、
#     寄前SBI universe 自体が100未満だと補充不能だった。
#   - 寄前モードでは、価格列が無いことを前提に、symbol_flags適格銘柄からも補充する。
#   - 通常場中モードでは、従来どおり流動性条件を守る。
#   - 保有中銘柄 protected は必ず残す。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from typing import Iterable, List, Set, Tuple

from global_state import global_data
from .config import (
    ACTIVE_REQUIRE_SYMBOL_FLAGS,
    ENABLE_PREMARKET_SBI,
    ENABLE_LIQUIDITY_FILTER,
    MAX_ACTIVE_SYMBOLS,
    MIN_PRICE,
    MIN_TICK_COUNT,
    MIN_TRADING_VALUE,
    MIN_VOLUME,
    TARGET_ACTIVE_SYMBOLS,
    USE_PREMARKET_WHEN_TODAY_RANKING_EMPTY,
)
from .global_helpers import get_global_attr, set_global_attr
from .liquidity import final_guard_min_price, filter_liquid_symbols, is_liquid_symbol
from .normalize import dedupe_keep_order, normalize_symbol, now as now_dt, to_float
from .premarket_source import filter_premarket_min_price, is_premarket_time, load_premarket_symbols
from .protected import get_protected_symbols
from .ranking_source import (
    build_liquidity_map,
    extract_volume_speed_symbols,
    today_ranking_available,
    today_ranking_symbols,
    update_last_seen_from_ranking,
)
from .reflect import reflect_active_to_global
from .symbol_flags import filter_by_symbol_flags, load_symbol_flags_eligible_symbols

logger = logging.getLogger(__name__)


def _publish_symbol_flags_cache(eligible_symbols: Set[str], flag_info: dict) -> None:
    """
    起動時 / active symbol 更新時に読み込んだ symbol_flags 情報を
    entry_controller / SELL_CREDIT_GUARD から参照できるように global_data へ保持する。
    """
    try:
        global_data.symbol_flags_eligible_symbols = set(eligible_symbols or set())
        global_data.symbol_flags_info_map = dict(flag_info or {})
        global_data.symbol_flags_loaded_at = dt.datetime.now()
        logger.info(
            "[ACTIVE FLAGS] global cache published eligible=%d info=%d",
            len(global_data.symbol_flags_eligible_symbols),
            len(global_data.symbol_flags_info_map),
        )
    except Exception:
        logger.exception("[ACTIVE FLAGS] global cache publish failed")


def _build_today_ranking_candidates(
    *,
    now: dt.datetime,
    eligible_symbols: Set[str],
    protected: Set[str],
    liquidity_map: dict[str, dict[str, float]],
) -> Tuple[List[str], Set[str], str]:
    today_symbols = today_ranking_symbols(now=now)
    universe = set(today_symbols)
    logger.info("[ACTIVE SOURCE] today_ranking symbols=%d head=%s", len(today_symbols), today_symbols[:20])
    candidates = filter_by_symbol_flags(today_symbols, eligible_symbols=eligible_symbols, context="today_ranking")
    candidates = filter_liquid_symbols(
        candidates,
        protected=protected,
        liquidity_map=liquidity_map,
        context="today_ranking",
        require_info=True,
    )
    return candidates, universe, "today_ranking"


def _build_premarket_candidates(
    *,
    now: dt.datetime,
    eligible_symbols: Set[str],
    protected: Set[str],
) -> Tuple[List[str], Set[str], str]:
    premarket_symbols = load_premarket_symbols(now=now)
    universe = set(premarket_symbols)
    logger.info("[ACTIVE SOURCE] premarket_sbi symbols=%d head=%s", len(premarket_symbols), premarket_symbols[:20])
    candidates = filter_by_symbol_flags(premarket_symbols, eligible_symbols=eligible_symbols, context="premarket_sbi")
    candidates = filter_premarket_min_price(candidates, now=now, protected=protected)
    return candidates, universe, "premarket_sbi"


def _iter_fallback_sources(
    prev_active: Iterable[str],
    hot_symbols: Iterable[str],
    primary_candidates: Iterable[str],
) -> Iterable[str]:
    for s in primary_candidates:
        yield str(s)
    for s in hot_symbols:
        yield str(s)
    for s in prev_active:
        yield str(s)
    for attr in (
        "candidate_push_symbols",
        "push_candidate_symbols",
        "push_symbols_100",
        "monitor_symbols",
    ):
        vals = get_global_attr(attr, [])
        for s in vals or []:
            yield str(s)


def _flag_bool(v) -> bool:
    try:
        if isinstance(v, bool):
            return bool(v)
        if v is None:
            return False
        s = str(v).strip().lower()
        return s in {"1", "true", "yes", "y", "on", "ok", "可能", "可"}
    except Exception:
        return False


def _load_daily_watchlist_symbols(limit: int = 200) -> List[str]:
    """
    optional_data.db の daily_watchlist から補充候補を取得する。

    補充品質を上げるため、単なる symbol_flags 全体より先に使う。
    build_daily_watchlist.py が保存した直近 date の100銘柄を優先する。
    """
    try:
        from config.paths import get_path

        db_path = get_path("optional_db")
    except Exception:
        logger.debug("[ACTIVE SUPPLEMENT] optional_db path resolve failed", exc_info=True)
        return []

    try:
        if not db_path or not db_path.exists():
            logger.info("[ACTIVE SUPPLEMENT] daily_watchlist db not found path=%s", db_path)
            return []
    except Exception:
        return []

    try:
        with sqlite3.connect(str(db_path), timeout=3.0) as con:
            cur = con.cursor()
            try:
                row = cur.execute("SELECT MAX(date) FROM daily_watchlist").fetchone()
            except sqlite3.Error:
                logger.info("[ACTIVE SUPPLEMENT] daily_watchlist table not ready path=%s", db_path)
                return []

            latest_date = row[0] if row else None
            if not latest_date:
                logger.info("[ACTIVE SUPPLEMENT] daily_watchlist empty path=%s", db_path)
                return []

            rows = cur.execute(
                """
                SELECT symbol
                FROM daily_watchlist
                WHERE date = ?
                ORDER BY
                    COALESCE(buy_score, 0) + COALESCE(sell_score, 0) DESC,
                    COALESCE(buy_score, 0) DESC,
                    COALESCE(sell_score, 0) DESC,
                    symbol ASC
                LIMIT ?
                """,
                (latest_date, int(limit)),
            ).fetchall()

        symbols = dedupe_keep_order(
            normalize_symbol(r[0]) for r in rows if r and r[0] is not None
        )
        logger.info(
            "[ACTIVE SUPPLEMENT] daily_watchlist loaded date=%s count=%d head=%s db=%s",
            latest_date,
            len(symbols),
            symbols[:20],
            db_path,
        )
        return symbols
    except sqlite3.OperationalError as e:
        logger.warning("[ACTIVE SUPPLEMENT] daily_watchlist read skipped sqlite err=%s path=%s", e, db_path)
        return []
    except Exception:
        logger.exception("[ACTIVE SUPPLEMENT] daily_watchlist read failed path=%s", db_path)
        return []


def _supplement_sort_key(
    sym: str,
    *,
    protected: Set[str],
    liquidity_map: dict[str, dict[str, float]],
    flag_info: dict,
):
    """
    補充候補の優先度。
    ランキング情報があれば売買代金/出来高/TICKを優先。
    寄前でランキング情報が無い場合は symbol_flags の ats/buy/sell/short を優先。
    """
    info = liquidity_map.get(sym, {}) or {}
    flags = flag_info.get(sym, {}) if isinstance(flag_info, dict) else {}
    if not isinstance(flags, dict):
        flags = {}

    is_protected = 1 if sym in protected else 0
    value = to_float(info.get("trading_value"), 0.0)
    volume = to_float(info.get("trading_volume"), 0.0)
    tick = to_float(info.get("tick_count"), 0.0)
    price = to_float(info.get("current_price"), 0.0)

    ats_ok = 1 if _flag_bool(flags.get("ats_ok")) else 0
    buy_target = 1 if _flag_bool(flags.get("buy_target")) else 0
    sell_target = 1 if _flag_bool(flags.get("sell_target")) else 0
    short_ok = 1 if _flag_bool(flags.get("short_ok")) else 0
    is_attention = 1 if _flag_bool(flags.get("is_attention")) else 0

    try:
        last_seen = global_data.symbol_last_seen.get(sym, dt.datetime.min)
    except Exception:
        last_seen = dt.datetime.min
    if last_seen is None:
        last_seen = dt.datetime.min

    # is_attention は少し下げるが完全除外はしない。
    return (
        is_protected,
        value,
        volume,
        tick,
        price,
        ats_ok,
        buy_target + sell_target + short_ok,
        -is_attention,
        last_seen,
        sym,
    )


def _iter_target_supplement_sources(
    *,
    prev_active: Iterable[str],
    hot_symbols: Iterable[str],
    primary_candidates: Iterable[str],
    allowed_universe: Set[str],
    eligible_symbols: Set[str],
    protected: Set[str],
    liquidity_map: dict[str, dict[str, float]],
    flag_info: dict,
) -> Iterable[str]:
    """
    TARGET_ACTIVE_SYMBOLS 未満のときの補充元。
    優先順:
      1. 既存候補 / 直近候補
      2. optional_data.db の daily_watchlist
      3. global_data 上の watchlist 候補
      4. allowed_universe 内の漏れ
      5. 最終手段として symbol_flags 適格銘柄
    """
    # まず既存の候補・直近候補。
    yield from _iter_fallback_sources(prev_active, hot_symbols, primary_candidates)

    # daily_watchlist DBを最優先補充元にする。
    for s in _load_daily_watchlist_symbols(limit=max(TARGET_ACTIVE_SYMBOLS * 2, 200)):
        yield str(s)

    # 起動済みプロセスに載っている可能性がある候補群。
    for attr in (
        "daily_watchlist_symbols",
        "daily_watchlist",
        "watchlist_symbols",
        "optional_watchlist_symbols",
        "ranking_candidate_symbols",
        "last_ranking_symbols",
        "latest_ranking_symbols",
        "active_symbols",
        "symbols_active",
    ):
        vals = get_global_attr(attr, [])
        if isinstance(vals, dict):
            vals = vals.keys()
        for s in vals or []:
            yield str(s)

    # universe 内でまだ漏れているもの。
    for s in sorted(allowed_universe):
        yield str(s)

    # 最終手段: symbol_flags 適格銘柄から、流動性/flags/last_seenで優先順に補充。
    eligible_sorted = sorted(
        [s for s in eligible_symbols if s not in protected],
        key=lambda x: _supplement_sort_key(
            x,
            protected=protected,
            liquidity_map=liquidity_map,
            flag_info=flag_info,
        ),
        reverse=True,
    )
    for s in eligible_sorted:
        yield str(s)


def _supplement_active_to_target(
    active: Set[str],
    *,
    target: int,
    premarket_mode: bool,
    allowed_universe: Set[str],
    eligible_symbols: Set[str],
    flag_info: dict,
    protected: Set[str],
    liquidity_map: dict[str, dict[str, float]],
    prev_active: Iterable[str],
    hot_symbols: Iterable[str],
    primary_candidates: Iterable[str],
) -> tuple[Set[str], dict]:
    """
    active が target 未満なら不足分を補充する。

    premarket_mode:
      寄前SBI CSVに価格列が無いことがあるため、allowed_universe外でも
      symbol_flags適格なら補充を許可する。

    通常時間:
      allowed_universe外は原則避け、流動性条件を守る。
    """
    before = len(active)
    added: list[str] = []
    skipped_existing = 0
    skipped_flags = 0
    skipped_universe = 0
    skipped_liquidity = 0

    if before >= target:
        return active, {
            "before": before,
            "after": before,
            "added": [],
            "skipped_existing": 0,
            "skipped_flags": 0,
            "skipped_universe": 0,
            "skipped_liquidity": 0,
        }

    for sym in _iter_target_supplement_sources(
        prev_active=prev_active,
        hot_symbols=hot_symbols,
        primary_candidates=primary_candidates,
        allowed_universe=allowed_universe,
        eligible_symbols=eligible_symbols,
        protected=protected,
        liquidity_map=liquidity_map,
        flag_info=flag_info,
    ):
        ns = normalize_symbol(sym)
        if not ns:
            continue
        if ns in active:
            skipped_existing += 1
            continue

        if ACTIVE_REQUIRE_SYMBOL_FLAGS and ns not in eligible_symbols and ns not in protected:
            skipped_flags += 1
            continue

        if not premarket_mode and ns not in allowed_universe and ns not in protected:
            skipped_universe += 1
            continue

        if not premarket_mode:
            if not is_liquid_symbol(ns, liquidity_map=liquidity_map, protected=protected, require_info=True):
                skipped_liquidity += 1
                continue

        active.add(ns)
        added.append(ns)
        if len(active) >= target:
            break

    diag = {
        "before": before,
        "after": len(active),
        "added": added,
        "skipped_existing": skipped_existing,
        "skipped_flags": skipped_flags,
        "skipped_universe": skipped_universe,
        "skipped_liquidity": skipped_liquidity,
        "premarket_mode": premarket_mode,
        "target": target,
    }

    if added:
        logger.warning(
            "[ACTIVE SUPPLEMENT] added to target before=%s after=%s target=%s premarket=%s added=%s skipped_existing=%s skipped_flags=%s skipped_universe=%s skipped_liquidity=%s",
            before,
            len(active),
            target,
            premarket_mode,
            added,
            skipped_existing,
            skipped_flags,
            skipped_universe,
            skipped_liquidity,
        )
    else:
        logger.warning(
            "[ACTIVE SUPPLEMENT] no supplement added before=%s target=%s premarket=%s skipped_existing=%s skipped_flags=%s skipped_universe=%s skipped_liquidity=%s",
            before,
            target,
            premarket_mode,
            skipped_existing,
            skipped_flags,
            skipped_universe,
            skipped_liquidity,
        )

    return active, diag


def _trim_to_max(
    symbols: Iterable[str],
    *,
    protected: Set[str],
    liquidity_map: dict[str, dict[str, float]],
) -> Set[str]:
    items = dedupe_keep_order(symbols)
    if len(items) <= MAX_ACTIVE_SYMBOLS:
        return set(items)

    def sort_key(sym: str):
        info = liquidity_map.get(sym, {})
        is_protected = 1 if sym in protected else 0
        value = to_float(info.get("trading_value"), 0.0)
        volume = to_float(info.get("trading_volume"), 0.0)
        tick = to_float(info.get("tick_count"), 0.0)
        try:
            last_seen = global_data.symbol_last_seen.get(sym, dt.datetime.min)
        except Exception:
            last_seen = dt.datetime.min
        if last_seen is None:
            last_seen = dt.datetime.min
        return (is_protected, value, volume, tick, last_seen)

    return set(sorted(items, key=sort_key, reverse=True)[:MAX_ACTIVE_SYMBOLS])


def update_active_symbols(force: bool = False) -> List[str]:
    try:
        return _update_active_symbols_impl(force=force)
    except Exception:
        logger.exception("[ACTIVE] update_active_symbols failed")
        try:
            prev = dedupe_keep_order(getattr(global_data, "symbols_active", []))
            if prev:
                return prev
        except Exception:
            pass
        return []


def _update_active_symbols_impl(force: bool = False) -> List[str]:
    del force
    n = now_dt()
    if not hasattr(global_data, "symbol_last_seen"):
        global_data.symbol_last_seen = {}
    if not hasattr(global_data, "symbols_active"):
        global_data.symbols_active = set()

    prev_active: Set[str] = set(dedupe_keep_order(global_data.symbols_active))
    protected = get_protected_symbols()

    update_last_seen_from_ranking(n)
    eligible_symbols, _flag_info = load_symbol_flags_eligible_symbols()
    _publish_symbol_flags_cache(eligible_symbols, _flag_info)
    liquidity_map = build_liquidity_map()

    today_available = today_ranking_available(now=n)
    premarket_mode = ENABLE_PREMARKET_SBI and (
        is_premarket_time(n)
        or (USE_PREMARKET_WHEN_TODAY_RANKING_EMPTY and not today_available)
    )

    if premarket_mode:
        primary_candidates, allowed_universe, source_name = _build_premarket_candidates(
            now=n,
            eligible_symbols=eligible_symbols,
            protected=protected,
        )
    else:
        primary_candidates, allowed_universe, source_name = _build_today_ranking_candidates(
            now=n,
            eligible_symbols=eligible_symbols,
            protected=protected,
            liquidity_map=liquidity_map,
        )

    active: Set[str] = set(primary_candidates)
    active |= protected

    hot_symbols = extract_volume_speed_symbols()
    hot_symbols_allowed = {
        s for s in dedupe_keep_order(hot_symbols)
        if s in allowed_universe or s in protected
    }
    if not premarket_mode:
        hot_symbols_allowed = set(
            filter_liquid_symbols(
                hot_symbols_allowed,
                protected=protected,
                liquidity_map=liquidity_map,
                context="hot_symbols",
                require_info=True,
            )
        )
    active |= hot_symbols_allowed

    skipped_outside_universe: List[str] = []
    skipped_flags_or_liq: List[str] = []

    # まず従来の狭い補充。allowed_universe内を優先する。
    if len(active) < TARGET_ACTIVE_SYMBOLS:
        for sym in _iter_fallback_sources(prev_active, hot_symbols_allowed, primary_candidates):
            ns = normalize_symbol(sym)
            if not ns or ns in active:
                continue
            if ns not in allowed_universe and ns not in protected:
                skipped_outside_universe.append(ns)
                continue
            if ACTIVE_REQUIRE_SYMBOL_FLAGS and ns not in eligible_symbols and ns not in protected:
                skipped_flags_or_liq.append(ns)
                continue
            if not premarket_mode:
                if not is_liquid_symbol(ns, liquidity_map=liquidity_map, protected=protected, require_info=True):
                    skipped_flags_or_liq.append(ns)
                    continue
            active.add(ns)
            if len(active) >= TARGET_ACTIVE_SYMBOLS:
                break

    # それでも足りない場合、daily_watchlist → symbol_flags 適格銘柄の順で補充する。
    supplement_diag = {}
    if len(active) < TARGET_ACTIVE_SYMBOLS:
        active, supplement_diag = _supplement_active_to_target(
            active,
            target=TARGET_ACTIVE_SYMBOLS,
            premarket_mode=premarket_mode,
            allowed_universe=allowed_universe,
            eligible_symbols=eligible_symbols,
            flag_info=_flag_info,
            protected=protected,
            liquidity_map=liquidity_map,
            prev_active=prev_active,
            hot_symbols=hot_symbols_allowed,
            primary_candidates=primary_candidates,
        )

    active = _trim_to_max(active, protected=protected, liquidity_map=liquidity_map)
    active_list = final_guard_min_price(
        active,
        protected=protected,
        liquidity_map=liquidity_map,
        premarket_mode=premarket_mode,
    )

    reflect_active_to_global(active_list)
    set_global_attr("active_symbol_source", source_name)
    set_global_attr("active_symbol_premarket_mode", premarket_mode)
    set_global_attr("active_symbol_today_ranking_available", today_available)
    set_global_attr("active_symbol_allowed_universe_size", len(allowed_universe))
    set_global_attr("active_symbol_supplement_diag", supplement_diag)

    logger.info(
        "[ACTIVE] total=%d source=%s premarket=%s today_ranking_available=%s allowed_universe=%d protected=%d last_seen=%d hot=%d liquidity_filter=%s min_value=%.0f min_volume=%.0f min_tick=%.0f min_price=%.0f skipped_outside_universe=%d skipped_flags_or_liq=%d supplement_added=%d head=%s",
        len(active_list),
        source_name,
        premarket_mode,
        today_available,
        len(allowed_universe),
        len(protected),
        len(global_data.symbol_last_seen),
        len(hot_symbols_allowed),
        ENABLE_LIQUIDITY_FILTER,
        MIN_TRADING_VALUE,
        MIN_VOLUME,
        MIN_TICK_COUNT,
        MIN_PRICE,
        len(skipped_outside_universe),
        len(skipped_flags_or_liq),
        len(supplement_diag.get("added", []) if isinstance(supplement_diag, dict) else []),
        active_list[:10],
    )

    if len(active_list) < TARGET_ACTIVE_SYMBOLS:
        logger.warning(
            "[ACTIVE] below target total=%d target=%d source=%s reason=allowed_universe_or_minprice_limited outside_head=%s flags_or_liq_head=%s supplement_diag=%s",
            len(active_list),
            TARGET_ACTIVE_SYMBOLS,
            source_name,
            skipped_outside_universe[:20],
            skipped_flags_or_liq[:20],
            supplement_diag,
        )
    else:
        logger.warning(
            "[ACTIVE] target satisfied total=%d target=%d source=%s premarket=%s supplement_added=%s",
            len(active_list),
            TARGET_ACTIVE_SYMBOLS,
            source_name,
            premarket_mode,
            supplement_diag.get("added", []) if isinstance(supplement_diag, dict) else [],
        )

    return active_list


def get_active_symbols(*args, **kwargs) -> List[str]:
    del args, kwargs
    symbols = dedupe_keep_order(getattr(global_data, "symbols_active", []))
    if not symbols:
        symbols = dedupe_keep_order(get_global_attr("active_symbols", []))
    if not symbols:
        symbols = dedupe_keep_order(get_global_attr("monitor_symbols", []))
    try:
        if not is_premarket_time(now_dt()):
            symbols = final_guard_min_price(
                symbols,
                protected=get_protected_symbols(),
                liquidity_map=build_liquidity_map(),
                premarket_mode=False,
            )
    except Exception:
        logger.debug("[ACTIVE] getter min price guard failed", exc_info=True)
    return symbols[:MAX_ACTIVE_SYMBOLS]


def get_current_active_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()


def get_monitor_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()[:MAX_ACTIVE_SYMBOLS]


def get_push_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()[:MAX_ACTIVE_SYMBOLS]


def get_register_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()[:MAX_ACTIVE_SYMBOLS]


def get_subscription_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()[:MAX_ACTIVE_SYMBOLS]


def get_rotation_symbols(*args, **kwargs) -> List[str]:
    return get_active_symbols()[:MAX_ACTIVE_SYMBOLS]


def debug_active_symbols() -> dict:
    symbols = get_active_symbols()
    liquidity_map = build_liquidity_map()
    liquid = [
        s for s in symbols
        if is_liquid_symbol(s, liquidity_map=liquidity_map, protected=get_protected_symbols(), require_info=False)
    ]
    payload = {
        "total": len(symbols),
        "liquid_total": len(liquid),
        "head": symbols[:20],
        "source": get_global_attr("active_symbol_source", None),
        "premarket_mode": get_global_attr("active_symbol_premarket_mode", None),
        "today_ranking_available": get_global_attr("active_symbol_today_ranking_available", None),
        "allowed_universe_size": get_global_attr("active_symbol_allowed_universe_size", None),
        "supplement_diag": get_global_attr("active_symbol_supplement_diag", None),
        "liquidity_filter": ENABLE_LIQUIDITY_FILTER,
        "min_trading_value": MIN_TRADING_VALUE,
        "min_volume": MIN_VOLUME,
        "min_tick_count": MIN_TICK_COUNT,
        "min_price": MIN_PRICE,
        "last_seen": len(getattr(global_data, "symbol_last_seen", {}) or {}),
    }
    logger.info("[ACTIVE DEBUG] %s", payload)
    return payload
