# -*- coding: utf-8 -*-
"""
PUSH registration recovery patch.

Fixes production failure modes observed in PUSH rotation:

1. kabusapi register/unregister_all returns Code=4001009 / APIキー不一致.
   The original subscription_manager.register_ops sent one request with the
   currently resolved X-API-KEY and returned False.  This patch now does a
   lightweight preflight token sync before register/unregister so the first
   request uses the newest token when another process has refreshed it.  If an
   auth error still occurs, it retries once after token_manager.refresh_token().
   It also updates common env/global slots so subsequent calls resolve the same
   token.

2. PUSH rotation uses the first runtime symbol list even when it has already
   shrunk to a partial list such as 21 symbols.  This patch tops up the list
   from global_data and dynamic providers up to PUSH_REGISTER_MIN_KEEP
   (default 100) before A/B splitting.

3. When the WebSocket becomes stale and reconnects, we need clear evidence that
   the next register cycle really rebuilt the 100-symbol pool and sent a 50-name
   A/B turn.  V3 adds audit logs around resolve_register_targets(),
   run_register_chunks(), and run_refresh_sequence().
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

VERSION = "V3-PUSH-REGISTER-RECOVERY-AUDIT"
_INSTALLED = False
_LAST_PREFLIGHT_TOKEN = ""
_LAST_PREFLIGHT_TS = 0.0


def _env_bool(name: str, default: bool = True) -> bool:
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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _is_auth_error(content: Any) -> bool:
    try:
        if isinstance(content, dict):
            code = str(content.get("Code") or content.get("code") or "")
            msg = str(content.get("Message") or content.get("message") or "")
            return code in {"4001009", "401"} or "APIキー不一致" in msg or "Unauthorized" in msg
        s = str(content)
        return "4001009" in s or "APIキー不一致" in s or "401" in s or "Unauthorized" in s
    except Exception:
        return False


def _publish_token(token: str) -> None:
    token = str(token or "").strip()
    if not token:
        return
    for key in ("KABU_API_KEY", "KABUSAPI_API_KEY", "KABUSAPI_TOKEN", "KABU_TOKEN", "X_API_KEY"):
        os.environ[key] = token
    try:
        import token_manager  # type: ignore
        try:
            token_manager.API_TOKEN = token
        except Exception:
            pass
    except Exception:
        pass
    for module_name, attr_name in (("global_state", "global_data"), ("core.global_context.context", "global_data")):
        try:
            mod = __import__(module_name, fromlist=[attr_name])
            gd = getattr(mod, attr_name, None)
            if gd is None:
                continue
            for name in ("kabu_api_key", "kabusapi_api_key", "api_key", "token", "kabu_token", "kabusapi_token"):
                try:
                    setattr(gd, name, token)
                except Exception:
                    pass
            try:
                headers = getattr(gd, "headers", None)
                if isinstance(headers, dict):
                    headers["X-API-KEY"] = token
            except Exception:
                pass
        except Exception:
            continue


def _get_cached_or_valid_token() -> str:
    global _LAST_PREFLIGHT_TOKEN, _LAST_PREFLIGHT_TS
    now = time.monotonic()
    min_interval = max(0.0, _env_float("PUSH_REGISTER_PREFLIGHT_TOKEN_MIN_INTERVAL_SEC", 1.0))
    if _LAST_PREFLIGHT_TOKEN and (now - _LAST_PREFLIGHT_TS) < min_interval:
        return _LAST_PREFLIGHT_TOKEN

    token = ""
    try:
        import token_manager  # type: ignore
        fn = getattr(token_manager, "get_valid_token", None)
        if callable(fn):
            token = str(fn() or "").strip()
        if not token:
            token = str(getattr(token_manager, "API_TOKEN", "") or "").strip()
    except Exception:
        token = ""

    if not token:
        for key in ("KABU_API_KEY", "KABUSAPI_API_KEY", "KABUSAPI_TOKEN", "KABU_TOKEN", "X_API_KEY"):
            token = str(os.environ.get(key) or "").strip()
            if token:
                break

    if token:
        _LAST_PREFLIGHT_TOKEN = token
        _LAST_PREFLIGHT_TS = now
        _publish_token(token)
    return token


def _refresh_token_once() -> str:
    global _LAST_PREFLIGHT_TOKEN, _LAST_PREFLIGHT_TS
    try:
        import token_manager  # type: ignore
        token = token_manager.refresh_token()
        token = str(token or "").strip()
        if token:
            _LAST_PREFLIGHT_TOKEN = token
            _LAST_PREFLIGHT_TS = time.monotonic()
            _publish_token(token)
            logger.warning("[PUSH REGISTER RECOVERY] refreshed kabusapi token for register/unregister token_len=%d", len(token))
            return token
    except Exception:
        logger.exception("[PUSH REGISTER RECOVERY] token refresh failed")
    return ""


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

    if not getattr(ro, "_PUSH_REGISTER_RECOVERY_HTTP_PATCHED", False):
        orig_http = getattr(ro, "_http_json_request", None)
        if callable(orig_http):
            def _http_json_request_patched(*, url: str, method: str, payload: Any, api_key: str, timeout: float = 10.0):
                request_token = str(api_key or "").strip()
                if _env_bool("PUSH_REGISTER_PREFLIGHT_TOKEN_SYNC", True):
                    synced = _get_cached_or_valid_token()
                    if synced and synced != request_token:
                        logger.info(
                            "[PUSH REGISTER RECOVERY] preflight token sync method=%s url=%s old_len=%d new_len=%d",
                            method, url, len(request_token), len(synced),
                        )
                        request_token = synced

                ok, content = orig_http(url=url, method=method, payload=payload, api_key=request_token, timeout=timeout)
                if ok or not _is_auth_error(content):
                    return ok, content
                if not _env_bool("PUSH_REGISTER_AUTH_RETRY_ENABLED", True):
                    return ok, content
                logger.warning(
                    "[PUSH REGISTER RECOVERY] auth error on %s %s -> refresh token and retry once content=%r",
                    method, url, content,
                )
                new_token = _refresh_token_once()
                if not new_token:
                    return ok, content
                ok2, content2 = orig_http(url=url, method=method, payload=payload, api_key=new_token, timeout=timeout)
                logger.warning("[PUSH REGISTER RECOVERY] retry result method=%s ok=%s content=%r", method, ok2, content2)
                return ok2, content2

            ro._http_json_request = _http_json_request_patched  # type: ignore[attr-defined]
            ro._PUSH_REGISTER_RECOVERY_HTTP_PATCHED = True  # type: ignore[attr-defined]
            logger.warning("[PUSH REGISTER RECOVERY] register_ops auth retry + preflight token sync patched")
            ok_any = True

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

        ro._PUSH_REGISTER_RECOVERY_SEQUENCE_AUDIT_PATCHED = True  # type: ignore[attr-defined]
        logger.warning("[PUSH REGISTER RECOVERY] register_ops sequence audit patched")
        ok_any = True

    return ok_any or bool(getattr(ro, "_PUSH_REGISTER_RECOVERY_HTTP_PATCHED", False))


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
    ok_register = _patch_register_ops()
    ok_symbols = _patch_rotation_symbols()
    _INSTALLED = bool(ok_register or ok_symbols)
    logger.warning(
        "[PUSH REGISTER RECOVERY] installed version=%s register_retry=%s preflight_token_sync=%s target_topup=%s min_keep=%s audit=%s",
        VERSION,
        ok_register,
        _env_bool("PUSH_REGISTER_PREFLIGHT_TOKEN_SYNC", True),
        ok_symbols,
        _env_int("PUSH_REGISTER_MIN_KEEP", 100),
        True,
    )
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[PUSH REGISTER RECOVERY] auto install failed")


__all__ = ["VERSION", "install"]
