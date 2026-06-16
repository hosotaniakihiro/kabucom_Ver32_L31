from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)
VERSION = "V1.3-ROTATION-TARGET-TOPUP-AB100"
_INSTALLED = False


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default)).replace(',', '')))
    except Exception:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)).replace(',', ''))
    except Exception:
        return default


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).lower() in {"1", "true", "yes", "on", "enabled"}


def _d(xs: Iterable[Any]) -> list[str]:
    out, seen = [], set()
    for x in xs or []:
        s = str(x).strip().upper()
        if s and s not in seen:
            seen.add(s); out.append(s)
    return out


def _rot(reason: Any) -> bool:
    s = str(reason or "").lower()
    return s.startswith("rotation_") or s.startswith("push_rotation_") or s in {"rotation", "rotate"}


def _onopen(reason: Any) -> bool:
    s = str(reason or "").lower()
    return s.startswith("on_open") or s.startswith("onopen") or "on_open" in s


def _patch_target_resolution() -> bool:
    """
    runtime.ats_register_targets が50銘柄しか無い場合でも、
    active_symbol_manager / global_data / dynamic providers から不足分を補充し、
    100銘柄候補を作って A=50 / B=50 ローテーションへ戻す。

    重要:
      rotation_core は `from .rotation_symbols import resolve_register_targets` で
      関数参照を保持しているため、rotation_core 側の参照も同時に差し替える。
    """
    try:
        from . import rotation_core
        from . import rotation_symbols
    except Exception:
        logger.exception("[PUSH ROTATION STABILITY] target resolution import failed")
        return False

    if getattr(rotation_symbols, "_STABILITY_PATCHED_TARGET_TOPUP", False):
        return True

    def _clean(src: Any) -> tuple[list[str], int, int, int]:
        try:
            return rotation_symbols.clean_symbol_list(src)
        except Exception:
            real = _d(src)
            return real, len(real), 0, 0

    def _add(dst: list[str], src: Any, *, source: str) -> None:
        real, raw_count, filler_count, invalid_count = _clean(src)
        if not real:
            logger.info(
                "[PUSH ROTATION TARGET TOPUP] source=%s raw=%d real=0 filler=%d invalid=%d total=%d",
                source,
                raw_count,
                filler_count,
                invalid_count,
                len(dst),
            )
            return
        before = len(dst)
        dst[:] = _d(list(dst) + list(real))
        logger.info(
            "[PUSH ROTATION TARGET TOPUP] source=%s raw=%d real=%d added=%d total=%d head=%s",
            source,
            raw_count,
            len(real),
            len(dst) - before,
            len(dst),
            dst[:10],
        )

    def _call(fn: Any) -> Any:
        if not callable(fn):
            return None
        for caller in (
            lambda: fn(limit=rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS),
            lambda: fn(max_symbols=rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS),
            lambda: fn(n=rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS),
            lambda: fn(rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS),
            lambda: fn(),
        ):
            try:
                return caller()
            except TypeError:
                continue
            except Exception:
                logger.debug("[PUSH ROTATION TARGET TOPUP] provider failed fn=%s", fn, exc_info=True)
                return None
        return None

    def _collect_runtime_all(dst: list[str]) -> None:
        # 既存の _resolve_from_runtime は「最初に見つかった実銘柄リスト」でreturnする。
        # ここでは全キーを集約し、runtime.ats_register_targets 50銘柄だけで止まらないようにする。
        for key in getattr(rotation_symbols, "_RUNTIME_SYMBOL_KEYS", ()):
            try:
                src = rotation_symbols._safe_get_runtime(key)
            except Exception:
                src = None
            if src is not None:
                _add(dst, src, source=f"runtime.{key}")

    def _collect_global_data_all(dst: list[str]) -> None:
        try:
            gd = rotation_symbols._get_global_data()
        except Exception:
            gd = None
        if gd is None:
            return

        attrs = list(getattr(rotation_symbols, "_GLOBAL_DATA_SYMBOL_ATTRS", ())) + [
            "ranking_candidate_symbols",
            "ranking_symbols",
            "last_ranking_symbols",
            "latest_ranking_symbols",
            "daily_active_symbols",
            "daily_watchlist_symbols",
            "watchlist_symbols",
            "optional_watchlist_symbols",
        ]
        for attr in attrs:
            try:
                val = getattr(gd, attr, None)
                val = _call(val) if callable(val) else val
                if val is not None:
                    _add(dst, val, source=f"global_data.{attr}")
            except Exception:
                logger.debug("[PUSH ROTATION TARGET TOPUP] global_data attr failed attr=%s", attr, exc_info=True)

        for name in (
            "get_monitor_symbols",
            "get_active_symbols",
            "get_push_symbols",
            "get_register_symbols",
            "get_ats_targets",
            "get_ats_register_targets",
            "get_ranking_symbols",
            "get_daily_watchlist_symbols",
        ):
            try:
                val = _call(getattr(gd, name, None))
                if val is not None:
                    _add(dst, val, source=f"global_data.{name}()")
            except Exception:
                logger.debug("[PUSH ROTATION TARGET TOPUP] global_data getter failed name=%s", name, exc_info=True)

    def _collect_active_symbol_manager(dst: list[str]) -> None:
        try:
            import trading.ranking.active_symbol_manager as asm
        except Exception:
            return

        try:
            fn = getattr(asm, "update_active_symbols", None)
            if callable(fn):
                rebuilt = fn(force=True)
                _add(dst, rebuilt, source="active_symbol_manager.update_active_symbols(force=True)")
        except TypeError:
            try:
                rebuilt = getattr(asm, "update_active_symbols")()
                _add(dst, rebuilt, source="active_symbol_manager.update_active_symbols()")
            except Exception:
                logger.warning("[PUSH ROTATION TARGET TOPUP] update_active_symbols failed; continue", exc_info=True)
        except Exception:
            logger.warning("[PUSH ROTATION TARGET TOPUP] update_active_symbols failed; continue", exc_info=True)

        for name in (
            "get_rotation_symbols",
            "get_register_symbols",
            "get_push_symbols",
            "get_monitor_symbols",
            "get_active_symbols",
            "get_current_active_symbols",
            "get_subscription_symbols",
            "get_daily_watchlist_symbols",
            "load_daily_watchlist_symbols",
        ):
            try:
                val = _call(getattr(asm, name, None))
                if val is not None:
                    _add(dst, val, source=f"active_symbol_manager.{name}()")
            except Exception:
                logger.debug("[PUSH ROTATION TARGET TOPUP] active getter failed name=%s", name, exc_info=True)

    def _collect_dynamic_providers_all(dst: list[str]) -> None:
        # 既存 _resolve_from_dynamic_providers は最初のproviderでreturnするため、ここでは全部集約する。
        import importlib

        for module_name, func_name in getattr(rotation_symbols, "_DYNAMIC_SYMBOL_PROVIDERS", ()):
            try:
                mod = importlib.import_module(module_name)
                val = _call(getattr(mod, func_name, None))
                if val is not None:
                    _add(dst, val, source=f"{module_name}.{func_name}()")
            except Exception:
                logger.debug(
                    "[PUSH ROTATION TARGET TOPUP] dynamic provider failed %s.%s",
                    module_name,
                    func_name,
                    exc_info=True,
                )

    def resolve_monitor_symbols_patched() -> list[str]:
        max_keep = max(1, _i("PUSH_ROTATION_TARGET_MAX_KEEP", rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS))
        min_keep = max(1, _i("PUSH_ROTATION_TARGET_MIN_KEEP", min(100, max_keep)))

        merged: list[str] = []

        _collect_runtime_all(merged)
        _collect_global_data_all(merged)

        if len(merged) < min_keep:
            _collect_active_symbol_manager(merged)
            _collect_global_data_all(merged)

        if len(merged) < min_keep:
            _collect_dynamic_providers_all(merged)

        merged = _d(merged)[:max_keep]

        if merged:
            level = logger.warning if len(merged) < min_keep else logger.info
            level(
                "[PUSH ROTATION TARGET TOPUP] resolved total=%d min_keep=%d max_keep=%d head=%s",
                len(merged),
                min_keep,
                max_keep,
                merged[:20],
            )
            return merged

        logger.warning("[PUSH ROTATION TARGET TOPUP] no symbols resolved after topup")
        return []

    def resolve_register_targets_patched() -> list[str]:
        targets = resolve_monitor_symbols_patched()
        targets, raw_count, filler_count, invalid_count = rotation_symbols.clean_symbol_list(targets)

        if not targets:
            logger.warning(
                "[push_stream] no real register targets resolved raw=%d filler=%d invalid=%d",
                raw_count,
                filler_count,
                invalid_count,
            )
            return []

        before_limit = len(targets)

        try:
            if callable(rotation_symbols.merge_protected_first):
                merged = rotation_symbols.merge_protected_first(
                    targets,
                    max_symbols=rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS,
                )
                if merged:
                    logger.warning(
                        "[push_stream] protected merge applied before=%d after=%d protected_head=%s",
                        len(targets),
                        len(merged),
                        merged[:20],
                    )
                    targets = merged
        except Exception:
            logger.exception("[push_stream] protected merge failed")

        targets = targets[:rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS]

        logger.info(
            "[push_stream] resolved register targets before_liquidity total=%d limited=%d max=%d chunk=%d head=%s",
            before_limit,
            len(targets),
            rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS,
            rotation_symbols.DEFAULT_REGISTER_CHUNK_SIZE,
            targets[:10],
        )

        targets = rotation_symbols.apply_register_liquidity_guard(targets)

        if not targets:
            logger.warning(
                "[push_stream] no register targets after liquidity guard before_limit=%d max=%d",
                before_limit,
                rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS,
            )
            return []

        targets = targets[:rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS]

        logger.info(
            "[push_stream] resolved register targets after_liquidity total=%d max=%d chunk=%d head=%s",
            len(targets),
            rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS,
            rotation_symbols.DEFAULT_REGISTER_CHUNK_SIZE,
            targets[:10],
        )
        return targets

    rotation_symbols.resolve_monitor_symbols = resolve_monitor_symbols_patched
    rotation_symbols.resolve_register_targets = resolve_register_targets_patched
    rotation_core.resolve_register_targets = resolve_register_targets_patched

    rotation_symbols._STABILITY_PATCHED_TARGET_TOPUP = True
    logger.warning("[PUSH ROTATION STABILITY] target topup patch installed version=%s", VERSION)
    return True


def _patch_batches() -> bool:
    try:
        from . import rotation_core
    except Exception:
        logger.exception("[PUSH ROTATION STABILITY] rotation_core import failed")
        return False
    if getattr(rotation_core, "_STABILITY_PATCHED_BATCHES", False):
        return True

    def build(targets: list[str]):
        targets = _d(targets)
        try:
            protected = _d(rotation_core._resolve_protected_safe())
        except Exception:
            protected = []
        chunk = int(getattr(rotation_core, "DEFAULT_REGISTER_CHUNK_SIZE", 50) or 50)
        fixed_n = min(max(0, _i("PUSH_ROTATION_FIXED_SYMBOLS", 15)), chunk)
        fixed = _d(protected[:fixed_n])
        slots = max(0, chunk - len(fixed))
        fixed_set = set(fixed)
        pool = _d([x for x in protected[fixed_n:] if x not in fixed_set] + [x for x in targets if x not in fixed_set])
        a = _d(fixed + pool[:slots])[:chunk]
        b = _d(fixed + pool[slots:slots * 2])[:chunk]
        if fixed and not b and pool:
            b = _d(fixed + pool[:slots])[:chunk]
        logger.warning("[PUSH ROTATION STABILITY] batches fixed=%d pool=%d A=%d B=%d", len(fixed), len(pool), len(a), len(b))
        return a, b, fixed

    rotation_core._build_protected_rotation_batches = build
    rotation_core._STABILITY_PATCHED_BATCHES = True
    return True


def _patch_liq() -> bool:
    try:
        from . import rotation_symbols
    except Exception:
        logger.exception("[PUSH ROTATION STABILITY] rotation_symbols import failed")
        return False
    if getattr(rotation_symbols, "_STABILITY_PATCHED_LIQ_FAILOPEN", False):
        return True
    orig = rotation_symbols.apply_register_liquidity_guard

    def guard(targets: Sequence[str]) -> list[str]:
        try:
            cleaned, _, _, _ = rotation_symbols.clean_symbol_list(targets)
        except Exception:
            cleaned = _d(targets)
        if not cleaned or not _b("PUSH_ROTATION_LIQ_GUARD_FAILOPEN_ON_COLLAPSE", True):
            return orig(targets)
        filtered = orig(targets)
        try:
            fc, _, _, _ = rotation_symbols.clean_symbol_list(filtered)
        except Exception:
            fc = _d(filtered)
        min_keep = max(1, _i("PUSH_ROTATION_LIQ_GUARD_MIN_KEEP", 50))
        if len(cleaned) >= min_keep and len(fc) < min_keep:
            logger.warning("[PUSH ROTATION STABILITY] liquidity fail-open before=%d after=%d", len(cleaned), len(fc))
            return cleaned
        return fc

    rotation_symbols.apply_register_liquidity_guard = guard
    rotation_symbols._STABILITY_PATCHED_LIQ_FAILOPEN = True
    return True


def _patch_refresh() -> bool:
    try:
        import trading.push.subscription_manager as sm
        from trading.push.subscription_manager import core
    except Exception:
        logger.exception("[PUSH ROTATION STABILITY] subscription_manager import failed")
        return False
    if getattr(core, "_STABILITY_PATCHED_ROTATION_CLEAR", False):
        return True
    orig = core.refresh_subscriptions

    def refresh(symbols: Any = None, *, reason: str = "manual", force: bool = False,
                max_symbols: int = core.REGISTER_MAX_SYMBOLS, clear_first: Any = None,
                unregister_first: Any = None, **kwargs: Any) -> bool:
        target = None
        if _rot(reason) or _onopen(reason):
            target = core.build_target_symbols(symbols=symbols, max_symbols=max_symbols, reason=reason)
            target = core.enforce_register_limit(target, register_chunk_size=core.REGISTER_CHUNK_SIZE, reason=reason)
        if not _rot(reason):
            if _onopen(reason) and target:
                wait = max(0.0, _f("PUSH_ROTATION_UNREGISTER_WAIT_SEC", 0.2))
                logger.info("[PUSH ROTATION STABILITY] onopen clear/register reason=%s target=%d wait=%.3f", reason, len(target), wait)
                return orig(symbols=target, reason=reason, force=True, max_symbols=max_symbols,
                            clear_first=True, unregister_first=True,
                            wait_after_clear_sec=wait, unregister_wait_sec=wait, **kwargs)
            return orig(symbols=symbols, reason=reason, force=force, max_symbols=max_symbols,
                        clear_first=clear_first, unregister_first=unregister_first, **kwargs)
        if not target:
            return True
        with core.state.manager_lock:
            current = list(core.state.last_registered_symbols)
        wait = max(0.0, _f("PUSH_ROTATION_UNREGISTER_WAIT_SEC", 0.2))
        logger.info("[PUSH ROTATION STABILITY] rotate clear/register reason=%s current=%d target=%d wait=%.3f", reason, len(current), len(target), wait)
        ok = core.run_refresh_sequence(current_symbols=current, target_symbols=target,
                                       clear_first=True, unregister_first=True,
                                       wait_after_clear_sec=wait, unregister_wait_sec=wait)
        if ok:
            with core.state.manager_lock:
                core.state.last_registered_symbols = list(target)
                core.state.last_refresh_ts = core.now_ts()
                core.state.last_refresh_target_fingerprint = core.target_fingerprint(target)
            core.mark_reason(reason)
        return bool(ok)

    core.refresh_subscriptions = refresh
    sm.refresh_subscriptions = refresh
    core._STABILITY_PATCHED_ROTATION_CLEAR = True
    return True


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    ok0 = _patch_target_resolution()
    ok1 = _patch_batches()
    ok2 = _patch_liq()
    ok3 = _patch_refresh()
    _INSTALLED = bool(ok0 and ok1 and ok2 and ok3)
    logger.warning(
        "[PUSH ROTATION STABILITY] installed=%s target_topup=%s batches=%s liquidity_failopen=%s explicit_clear=%s version=%s",
        _INSTALLED,
        ok0,
        ok1,
        ok2,
        ok3,
        VERSION,
    )
    return _INSTALLED


install()
__all__ = ["VERSION", "install"]
