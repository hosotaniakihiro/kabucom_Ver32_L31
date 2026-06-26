# ============================================================
# File   : core/startup/kabusapi_token_retry_register_patch.py
# Version: V3-KABUSAPI-REGISTER-STARTUP-BOOTSTRAP-NO-RUNTIME-REFRESH
# ------------------------------------------------------------
# Purpose:
#   PUSH subscription register/unregister may run in child processes.
#   Operational policy is startup-once token handling:
#     - token is obtained once before live register/unregister and stored in settings.ini/runtime cache
#     - live register/unregister must NOT call /token automatically after startup/bootstrap
#     - 4001009 / APIキー不一致 must be surfaced as a real auth failure
#
#   This patch is loaded in the process that actually performs PUSH REST register.
#   Therefore it performs one defensive startup bootstrap before patching register_ops.
#   After that, it only normalizes token resolution/publishing from settings.ini/runtime cache.
# ============================================================
from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_BOOTSTRAPPED = False
_LOCK = threading.RLock()

_TOKEN_ENV_KEYS = (
    "KABU_API_KEY",
    "KABUSAPI_API_KEY",
    "X_API_KEY",
    "KABU_TOKEN",
    "KABUSAPI_TOKEN",
    "KABU_API_TOKEN",
    "AUKABU_TOKEN",
    "API_TOKEN",
    "TOKEN",
)


def _safe_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v).strip().strip('"').strip("'").strip()
    except Exception:
        return ""


def _env_bool(name: str, default: bool) -> bool:
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


def _is_api_key_mismatch(content: Any) -> bool:
    try:
        if isinstance(content, dict):
            code = str(content.get("Code") or "")
            msg = str(content.get("Message") or "")
            return code == "4001009" or "APIキー不一致" in msg
        s = str(content)
        return "4001009" in s or "APIキー不一致" in s
    except Exception:
        return False


def _publish_token(token: str) -> str:
    token = _safe_str(token)
    if not token:
        return ""
    for k in _TOKEN_ENV_KEYS:
        os.environ[k] = token
    try:
        import token_manager
        token_manager.API_TOKEN = token
    except Exception:
        pass
    try:
        from core.global_context import context as gc
        gd = getattr(gc, "global_data", None)
        if gd is not None:
            for name in ("kabu_api_key", "kabusapi_api_key", "api_key", "token", "kabu_token", "kabusapi_token", "X_API_KEY"):
                try:
                    setattr(gd, name, token)
                except Exception:
                    pass
    except Exception:
        pass
    return token


def _read_settings_or_runtime_token() -> str:
    """Return the already-issued startup token without calling /token."""
    try:
        import token_manager
        fn = getattr(token_manager, "get_valid_token", None)
        if callable(fn):
            token = _safe_str(fn())
            if token:
                return _publish_token(token)
        token = _safe_str(getattr(token_manager, "API_TOKEN", None))
        if token:
            return _publish_token(token)
    except Exception:
        pass

    for key in _TOKEN_ENV_KEYS:
        token = _safe_str(os.environ.get(key))
        if token:
            return _publish_token(token)
    return ""


def _bootstrap_token_once() -> str:
    """Acquire one fresh token before PUSH REST registration starts.

    This is not runtime retry.  It runs once when this startup patch is installed in
    the process that will call /kabusapi/register.  It uses token_manager.refresh_token's
    original function when another startup-once wrapper is already installed.
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return _read_settings_or_runtime_token()
    _BOOTSTRAPPED = True

    if not _env_bool("KABU_REGISTER_BOOTSTRAP_TOKEN_ON_INSTALL", True):
        token = _read_settings_or_runtime_token()
        logger.warning(
            "[KABUSAPI TOKEN RETRY REGISTER] bootstrap disabled; using existing token token_len=%d",
            len(token),
        )
        return token

    try:
        import token_manager
        refresh = getattr(token_manager, "refresh_token", None)
        if callable(refresh):
            original = getattr(refresh, "_original", refresh)
            token = _safe_str(original())
            if token:
                _publish_token(token)
                os.environ["KABU_REGISTER_BOOTSTRAP_DONE"] = "1"
                logger.warning(
                    "[KABUSAPI TOKEN RETRY REGISTER] startup bootstrap token refreshed/saved before register token_len=%d",
                    len(token),
                )
                return token
            logger.error("[KABUSAPI TOKEN RETRY REGISTER] startup bootstrap returned empty token before register")
        else:
            logger.error("[KABUSAPI TOKEN RETRY REGISTER] token_manager.refresh_token missing; cannot bootstrap before register")
    except Exception:
        logger.exception("[KABUSAPI TOKEN RETRY REGISTER] startup bootstrap token refresh failed before register")

    token = _read_settings_or_runtime_token()
    logger.warning(
        "[KABUSAPI TOKEN RETRY REGISTER] fallback to existing token after bootstrap failure token_len=%d",
        len(token),
    )
    return token


def _install_now() -> bool:
    global _INSTALLED
    with _LOCK:
        if _INSTALLED:
            return True
        try:
            import trading.push.subscription_manager.register_ops as ops
        except Exception:
            logger.debug("[KABUSAPI TOKEN RETRY REGISTER] register_ops not ready", exc_info=True)
            return False

        if getattr(ops, "_kabusapi_token_retry_register_v3", False):
            _INSTALLED = True
            return True

        orig_resolve = getattr(ops, "_resolve_api_key", None)
        orig_http = getattr(ops, "_http_json_request", None)
        if not callable(orig_resolve) or not callable(orig_http):
            return False

        _bootstrap_token_once()

        def _resolve_api_key_patched(*args: Any, **kwargs: Any) -> str:
            token = _read_settings_or_runtime_token()
            if token:
                return token
            try:
                token = _safe_str(orig_resolve(*args, **kwargs))
                if token:
                    return _publish_token(token)
            except Exception:
                logger.debug("[KABUSAPI TOKEN RETRY REGISTER] original _resolve_api_key failed", exc_info=True)
            logger.warning("[KABUSAPI TOKEN RETRY REGISTER] no startup/settings.ini token available; runtime refresh disabled")
            return ""

        def _http_json_request_patched(*, url: str, method: str, payload: Any, api_key: str, timeout: float = 10.0):
            token = _safe_str(api_key) or _resolve_api_key_patched()
            ok, content = orig_http(url=url, method=method, payload=payload, api_key=token, timeout=timeout)
            if ok or not _is_api_key_mismatch(content):
                return ok, content
            logger.error(
                "[KABUSAPI TOKEN RETRY REGISTER] API key mismatch method=%s url=%s; runtime refresh/retry disabled by startup-once policy content=%r",
                method,
                url,
                content,
            )
            return ok, content

        _resolve_api_key_patched._kabusapi_token_retry_register_v3 = True  # type: ignore[attr-defined]
        _resolve_api_key_patched._original = orig_resolve  # type: ignore[attr-defined]
        _http_json_request_patched._kabusapi_token_retry_register_v3 = True  # type: ignore[attr-defined]
        _http_json_request_patched._original = orig_http  # type: ignore[attr-defined]

        ops._resolve_api_key = _resolve_api_key_patched
        ops._http_json_request = _http_json_request_patched
        ops._kabusapi_token_retry_register_v3 = True
        _INSTALLED = True
        logger.warning("[KABUSAPI TOKEN RETRY REGISTER] installed v3 startup_bootstrap=True no_runtime_refresh=True")
        return True


def install() -> bool:
    return _install_now()


try:
    install()
except Exception:
    logger.exception("[KABUSAPI TOKEN RETRY REGISTER] auto install failed")


__all__ = ["install"]
