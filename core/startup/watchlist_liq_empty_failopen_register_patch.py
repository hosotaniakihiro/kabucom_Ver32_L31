from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable, List

logger = logging.getLogger(__name__)
_INSTALLED = False
_LAST_GOOD_REGISTER_SYMBOLS: list[str] = []
_LAST_GOOD_TS: float = 0.0


def _is_push_register_context(context: str) -> bool:
    text = str(context or "").lower()
    return "push" in text or "rotation" in text or "register" in text


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _target_keep_count(items: list[str]) -> int:
    if not items:
        return 0
    default_target = 100 if len(items) >= 80 else min(len(items), 50)
    target = _env_int("WATCHLIST_RECENT_LIQ_FAILOPEN_TARGET_KEEP", default_target)
    return max(1, min(len(items), int(target)))


def _merge_keep_order(primary: list[str], items: list[str], *, target: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for src in (primary or [], items or []):
        s = str(src).strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= target:
            break
    return out


def _remember_good(symbols: list[str], *, context: str) -> None:
    global _LAST_GOOD_REGISTER_SYMBOLS, _LAST_GOOD_TS
    if not symbols or not _is_push_register_context(context):
        return

    # 25銘柄などに縮んだ一時結果で、より大きい last_good を上書きしない。
    min_good = max(1, _env_int("WATCHLIST_LIQ_EMPTY_MIN_LAST_GOOD_KEEP", 50))
    if len(symbols) < min_good and len(_LAST_GOOD_REGISTER_SYMBOLS) >= min_good:
        logger.warning(
            "[WATCHLIST LIQ EMPTY SAFE] keep previous last_good context=%s new_count=%s prev_count=%s min_good=%s",
            context,
            len(symbols),
            len(_LAST_GOOD_REGISTER_SYMBOLS),
            min_good,
        )
        return

    _LAST_GOOD_REGISTER_SYMBOLS = list(symbols)
    _LAST_GOOD_TS = time.time()


def _last_good(max_age_sec: float = 900.0) -> list[str]:
    if not _LAST_GOOD_REGISTER_SYMBOLS:
        return []
    if (time.time() - _LAST_GOOD_TS) > max_age_sec:
        return []
    return list(_LAST_GOOD_REGISTER_SYMBOLS)


def _safe_failopen_subset(items: list[str], *, context: str, reason: str) -> list[str]:
    if not items:
        return []
    min_keep = max(1, _env_int("WATCHLIST_RECENT_LIQ_FAILOPEN_MIN_KEEP", 50))
    target_keep = _target_keep_count(items)
    max_keep = max(min_keep, _env_int("WATCHLIST_RECENT_LIQ_FAILOPEN_MAX_KEEP", target_keep))
    max_keep = min(len(items), max_keep)
    # PUSH rotation は内部で50件ずつ分割登録するため、ここでは100件候補を維持してよい。
    out = list(items[:max_keep])
    logger.warning(
        "[WATCHLIST LIQ EMPTY SAFE] fail-open subset context=%s reason=%s before=%s after=%s min_keep=%s max_keep=%s target_keep=%s head=%s",
        context,
        reason,
        len(items),
        len(out),
        min_keep,
        max_keep,
        target_keep,
        out[:20],
    )
    _remember_good(out, context=context)
    return out


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from core.startup import watchlist_recent_liquidity_guard_patch as mod
        from core.startup import watchlist_recent_liquidity_bulk_patch as bulk

        def _fallback_for_liq_unavailable(items: list[str], *, context: str, reason: str) -> list[str]:
            max_last_good_age = mod._env_float("WATCHLIST_LIQ_EMPTY_LAST_GOOD_SEC", 900.0)
            fallback = _last_good(max_last_good_age)
            if fallback:
                if _is_push_register_context(context):
                    target_keep = _target_keep_count(items)
                    if items and len(fallback) < target_keep:
                        merged = _merge_keep_order(fallback, items, target=target_keep)
                        logger.warning(
                            "[WATCHLIST LIQ EMPTY SAFE] expand small last_good context=%s reason=%s current_count=%s fallback_count=%s expanded_count=%s target_keep=%s age_sec=%.1f",
                            context,
                            reason,
                            len(items),
                            len(fallback),
                            len(merged),
                            target_keep,
                            time.time() - _LAST_GOOD_TS,
                        )
                        _remember_good(merged, context=context)
                        return merged

                logger.warning(
                    "[WATCHLIST LIQ EMPTY SAFE] use last_good context=%s reason=%s current_count=%s fallback_count=%s age_sec=%.1f",
                    context,
                    reason,
                    len(items),
                    len(fallback),
                    time.time() - _LAST_GOOD_TS,
                )
                return fallback

            # 起動直後やDBタイムアウトで last_good が無い場合でも、PUSH登録対象を0件にしない。
            # ここで空にすると ws_alive=True でも登録更新が止まり、PUSH受信が細る。
            if _is_push_register_context(context) and _env_bool("WATCHLIST_RECENT_LIQ_FAILOPEN_WITHOUT_LAST_GOOD_FOR_PUSH", True):
                return _safe_failopen_subset(items, context=context, reason=reason)

            if mod._env_bool("WATCHLIST_RECENT_LIQ_ALLOW_FAIL_OPEN_WITHOUT_LAST_GOOD", False):
                logger.warning(
                    "[WATCHLIST LIQ EMPTY SAFE] explicit fail-open allowed context=%s reason=%s count=%s",
                    context,
                    reason,
                    len(items),
                )
                _remember_good(items, context=context)
                return items

            logger.warning(
                "[WATCHLIST LIQ EMPTY SAFE] fail-closed context=%s reason=%s count=%s no_last_good; return empty to avoid low-liquidity registration",
                context,
                reason,
                len(items),
            )
            return []

        def _filter_symbols_safe(symbols: Iterable[Any], *, context: str) -> List[str]:
            items = mod._dedupe(symbols)

            if not mod._env_bool("WATCHLIST_RECENT_LIQ_ENABLED", True):
                _remember_good(items, context=context)
                return items

            if not items:
                if _is_push_register_context(context):
                    return _fallback_for_liq_unavailable(items, context=context, reason="empty_input")
                return items

            protected = mod._protected_symbols()
            protected_items = [s for s in items if s in protected]
            check_items = [s for s in items if s not in protected]
            stats_map, timed_out = bulk._bulk_stats(mod, check_items)

            if timed_out:
                return _fallback_for_liq_unavailable(items, context=context, reason="timeout_or_main_skip")

            if check_items and not stats_map:
                return _fallback_for_liq_unavailable(items, context=context, reason="no_recent_summary_hit")

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
                    skipped.append({"reason": "NO_RECENT_LIQ_DATA", **detail})
                elif bulk._as_float(st.get("latest_volume"), 0.0) < min_latest:
                    skipped.append({"reason": "LATEST_VOLUME_LOW", **detail})
                elif bulk._as_float(st.get("avg_volume"), 0.0) < min_avg:
                    skipped.append({"reason": "AVG_VOLUME_LOW", **detail})
                elif bulk._as_float(st.get("total_turnover"), 0.0) < min_turnover:
                    skipped.append({"reason": "TURNOVER_LOW", **detail})
                else:
                    kept.append(s)

            if not kept and items:
                return _fallback_for_liq_unavailable(items, context=context, reason="all_filtered")

            if skipped:
                logger.warning(
                    "[WATCHLIST LIQ EMPTY SAFE] filtered context=%s before=%s after=%s protected=%s skipped=%s",
                    context,
                    len(items),
                    len(kept),
                    len(protected_items),
                    skipped[:80],
                )
            else:
                logger.info("[WATCHLIST LIQ EMPTY SAFE] passed context=%s count=%s protected=%s", context, len(kept), len(protected_items))

            _remember_good(kept, context=context)
            return kept

        mod._filter_symbols = _filter_symbols_safe
        _INSTALLED = True
        logger.warning(
            "[WATCHLIST LIQ EMPTY SAFE] installed v3 push_failopen_without_last_good=%s min_keep=%s max_keep=%s target_keep=%s min_last_good=%s explicit_failopen=%s",
            _env_bool("WATCHLIST_RECENT_LIQ_FAILOPEN_WITHOUT_LAST_GOOD_FOR_PUSH", True),
            _env_int("WATCHLIST_RECENT_LIQ_FAILOPEN_MIN_KEEP", 50),
            _env_int("WATCHLIST_RECENT_LIQ_FAILOPEN_MAX_KEEP", 100),
            _env_int("WATCHLIST_RECENT_LIQ_FAILOPEN_TARGET_KEEP", 100),
            _env_int("WATCHLIST_LIQ_EMPTY_MIN_LAST_GOOD_KEEP", 50),
            mod._env_bool("WATCHLIST_RECENT_LIQ_ALLOW_FAIL_OPEN_WITHOUT_LAST_GOOD", False),
        )
        return True
    except Exception:
        logger.exception("[WATCHLIST LIQ EMPTY SAFE] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[WATCHLIST LIQ EMPTY SAFE] auto install failed")

__all__ = ["install"]
