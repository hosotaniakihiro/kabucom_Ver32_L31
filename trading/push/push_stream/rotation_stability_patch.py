from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)
VERSION = "V1.1-ROTATION-CLEAR-AB50"
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
        if not _rot(reason):
            return orig(symbols=symbols, reason=reason, force=force, max_symbols=max_symbols,
                        clear_first=clear_first, unregister_first=unregister_first, **kwargs)
        target = core.build_target_symbols(symbols=symbols, max_symbols=max_symbols, reason=reason)
        target = core.enforce_register_limit(target, register_chunk_size=core.REGISTER_CHUNK_SIZE, reason=reason)
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
    ok1 = _patch_batches()
    ok2 = _patch_liq()
    ok3 = _patch_refresh()
    _INSTALLED = bool(ok1 and ok2 and ok3)
    logger.warning("[PUSH ROTATION STABILITY] installed=%s batches=%s liquidity_failopen=%s explicit_clear=%s version=%s", _INSTALLED, ok1, ok2, ok3, VERSION)
    return _INSTALLED


install()
__all__ = ["VERSION", "install"]
