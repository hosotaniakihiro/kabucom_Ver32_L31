# -*- coding: utf-8 -*-
"""
PUSH registration recovery patch.

Current production policy:

1. Token/auth handling is owned by:
   - main_database.py parent preflight
   - token_manager.py parent-only refresh guard
   - core.startup.kabusapi_token_retry_register_patch v5 canonical token wrapper

2. This patch must NOT wrap register_ops._http_json_request, must NOT sync tokens,
   and must NOT refresh/retry on 4001009 / APIキー不一致.

3. This patch only keeps the safe parts:
   - audit logs around register target/sequence
   - top-up of PUSH targets to 100 symbols before A/B splitting

Reason:
Older versions of this patch re-wrapped _http_json_request after the canonical token
wrapper and could overwrite or retry token handling in child processes.  That made
PUSH registration diagnostics ambiguous and could fight the startup-once token policy.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

VERSION = "V4-PUSH-REGISTER-RECOVERY-NO-TOKEN-WRAPPER"
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
        return bool(default)
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


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items or []:
        s = str(x or "").strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _safe_clean_symbols(rs: Any, values: Any) -> list[str]:
    try:
        cleaned, *_ = rs.clean_symbol_list(values)
        return list(cleaned or [])
    except Exception:
        try:
            return _dedupe([str(x) for x in list(values or [])])
        except Exception:
            return []


def _split_ab_counts(symbols: Sequence[str]) -> tuple[int, int, list[str], list[str]]:
    chunk = max(1, _env_int("PUSH_REGISTER_CHUNK_SIZE", 50))
    a = list(symbols or [])[:chunk]
    b = list(symbols or [])[chunk:chunk * 2]
    return len(a), len(b), a[:10], b[:10]


def _patch_register_ops() -> bool:
    try:
        from trading.push.subscription_manager import register_ops as ro  # type: ignore
    except Exception:
        logger.exception("[PUSH REGISTER RECOVERY] import register_ops failed")
        return False

    ok_any = False

    # IMPORTANT: Do not patch ro._http_json_request here.
    # Token handling must remain owned by kabusapi_token_retry_register_patch v5.
    if not getattr(ro, "_PUSH_REGISTER_RECOVERY_HTTP_DISABLED_V4", False):
        ro._PUSH_REGISTER_RECOVERY_HTTP_DISABLED_V4 = True  # type: ignore[attr-defined]
        ro._PUSH_REGISTER_RECOVERY_HTTP_PATCHED = False  # type: ignore[attr-defined]
        logger.warning("[PUSH REGISTER RECOVERY] register_ops auth retry/preflight token sync disabled; canonical token wrapper owns HTTP")

    if not getattr(ro, "_PUSH_REGISTER_RECOVERY_SEQUENCE_AUDIT_PATCHED", False):
        orig_refresh = getattr(ro, "run_refresh_sequence", None)
        orig_chunks = getattr(ro, "run_register_chunks", None)

        if callable(orig_refresh):
            def run_refresh_sequence_patched(current_symbols, target_symbols, *args, **kwargs):
                target = _dedupe([str(x) for x in list(target_symbols or [])])
                a_count, b_count, a_head, b_head = _split_ab_counts(target)
                t0 = time.monotonic()
                logger.warning(
                    "[PUSH REGISTER RECOVERY][REGISTER AUDIT] refresh start current=%s target=%s A=%s B=%s clear_first=%s unregister_first=%s target_head=%s A_head=%s B_head=%s",
                    len(current_symbols or []), len(target), a_count, b_count,
                    kwargs.get("clear_first"), kwargs.get("unregister_first"), target[:10], a_head, b_head,
                )
                ok = orig_refresh(current_symbols, target_symbols, *args, **kwargs)
                logger.warning(
                    "[PUSH REGISTER RECOVERY][REGISTER AUDIT] refresh done ok=%s elapsed=%.3fs target=%s A=%s B=%s",
                    ok, time.monotonic() - t0, len(target), a_count, b_count,
                )
                return ok
            ro.run_refresh_sequence = run_refresh_sequence_patched  # type: ignore[attr-defined]
            ok_any = True

        if callable(orig_chunks):
            def run_register_chunks_patched(symbols, *args, **kwargs):
                target = _dedupe([str(x) for x in list(symbols or [])])
                a_count, b_count, a_head, b_head = _split_ab_counts(target)
                t0 = time.monotonic()
                logger.warning(
                    "[PUSH REGISTER RECOVERY][REGISTER AUDIT] register_chunks start target=%s A=%s B=%s head=%s A_head=%s B_head=%s",
                    len(target), a_count, b_count, target[:10], a_head, b_head,
                )
                ok = orig_chunks(symbols, *args, **kwargs)
                logger.warning(
                    "[PUSH REGISTER RECOVERY][REGISTER AUDIT] register_chunks done ok=%s elapsed=%.3fs target=%s A=%s B=%s",
                    ok, time.monotonic() - t0, len(target), a_count, b_count,
                )
                return ok
            ro.run_register_chunks = run_register_chunks_patched  # type: ignore[attr-defined]
            ok_any = True

        ro._PUSH_REGISTER_RECOVERY_SEQUENCE_AUDIT_PATCHED = True  # type: ignore[attr-defined]
        logger.warning("[PUSH REGISTER RECOVERY] register_ops sequence audit patched")

    return ok_any or bool(getattr(ro, "_PUSH_REGISTER_RECOVERY_SEQUENCE_AUDIT_PATCHED", False))


def _extend_symbols(rs: Any, base: Sequence[str], extra: Sequence[str], limit: int) -> list[str]:
    cleaned = _safe_clean_symbols(rs, extra)
    merged = _dedupe([*(base or []), *cleaned])
    return merged[:limit]


def _patch_rotation_symbols() -> bool:
    try:
        from trading.push.push_stream import rotation_symbols as rs  # type: ignore
    except Exception:
        logger.exception("[PUSH REGISTER RECOVERY] import rotation_symbols failed")
        return False

    ok_any = False

    if not getattr(rs, "_PUSH_REGISTER_TARGET_TOPUP_PATCHED", False):
        orig_resolve = getattr(rs, "resolve_monitor_symbols", None)
        if callable(orig_resolve):
            def resolve_monitor_symbols_patched():
                min_keep = max(1, _env_int("PUSH_REGISTER_MIN_KEEP", 100))
                max_keep = max(min_keep, _env_int("PUSH_REGISTER_MAX_KEEP", getattr(rs, "DEFAULT_REGISTER_MAX_SYMBOLS", 100)))
                if not _env_bool("PUSH_REGISTER_TARGET_TOPUP_ENABLED", True):
                    return orig_resolve()

                try:
                    base = orig_resolve()
                except Exception:
                    logger.exception("[PUSH REGISTER RECOVERY] original resolve_monitor_symbols failed")
                    base = []
                base = _safe_clean_symbols(rs, base)

                if len(base) >= min_keep:
                    out = base[:max_keep]
                    a_count, b_count, a_head, b_head = _split_ab_counts(out)
                    logger.warning(
                        "[PUSH REGISTER RECOVERY][TARGET AUDIT] monitor resolved enough total=%s min_keep=%s max_keep=%s A=%s B=%s head=%s A_head=%s B_head=%s",
                        len(out), min_keep, max_keep, a_count, b_count, out[:10], a_head, b_head,
                    )
                    return out

                before = len(base)
                sources: list[tuple[str, Any]] = []
                for name in ("_resolve_from_global_data", "_resolve_from_dynamic_providers"):
                    fn = getattr(rs, name, None)
                    if callable(fn):
                        sources.append((name, fn))

                merged = list(base)
                for source_name, fn in sources:
                    if len(merged) >= min_keep:
                        break
                    try:
                        extra = fn()
                        old = len(merged)
                        merged = _extend_symbols(rs, merged, extra, max_keep)
                        logger.warning(
                            "[PUSH REGISTER RECOVERY] target topup source=%s before=%d after=%d min_keep=%d head=%s",
                            source_name, old, len(merged), min_keep, merged[:10],
                        )
                    except Exception:
                        logger.exception("[PUSH REGISTER RECOVERY] target topup failed source=%s", source_name)

                if len(merged) < min_keep:
                    logger.warning(
                        "[PUSH REGISTER RECOVERY] target topup insufficient before=%d after=%d min_keep=%d max_keep=%d head=%s",
                        before, len(merged), min_keep, max_keep, merged[:10],
                    )
                else:
                    logger.warning(
                        "[PUSH REGISTER RECOVERY] target topup ok before=%d after=%d min_keep=%d max_keep=%d",
                        before, len(merged), min_keep, max_keep,
                    )
                out = merged[:max_keep]
                a_count, b_count, a_head, b_head = _split_ab_counts(out)
                logger.warning(
                    "[PUSH REGISTER RECOVERY][TARGET AUDIT] monitor final total=%s A=%s B=%s head=%s A_head=%s B_head=%s",
                    len(out), a_count, b_count, out[:10], a_head, b_head,
                )
                return out

            rs.resolve_monitor_symbols = resolve_monitor_symbols_patched  # type: ignore[attr-defined]
            rs._PUSH_REGISTER_TARGET_TOPUP_PATCHED = True  # type: ignore[attr-defined]
            logger.warning("[PUSH REGISTER RECOVERY] rotation symbol target topup patched")
            ok_any = True

    if not getattr(rs, "_PUSH_REGISTER_RESOLVE_TARGET_AUDIT_PATCHED", False):
        orig_targets = getattr(rs, "resolve_register_targets", None)
        if callable(orig_targets):
            def resolve_register_targets_patched():
                t0 = time.monotonic()
                targets = orig_targets()
                targets_clean = _safe_clean_symbols(rs, targets)
                a_count, b_count, a_head, b_head = _split_ab_counts(targets_clean)
                logger.warning(
                    "[PUSH REGISTER RECOVERY][TARGET AUDIT] resolve_register_targets done total=%s A=%s B=%s elapsed=%.3fs head=%s A_head=%s B_head=%s",
                    len(targets_clean), a_count, b_count, time.monotonic() - t0, targets_clean[:10], a_head, b_head,
                )
                return targets
            rs.resolve_register_targets = resolve_register_targets_patched  # type: ignore[attr-defined]
            rs._PUSH_REGISTER_RESOLVE_TARGET_AUDIT_PATCHED = True  # type: ignore[attr-defined]
            logger.warning("[PUSH REGISTER RECOVERY] rotation resolve_register_targets audit patched")
            ok_any = True

    return ok_any or bool(getattr(rs, "_PUSH_REGISTER_TARGET_TOPUP_PATCHED", False))


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _env_bool("PUSH_REGISTER_RECOVERY_PATCH_ENABLED", True):
        logger.warning("[PUSH REGISTER RECOVERY] disabled by env")
        return False

    # Keep env flags explicit so later code does not re-enable runtime token behavior.
    os.environ.setdefault("PUSH_REGISTER_PREFLIGHT_TOKEN_SYNC", "0")
    os.environ.setdefault("PUSH_REGISTER_AUTH_RETRY_ENABLED", "0")

    ok_register = _patch_register_ops()
    ok_symbols = _patch_rotation_symbols()
    _INSTALLED = bool(ok_register or ok_symbols)
    logger.warning(
        "[PUSH REGISTER RECOVERY] installed version=%s register_audit=%s http_token_wrapper=False auth_retry=False preflight_token_sync=False target_topup=%s min_keep=%s audit=True",
        VERSION,
        ok_register,
        ok_symbols,
        _env_int("PUSH_REGISTER_MIN_KEEP", 100),
    )
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[PUSH REGISTER RECOVERY] auto install failed")


__all__ = ["VERSION", "install"]
