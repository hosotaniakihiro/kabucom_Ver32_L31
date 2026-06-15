# -*- coding: utf-8 -*-
"""
Active symbol getter patch.

Premarket SBI symbols are useful only before the market opens.  During market
hours, even if the last active list was created from the premarket SBI source,
getters must re-guard/rebuild against the normal ranking-derived universe so
PUSH rotation follows live ranking activity.
"""
from __future__ import annotations

import logging
import os
from typing import List

from global_state import global_data

logger = logging.getLogger(__name__)

_INSTALLED = False


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        from trading.ranking.active_symbols import manager as mgr
        from trading.ranking.active_symbols.global_helpers import get_global_attr
        from trading.ranking.active_symbols.liquidity import final_guard_min_price
        from trading.ranking.active_symbols.normalize import dedupe_keep_order
        from trading.ranking.active_symbols.protected import get_protected_symbols
        from trading.ranking.active_symbols.ranking_source import build_liquidity_map
        from trading.ranking.active_symbols.premarket_source import is_premarket_time
        from trading.ranking.active_symbols.normalize import now as now_dt
    except Exception:
        logger.exception("[ACTIVE GETTER PREMARKET PATCH] import failed")
        return False

    allow_intraday_sbi = _env_bool("ACTIVE_ALLOW_INTRADAY_PREMARKET_FALLBACK", False)
    if not allow_intraday_sbi:
        # manager.py imports this as a module-level constant.  Override it too
        # so update_active_symbols() itself cannot fall back to premarket SBI
        # after 09:00 just because today's ranking is temporarily empty/stale.
        try:
            setattr(mgr, "USE_PREMARKET_WHEN_TODAY_RANKING_EMPTY", False)
            os.environ["ACTIVE_USE_PREMARKET_WHEN_TODAY_RANKING_EMPTY"] = "0"
        except Exception:
            logger.debug("[ACTIVE GETTER PREMARKET PATCH] manager fallback override failed", exc_info=True)

    original_get_active_symbols = getattr(mgr, "get_active_symbols", None)

    def _patched_get_active_symbols(*args, **kwargs) -> List[str]:
        del args, kwargs
        symbols = dedupe_keep_order(getattr(global_data, "symbols_active", []))
        if not symbols:
            symbols = dedupe_keep_order(get_global_attr("active_symbols", []))
        if not symbols:
            symbols = dedupe_keep_order(get_global_attr("monitor_symbols", []))

        if not symbols:
            return []

        try:
            current_is_premarket = bool(is_premarket_time(now_dt()))
            source_premarket = bool(get_global_attr("active_symbol_premarket_mode", False))
            preserve_premarket = _env_bool("ACTIVE_GETTER_PRESERVE_PREMARKET_MODE", True)
            skip_guard_when_premarket = _env_bool("ACTIVE_GETTER_SKIP_PRICE_GUARD_FOR_PREMARKET", True)
            allow_intraday_sbi_now = _env_bool("ACTIVE_ALLOW_INTRADAY_PREMARKET_FALLBACK", False)

            if (
                current_is_premarket
                and source_premarket
                and preserve_premarket
                and skip_guard_when_premarket
            ):
                # 寄前時間中だけ、寄前SBI由来リストを再ガードせず保持する。
                logger.info(
                    "[ACTIVE GETTER PREMARKET PATCH] keep premarket symbols before open count=%d head=%s",
                    len(symbols),
                    symbols[:20],
                )
                return symbols[: getattr(mgr, "MAX_ACTIVE_SYMBOLS", 100)]

            if source_premarket and not current_is_premarket and not allow_intraday_sbi_now:
                logger.info(
                    "[ACTIVE GETTER PREMARKET PATCH] market-hours ranking mode: re-guard former premarket symbols count=%d",
                    len(symbols),
                )

            premarket_mode = bool(current_is_premarket and source_premarket and preserve_premarket)
            symbols = final_guard_min_price(
                symbols,
                protected=get_protected_symbols(),
                liquidity_map=build_liquidity_map(),
                premarket_mode=premarket_mode,
            )
        except Exception:
            logger.debug("[ACTIVE GETTER PREMARKET PATCH] getter min price guard failed", exc_info=True)
            if callable(original_get_active_symbols):
                try:
                    return original_get_active_symbols()[: getattr(mgr, "MAX_ACTIVE_SYMBOLS", 100)]
                except Exception:
                    pass

        return symbols[: getattr(mgr, "MAX_ACTIVE_SYMBOLS", 100)]

    def _patched_current(*args, **kwargs) -> List[str]:
        return _patched_get_active_symbols()

    mgr.get_active_symbols = _patched_get_active_symbols
    mgr.get_current_active_symbols = _patched_current
    mgr.get_monitor_symbols = lambda *a, **k: _patched_get_active_symbols()[: getattr(mgr, "MAX_ACTIVE_SYMBOLS", 100)]
    mgr.get_push_symbols = lambda *a, **k: _patched_get_active_symbols()[: getattr(mgr, "MAX_ACTIVE_SYMBOLS", 100)]
    mgr.get_register_symbols = lambda *a, **k: _patched_get_active_symbols()[: getattr(mgr, "MAX_ACTIVE_SYMBOLS", 100)]
    mgr.get_subscription_symbols = lambda *a, **k: _patched_get_active_symbols()[: getattr(mgr, "MAX_ACTIVE_SYMBOLS", 100)]
    mgr.get_rotation_symbols = lambda *a, **k: _patched_get_active_symbols()[: getattr(mgr, "MAX_ACTIVE_SYMBOLS", 100)]

    _INSTALLED = True
    logger.warning(
        "[ACTIVE GETTER PREMARKET PATCH] installed premarket_only_before_open=True intraday_sbi_fallback=%s",
        allow_intraday_sbi,
    )
    return True


__all__ = ["install"]