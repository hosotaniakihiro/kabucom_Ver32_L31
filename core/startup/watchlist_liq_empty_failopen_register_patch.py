from __future__ import annotations

import logging
import time
from typing import Any, Iterable, List

logger = logging.getLogger(__name__)
_INSTALLED = False
_LAST_GOOD_REGISTER_SYMBOLS: list[str] = []
_LAST_GOOD_TS: float = 0.0


def _is_push_register_context(context: str) -> bool:
    text = str(context or "").lower()
    return "push" in text or "rotation" in text or "register" in text


def _remember_good(symbols: list[str], *, context: str) -> None:
    global _LAST_GOOD_REGISTER_SYMBOLS, _LAST_GOOD_TS
    if symbols and _is_push_register_context(context):
        _LAST_GOOD_REGISTER_SYMBOLS = list(symbols)
        _LAST_GOOD_TS = time.time()


def _last_good(max_age_sec: float = 900.0) -> list[str]:
    if not _LAST_GOOD_REGISTER_SYMBOLS:
        return []
    if (time.time() - _LAST_GOOD_TS) > max_age_sec:
        return []
    return list(_LAST_GOOD_REGISTER_SYMBOLS)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from core.startup import watchlist_recent_liquidity_guard_patch as mod
        from core.startup import watchlist_recent_liquidity_bulk_patch as bulk

        def _filter_symbols_failopen_empty(symbols: Iterable[Any], *, context: str) -> List[str]:
            items = mod._dedupe(symbols)
            max_last_good_age = mod._env_float("WATCHLIST_LIQ_EMPTY_LAST_GOOD_SEC", 900.0)

            if not mod._env_bool("WATCHLIST_RECENT_LIQ_ENABLED", True):
                _remember_good(items, context=context)
                return items

            if not items and _is_push_register_context(context):
                fallback = _last_good(max_last_good_age)
                if fallback:
                    logger.warning(
                        "[WATCHLIST LIQ EMPTY FAILOPEN] fail-open context=%s count=0 reason=empty_input_last_good fallback=%s age_sec=%.1f",
                        context, len(fallback), time.time() - _LAST_GOOD_TS,
                    )
                    return fallback
                logger.warning(
                    "[WATCHLIST LIQ EMPTY FAILOPEN] empty input context=%s no last_good available -> keep empty",
                    context,
                )
                return items

            protected = mod._protected_symbols()
            protected_items = [s for s in items if s in protected]
            check_items = [s for s in items if s not in protected]
            stats_map, timed_out = bulk._bulk_stats(mod, check_items)

            if timed_out and mod._env_bool("WATCHLIST_RECENT_LIQ_FAIL_OPEN_ON_TIMEOUT", True):
                logger.warning("[WATCHLIST LIQ EMPTY FAILOPEN] fail-open context=%s count=%s reason=timeout_or_main_skip", context, len(items))
                _remember_good(items, context=context)
                return items

            if check_items and not stats_map:
                # PUSH登録でここを空にするとWebSocket登録が0件になり受信が止まる。
                # 起動直後・サマリー未作成・DB更新遅延は監視銘柄を落とさず通す。
                logger.warning("[WATCHLIST LIQ EMPTY FAILOPEN] fail-open context=%s count=%s reason=no_recent_summary_hit protected=%s", context, len(items), len(protected_items))
                _remember_good(items, context=context)
                return items

            kept: List[str] = []
            skipped: List[dict[str, Any]] = []
            min_latest = mod._env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0)
            min_avg = mod._env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0)
            min_turnover = mod._env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1_000_000.0)

            for s in items:
                if s in protected:
                    kept.append(s)
                    continue
                st = stats_map.get(s) or {}
                detail = {"symbol": s, **st, "min_latest_volume": min_latest, "min_avg_volume": min_avg, "min_turnover": min_turnover}
                if not st:
                    # 一部だけ欠けている場合も登録用途では落としすぎない。
                    kept.append(s)
                elif bulk._as_float(st.get("latest_volume"), 0.0) < min_latest:
                    skipped.append({"reason": "LATEST_VOLUME_LOW", **detail})
                elif bulk._as_float(st.get("avg_volume"), 0.0) < min_avg:
                    skipped.append({"reason": "AVG_VOLUME_LOW", **detail})
                elif bulk._as_float(st.get("total_turnover"), 0.0) < min_turnover:
                    skipped.append({"reason": "TURNOVER_LOW", **detail})
                else:
                    kept.append(s)

            if not kept and items:
                logger.warning("[WATCHLIST LIQ EMPTY FAILOPEN] fail-open context=%s count=%s reason=all_filtered", context, len(items))
                _remember_good(items, context=context)
                return items

            if skipped:
                logger.warning("[WATCHLIST LIQ EMPTY FAILOPEN] filtered context=%s before=%s after=%s protected=%s skipped=%s", context, len(items), len(kept), len(protected_items), skipped[:80])
            else:
                logger.info("[WATCHLIST LIQ EMPTY FAILOPEN] passed context=%s count=%s protected=%s", context, len(kept), len(protected_items))

            _remember_good(kept, context=context)
            return kept

        mod._filter_symbols = _filter_symbols_failopen_empty
        _INSTALLED = True
        logger.warning("[WATCHLIST LIQ EMPTY FAILOPEN] installed V2 last_good_empty_input=1")
        return True
    except Exception:
        logger.exception("[WATCHLIST LIQ EMPTY FAILOPEN] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[WATCHLIST LIQ EMPTY FAILOPEN] auto install failed")

__all__ = ["install"]
