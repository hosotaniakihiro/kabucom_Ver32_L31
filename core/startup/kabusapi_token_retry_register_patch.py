# ============================================================
# File   : core/startup/kabusapi_token_retry_register_patch.py
# Version: V1-KABUSAPI-REGISTER-4001009-TOKEN-REFRESH
# ------------------------------------------------------------
# Purpose:
#   PUSH subscription register/unregister may run in child processes.
#   If an old X-API-KEY remains in env/global state, kabu Station returns
#   Code 4001009 / APIキー不一致.  Patch register_ops so register APIs
#   prefer a freshly refreshed runtime token and retry once on 4001009.
# ============================================================
from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_LOCK = threading.RLock()

_TOKEN_ENV_KEYS = (
    "KABU_API_KEY",
    "KABUSAPI_API_KEY",
    "X_API_KEY",
    "KABU_TOKEN",
    "KABUSAPI_TOKEN",
)


def _safe_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v).strip().strip('"').strip("'").strip()
    except Exception:
        return ""


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


def _refresh_token(reason: str = "register_4001009") -> str:
    try:
        import token_manager
        token = _safe_str(token_manager.refresh_token())
        if token:
            _publish_token(token)
            logger.warning("[KABUSAPI TOKEN RETRY REGISTER] refreshed token reason=%s token_len=%d", reason, len(token))
            return token
    except Exception:
        logger.exception("[KABUSAPI TOKEN RETRY REGISTER] refresh_token failed reason=%s", reason)
    return ""


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

        if getattr(ops, "_kabusapi_token_retry_register_v1", False):
            _INSTALLED = True
            return True

        orig_resolve = getattr(ops, "_resolve_api_key", None)
        orig_http = getattr(ops, "_http_json_request", None)
        if not callable(orig_resolve) or not callable(orig_http):
            return False

        def _resolve_api_key_patched(*args: Any, **kwargs: Any) -> str:
            # Prefer in-memory token_manager token first.  If empty, use original resolver.
            try:
                import token_manager
                token = _safe_str(getattr(token_manager, "API_TOKEN", None))
                if token:
                    return _publish_token(token)
            except Exception:
                pass
            try:
                token = _safe_str(orig_resolve(*args, **kwargs))
                if token:
                    return token
            except Exception:
                logger.debug("[KABUSAPI TOKEN RETRY REGISTER] original _resolve_api_key failed", exc_info=True)
            # Last chance: refresh from APIPassword in child process.
            token = _refresh_token(reason="resolve_empty")
            return token

        def _http_json_request_patched(*, url: str, method: str, payload: Any, api_key: str, timeout: float = 10.0):
            token = _safe_str(api_key) or _resolve_api_key_patched()
            ok, content = orig_http(url=url, method=method, payload=payload, api_key=token, timeout=timeout)
            if ok or not _is_api_key_mismatch(content):
                return ok, content
            logger.warning(
                "[KABUSAPI TOKEN RETRY REGISTER] detected API key mismatch method=%s url=%s -> refresh and retry once content=%r",
                method,
                url,
                content,
            )
            new_token = _refresh_token(reason="http_4001009")
            if not new_token or new_token == token:
                return ok, content
            ok2, content2 = orig_http(url=url, method=method, payload=payload, api_key=new_token, timeout=timeout)
            logger.warning(
                "[KABUSAPI TOKEN RETRY REGISTER] retry after token refresh ok=%s content=%r",
                ok2,
                content2,
            )
            return ok2, content2

        _resolve_api_key_patched._kabusapi_token_retry_register_v1 = True  # type: ignore[attr-defined]
        _resolve_api_key_patched._original = orig_resolve  # type: ignore[attr-defined]
        _http_json_request_patched._kabusapi_token_retry_register_v1 = True  # type: ignore[attr-defined]
        _http_json_request_patched._original = orig_http  # type: ignore[attr-defined]

        ops._resolve_api_key = _resolve_api_key_patched
        ops._http_json_request = _http_json_request_patched
        ops._kabusapi_token_retry_register_v1 = True
        _INSTALLED = True
        logger.warning("[KABUSAPI TOKEN RETRY REGISTER] installed v1")
        return True


def install() -> bool:
    return _install_now()


try:
    install()
except Exception:
    logger.exception("[KABUSAPI TOKEN RETRY REGISTER] auto install failed")


__all__ = ["install"]
