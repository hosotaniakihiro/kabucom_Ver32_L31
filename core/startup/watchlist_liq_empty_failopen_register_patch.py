from __future__ import annotations

import logging
from typing import Any, Iterable, List

logger = logging.getLogger(__name__)
_INSTALLED = False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from core.startup import watchlist_recent_liquidity_guard_patch as mod
        from core.startup import watchlist_recent_liquidity_bulk_patch as bulk

        def _filter_symbols_failopen_empty(symbols: Iterable[Any], *, context: str) -> List[str]:
            items = mod._dedupe(symbols)
            if not mod._env_bool("WATCHLIST_RECENT_LIQ_ENABLED", True):
                return items

            protected = mod._protected_symbols()
            protected_items = [s for s in items if s in protected]
            check_items = [s for s in items if s not in protected]
            stats_map, timed_out = bulk._bulk_stats(mod, check_items)

            if timed_out and mod._env_bool("WATCHLIST_RECENT_LIQ_FAIL_OPEN_ON_TIMEOUT", True):
                logger.warning("[WATCHLIST LIQ EMPTY FAILOPEN] fail-open context=%s count=%s reason=timeout_or_main_skip", context, len(items))
                return items

            if check_items and not stats_map:
                # PUSH登録でここを空にするとWebSocket登録が0件になり受信が止まる。
                # 起動直後・サマリー未作成・DB更新遅延は監視銘柄を落とさず通す。
                logger.warning("[WATCHLIST LIQ EMPTY FAILOPEN] fail-open context=%s count=%s reason=no_recent_summary_hit protected=%s", context, len(items), len(protected_items))
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
                return items

            if skipped:
                logger.warning("[WATCHLIST LIQ EMPTY FAILOPEN] filtered context=%s before=%s after=%s protected=%s skipped=%s", context, len(items), len(kept), len(protected_items), skipped[:80])
            else:
                logger.info("[WATCHLIST LIQ EMPTY FAILOPEN] passed context=%s count=%s protected=%s", context, len(kept), len(protected_items))
            return kept

        mod._filter_symbols = _filter_symbols_failopen_empty
        _INSTALLED = True
        logger.warning("[WATCHLIST LIQ EMPTY FAILOPEN] installed V1")
        return True
    except Exception:
        logger.exception("[WATCHLIST LIQ EMPTY FAILOPEN] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[WATCHLIST LIQ EMPTY FAILOPEN] auto install failed")

__all__ = ["install"]
