# ============================================================
# File   : trading/push/push_stream/rotation_stability_patch.py
# Version: V1.0-PUSH-ROTATION-STABILITY-FAILOPEN
# ------------------------------------------------------------
# Runtime hardening for PUSH A/B rotation.
#
# Fixes:
#   1. rotation_* refresh must not force unregister_all on every turn.
#      unregister/register every 4.8s can trigger kabu Station WinError 10054.
#   2. When no protected/fixed symbols exist, A/B must be 50/50, not 35/35.
#      If fixed symbols are fewer than 15, fill the remaining chunk with variable symbols.
#   3. PUSH rotation liquidity guard must fail-open when it collapses 100 candidates
#      below one register chunk. Low-liquidity rejection remains in entry-side guards.
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

VERSION = "V1.0-PUSH-ROTATION-STABILITY-FAILOPEN"
_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _dedupe(items: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items or []:
        s = str(x).strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _is_rotation_reason(reason: Any) -> bool:
    try:
        s = str(reason or "").strip().lower()
        return s.startswith("rotation_") or s.startswith("push_rotation_") or s in {"rotation", "rotate"}
    except Exception:
        return False


def _patch_rotation_batches() -> bool:
    try:
        from . import rotation_core
    except Exception:
        logger.exception("[PUSH ROTATION STABILITY] import rotation_core failed")
        return False

    if getattr(rotation_core, "_STABILITY_PATCHED_BATCHES", False):
        return True

    def _build_protected_rotation_batches_patched(targets: list[str]):
        targets = _dedupe(targets)
        try:
            protected_all = _dedupe(rotation_core._resolve_protected_safe())
        except Exception:
            logger.exception("[PUSH ROTATION STABILITY] protected resolve failed")
            protected_all = []

        chunk = max(1, int(getattr(rotation_core, "DEFAULT_REGISTER_CHUNK_SIZE", 50) or 50))
        configured_fixed = max(0, _env_int("PUSH_ROTATION_FIXED_SYMBOLS", 15))
        configured_fixed = min(configured_fixed, chunk)

        # 実際に存在するprotectedだけを固定枠にする。
        # fixed=0なのに variable=35 のままだと A=35/B=35 になって50枠を使い切れない。
        fixed = _dedupe(protected_all[:configured_fixed])
        fixed_slots_actual = len(fixed)
        variable_slots = max(0, chunk - fixed_slots_actual)

        fixed_set = set(fixed)
        protected_overflow = [x for x in protected_all[configured_fixed:] if x not in fixed_set]
        normal_from_targets = [x for x in targets if x not in fixed_set]
        variable_pool = _dedupe(protected_overflow + normal_from_targets)

        first = _dedupe(fixed + variable_pool[:variable_slots])[:chunk]
        second = _dedupe(fixed + variable_pool[variable_slots:variable_slots * 2])[:chunk]

        if fixed and not second and variable_pool:
            second = _dedupe(fixed + variable_pool[:variable_slots])[:chunk]

        logger.warning(
            "[PUSH ROTATION STABILITY] batches fixed=%d configured_fixed=%d variable_slots=%d "
            "protected_total=%d protected_overflow=%d variable_pool=%d A=%d B=%d head_fixed=%s headA=%s headB=%s",
            len(fixed),
            configured_fixed,
            variable_slots,
            len(protected_all),
            len(protected_overflow),
            len(variable_pool),
            len(first),
            len(second),
            fixed[:15],
            first[:15],
            second[:15],
        )
        return first, second, fixed

    rotation_core._build_protected_rotation_batches = _build_protected_rotation_batches_patched  # type: ignore[attr-defined]
    rotation_core._STABILITY_PATCHED_BATCHES = True  # type: ignore[attr-defined]
    return True


def _patch_liquidity_guard_failopen() -> bool:
    try:
        from . import rotation_symbols
    except Exception:
        logger.exception("[PUSH ROTATION STABILITY] import rotation_symbols failed")
        return False

    if getattr(rotation_symbols, "_STABILITY_PATCHED_LIQ_FAILOPEN", False):
        return True

    original = rotation_symbols.apply_register_liquidity_guard

    def apply_register_liquidity_guard_patched(targets: Sequence[str]) -> list[str]:
        try:
            cleaned, _, _, _ = rotation_symbols.clean_symbol_list(targets)
        except Exception:
            cleaned = _dedupe(targets)

        if not cleaned:
            return original(targets)

        if not _env_bool("PUSH_ROTATION_LIQ_GUARD_FAILOPEN_ON_COLLAPSE", True):
            return original(targets)

        filtered = original(targets)
        try:
            filtered_clean, _, _, _ = rotation_symbols.clean_symbol_list(filtered)
        except Exception:
            filtered_clean = _dedupe(filtered)

        min_keep = max(1, _env_int("PUSH_ROTATION_LIQ_GUARD_MIN_KEEP", int(getattr(rotation_symbols, "DEFAULT_REGISTER_CHUNK_SIZE", 50) or 50)))

        # 100候補が30候補に崩れると A/B が成立しないため、PUSH登録ではfail-open。
        # 低流動性の最終除外はentry側のガードで実施する。
        if len(cleaned) >= min_keep and len(filtered_clean) < min_keep:
            logger.warning(
                "[PUSH ROTATION STABILITY] liquidity collapse fail-open before=%d after=%d min_keep=%d head=%s filtered_head=%s",
                len(cleaned),
                len(filtered_clean),
                min_keep,
                cleaned[:10],
                filtered_clean[:10],
            )
            return cleaned

        return filtered_clean

    rotation_symbols.apply_register_liquidity_guard = apply_register_liquidity_guard_patched  # type: ignore[assignment]
    rotation_symbols._STABILITY_PATCHED_LIQ_FAILOPEN = True  # type: ignore[attr-defined]
    return True


def _patch_subscription_rotation_register_only() -> bool:
    try:
        import trading.push.subscription_manager as sm
        from trading.push.subscription_manager import core
    except Exception:
        logger.exception("[PUSH ROTATION STABILITY] import subscription_manager failed")
        return False

    if getattr(core, "_STABILITY_PATCHED_ROTATION_REGISTER_ONLY", False):
        return True

    original_refresh = core.refresh_subscriptions

    def refresh_subscriptions_patched(
        symbols: Any = None,
        *,
        reason: str = "manual",
        force: bool = False,
        max_symbols: int = core.REGISTER_MAX_SYMBOLS,
        clear_first: Any = None,
        unregister_first: Any = None,
        **kwargs: Any,
    ) -> bool:
        if not _is_rotation_reason(reason):
            return original_refresh(
                symbols=symbols,
                reason=reason,
                force=force,
                max_symbols=max_symbols,
                clear_first=clear_first,
                unregister_first=unregister_first,
                **kwargs,
            )

        try:
            target_symbols = core.build_target_symbols(
                symbols=symbols,
                max_symbols=max_symbols,
                reason=reason,
            )
            target_symbols = core.enforce_register_limit(
                target_symbols,
                register_chunk_size=core.REGISTER_CHUNK_SIZE,
                reason=reason,
            )

            with core.state.manager_lock:
                current_symbols = list(core.state.last_registered_symbols)

            if len(target_symbols) == 0:
                logger.warning(
                    "[SUB MANAGER CORE] rotation register-only skip empty keep current reason=%s current=%d",
                    reason,
                    len(current_symbols),
                )
                return True

            stats = core.refresh_change_stats(current_symbols, target_symbols)
            removed = stats["removed"]
            added = stats["added"]
            diff_ratio = stats["ratio"]

            # rotationでは unregister_all を禁止。register-onlyで50件を上書きする。
            decided_clear_first = False
            effective_unregister_first = False
            wait_after_clear_sec = 0.0
            clear_policy = "rotation_register_only_no_unregister"

            logger.info(
                "[SUB MANAGER CORE] refresh start reason=%s force=%s clear_first=%s unregister_first=%s clear_policy=%s "
                "current=%d target=%d removed=%d added=%d diff_ratio=%.3f wait_after_clear=%.3fs rotation=%s reconnect=%s force_clear=%s",
                reason,
                True,
                decided_clear_first,
                effective_unregister_first,
                clear_policy,
                len(current_symbols),
                len(target_symbols),
                len(removed),
                len(added),
                diff_ratio,
                wait_after_clear_sec,
                True,
                False,
                False,
            )

            core.log_kabustation_register_symbols(
                target_symbols,
                current_symbols=current_symbols,
                added_symbols=added,
                removed_symbols=removed,
                reason=reason,
            )

            ok = core.run_refresh_sequence(
                current_symbols=current_symbols,
                target_symbols=target_symbols,
                clear_first=decided_clear_first,
                unregister_first=effective_unregister_first,
                wait_after_clear_sec=wait_after_clear_sec,
                unregister_wait_sec=wait_after_clear_sec,
            )

            if ok:
                with core.state.manager_lock:
                    core.state.last_registered_symbols = list(target_symbols)
                    core.state.last_refresh_ts = core.now_ts()
                    core.state.last_refresh_target_fingerprint = core.target_fingerprint(target_symbols)

                core.mark_reason(reason)
                logger.info(
                    "[SUB MANAGER CORE] refresh done reason=%s current=%d target=%d rotation=True reconnect=False force_clear=False wait_after_clear=%.3fs policy=%s",
                    reason,
                    len(current_symbols),
                    len(target_symbols),
                    wait_after_clear_sec,
                    clear_policy,
                )
                return True

            logger.warning(
                "[SUB MANAGER CORE] refresh failed reason=%s current=%d target=%d removed=%d added=%d diff_ratio=%.3f rotation=True reconnect=False force_clear=False policy=%s",
                reason,
                len(current_symbols),
                len(target_symbols),
                len(removed),
                len(added),
                diff_ratio,
                clear_policy,
            )
            return False

        except Exception:
            logger.exception("[PUSH ROTATION STABILITY] patched rotation refresh failed -> fallback original reason=%s", reason)
            return original_refresh(
                symbols=symbols,
                reason=reason,
                force=force,
                max_symbols=max_symbols,
                clear_first=False,
                unregister_first=False,
                wait_after_clear_sec=0.0,
                unregister_wait_sec=0.0,
                **kwargs,
            )

    core.refresh_subscriptions = refresh_subscriptions_patched  # type: ignore[assignment]
    sm.refresh_subscriptions = refresh_subscriptions_patched  # type: ignore[attr-defined]
    core._STABILITY_PATCHED_ROTATION_REGISTER_ONLY = True  # type: ignore[attr-defined]
    return True


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    ok_batches = _patch_rotation_batches()
    ok_liq = _patch_liquidity_guard_failopen()
    ok_sub = _patch_subscription_rotation_register_only()

    _INSTALLED = bool(ok_batches and ok_liq and ok_sub)
    logger.warning(
        "[PUSH ROTATION STABILITY] installed=%s batches=%s liquidity_failopen=%s register_only=%s version=%s",
        _INSTALLED,
        ok_batches,
        ok_liq,
        ok_sub,
        VERSION,
    )
    return _INSTALLED


install()

__all__ = ["VERSION", "install"]
