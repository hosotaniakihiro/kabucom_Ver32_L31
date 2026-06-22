# ============================================================
# File   : core/startup/active_symbols_empty_universe_supplement_patch.py
# Version: V1-ACTIVE-EMPTY-UNIVERSE-SUPPLEMENT
# ------------------------------------------------------------
# Purpose:
#   During market hours, active_symbols.manager normally restricts
#   supplements to allowed_universe from today's ranking.
#   In push_receiver/data-collector processes, global_data.latest_ranking
#   can be empty while ranking DB fallback has usable symbols. When
#   allowed_universe is empty, the strict universe check drops every
#   daily_watchlist/global candidate and logs skipped_universe=xxxx.
#
#   This patch relaxes only that empty-universe case so PUSH can keep a
#   100-symbol target without relying on the later DB fallback path.
# ============================================================
from __future__ import annotations

import logging
from typing import Any, Iterable, Set

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_SUPPLEMENT = None


def install() -> bool:
    global _PATCHED, _ORIGINAL_SUPPLEMENT
    if _PATCHED:
        return True

    try:
        import trading.ranking.active_symbols.manager as manager

        original = getattr(manager, "_supplement_active_to_target", None)
        if not callable(original):
            logger.warning("[ACTIVE EMPTY UNIVERSE PATCH] skipped: target function missing")
            return False

        _ORIGINAL_SUPPLEMENT = original

        def _patched_supplement_active_to_target(
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
            universe_empty = not bool(allowed_universe)
            if premarket_mode or not universe_empty:
                return original(
                    active,
                    target=target,
                    premarket_mode=premarket_mode,
                    allowed_universe=allowed_universe,
                    eligible_symbols=eligible_symbols,
                    flag_info=flag_info,
                    protected=protected,
                    liquidity_map=liquidity_map,
                    prev_active=prev_active,
                    hot_symbols=hot_symbols,
                    primary_candidates=primary_candidates,
                )

            before = len(active)
            added: list[str] = []
            skipped_existing = 0
            skipped_flags = 0
            skipped_liquidity = 0

            try:
                sources = manager._iter_target_supplement_sources(
                    prev_active=prev_active,
                    hot_symbols=hot_symbols,
                    primary_candidates=primary_candidates,
                    allowed_universe=set(),
                    eligible_symbols=eligible_symbols,
                    protected=protected,
                    liquidity_map=liquidity_map,
                    flag_info=flag_info,
                )
            except Exception:
                logger.exception("[ACTIVE EMPTY UNIVERSE PATCH] source iterator failed")
                return original(
                    active,
                    target=target,
                    premarket_mode=premarket_mode,
                    allowed_universe=allowed_universe,
                    eligible_symbols=eligible_symbols,
                    flag_info=flag_info,
                    protected=protected,
                    liquidity_map=liquidity_map,
                    prev_active=prev_active,
                    hot_symbols=hot_symbols,
                    primary_candidates=primary_candidates,
                )

            for sym in sources:
                ns = manager.normalize_symbol(sym)
                if not ns:
                    continue
                if ns in active:
                    skipped_existing += 1
                    continue
                if manager.ACTIVE_REQUIRE_SYMBOL_FLAGS and ns not in eligible_symbols and ns not in protected:
                    skipped_flags += 1
                    continue

                # allowed_universe is empty because this process has no in-memory
                # ranking snapshot. Do not count these as skipped_universe. If
                # ranking liquidity info exists, enforce it; if not, keep the
                # candidate and let final_guard_min_price do the summary DB price
                # fallback / final price-band check.
                info = (liquidity_map or {}).get(ns)
                if info:
                    try:
                        if not manager.is_liquid_symbol(ns, liquidity_map=liquidity_map, protected=protected, require_info=False):
                            skipped_liquidity += 1
                            continue
                    except Exception:
                        logger.debug("[ACTIVE EMPTY UNIVERSE PATCH] liquidity check failed sym=%s", ns, exc_info=True)

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
                "skipped_universe": 0,
                "skipped_liquidity": skipped_liquidity,
                "premarket_mode": premarket_mode,
                "target": target,
                "allowed_universe_empty_relaxed": True,
            }
            logger.warning(
                "[ACTIVE EMPTY UNIVERSE PATCH] relaxed supplement before=%s after=%s target=%s added=%s skipped_existing=%s skipped_flags=%s skipped_liquidity=%s",
                before,
                len(active),
                target,
                added[:30],
                skipped_existing,
                skipped_flags,
                skipped_liquidity,
            )
            return active, diag

        manager._supplement_active_to_target = _patched_supplement_active_to_target
        _PATCHED = True
        logger.warning("[ACTIVE EMPTY UNIVERSE PATCH] installed")
        return True
    except Exception:
        logger.exception("[ACTIVE EMPTY UNIVERSE PATCH] install failed")
        return False


__all__ = ["install"]
