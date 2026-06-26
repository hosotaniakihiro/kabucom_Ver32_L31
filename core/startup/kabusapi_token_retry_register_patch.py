# ============================================================
# File   : core/startup/kabusapi_token_retry_register_patch.py
# Version: V5-KABUSAPI-REGISTER-CANONICAL-TOKEN-CHILD-NO-BOOTSTRAP
# ------------------------------------------------------------
# Purpose:
#   PUSH subscription register/unregister may run in child processes.
#   Operational policy is startup-once token handling:
#     - token is obtained once by parent main_database.py/main.py and stored in settings.ini/runtime cache
#     - child push_receiver must NOT call /token or bypass token_manager child guards
#     - live register/unregister must use the canonical settings/runtime token
#     - 4001009 / APIキー不一致 must be surfaced as a real auth failure
# ============================================================
from __future__ import annotations

import configparser
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_BOOTSTRAPPED = False
_LOCK = threading.RLock()

_TOKEN_ENV_KEYS = (
    "KABUSAPI_TOKEN",
    "KABU_API_TOKEN",
    "AUKABU_TOKEN",
    "API_TOKEN",
    "TOKEN",
    "KABU_TOKEN",
    "X_API_KEY",
    "KABU_API_KEY",
    "KABUSAPI_API_KEY",
)

_CHILD_MARKERS = (
    "push_receiver_runner.py",
    "ranking_collector_runner.py",
    "summary_database_runner.py",
    "yahoo_complement_runner.py",
    "db_prepare_runner.py",
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


def _argv_text() -> str:
    try:
        return " ".join(str(x) for x in getattr(sys, "argv", [])).lower()
    except Exception:
        return ""


def _is_child_process() -> bool:
    txt = _argv_text()
    if any(marker in txt for marker in _CHILD_MARKERS):
        return True
    for key in ("DATA_COLLECTOR_CHILD", "KABU_CHILD_PROCESS", "IS_CHILD_PROCESS"):
        if _env_bool(key, False):
            return True
    return False


def _is_parent_process() -> bool:
    txt = _argv_text()
    return "main_database.py" in txt or "main.py" in txt


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


def _settings_paths() -> list[Path]:
    out: list[Path] = []
    for env_name in ("SETTINGS_INI_PATH", "KABU_SETTINGS_INI"):
        v = _safe_str(os.environ.get(env_name))
        if v:
            out.append(Path(v))
    try:
        import token_manager
        p = _safe_str(getattr(token_manager, "_CONFIG_FILE_PATH", None))
        if p:
            out.append(Path(p))
    except Exception:
        pass
    cwd = Path.cwd()
    out.extend([cwd / "settings.ini", cwd / "config" / "settings.ini"])
    try:
        here = Path(__file__).resolve().parents[2]
        out.extend([here / "settings.ini", here / "config" / "settings.ini"])
    except Exception:
        pass
    uniq: list[Path] = []
    seen: set[str] = set()
    for p in out:
        try:
            key = str(p.resolve())
        except Exception:
            key = str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _read_settings_ini_token() -> str:
    for path in _settings_paths():
        try:
            if not path.exists():
                continue
            cp = configparser.ConfigParser()
            cp.read(path, encoding="utf-8-sig")
            for sec in ("aukabu", "kabusapi"):
                if cp.has_section(sec):
                    token = _safe_str(cp.get(sec, "token", fallback=""))
                    if token:
                        return _publish_token(token)
        except Exception:
            logger.debug("[KABUSAPI TOKEN RETRY REGISTER] settings token read failed path=%s", path, exc_info=True)
    return ""


def _canonical_token() -> tuple[str, str]:
    """Return the canonical token and source for register/unregister HTTP calls.

    This intentionally ignores the api_key argument passed by wrappers, because older
    recovery patches may pass stale env/global tokens.  The canonical order is:
      1. token_manager.API_TOKEN, which should be populated by parent startup/preflight
      2. settings.ini [aukabu]/[kabusapi] token
      3. explicit token environment variables as last fallback
    """
    try:
        import token_manager
        token = _safe_str(getattr(token_manager, "API_TOKEN", None))
        if token:
            return _publish_token(token), "token_manager.API_TOKEN"
    except Exception:
        pass

    token = _read_settings_ini_token()
    if token:
        return token, "settings.ini"

    for key in _TOKEN_ENV_KEYS:
        token = _safe_str(os.environ.get(key))
        if token:
            return _publish_token(token), f"env.{key}"
    return "", "none"


def _read_settings_or_runtime_token() -> str:
    token, _source = _canonical_token()
    return token


def _bootstrap_token_once() -> str:
    """Acquire no token in child processes.

    Previous versions called token_manager.refresh_token._original() here.  That bypassed
    token_manager's child-process guard and let push_receiver call /token after the
    parent preflight.  In kabu Station this can make the token used by other child
    processes inconsistent.  Children now only read the parent-issued token.
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return _read_settings_or_runtime_token()
    _BOOTSTRAPPED = True

    if _is_child_process() and not _env_bool("KABU_REGISTER_ALLOW_CHILD_BOOTSTRAP_TOKEN", False):
        token, source = _canonical_token()
        logger.warning(
            "[KABUSAPI TOKEN RETRY REGISTER] child process detected; /token bootstrap skipped source=%s token_len=%d argv=%s",
            source,
            len(token),
            _argv_text(),
        )
        return token

    if (not _is_parent_process()) and not _env_bool("KABU_REGISTER_ALLOW_NONPARENT_BOOTSTRAP_TOKEN", False):
        token, source = _canonical_token()
        logger.warning(
            "[KABUSAPI TOKEN RETRY REGISTER] non-parent process; /token bootstrap skipped source=%s token_len=%d argv=%s",
            source,
            len(token),
            _argv_text(),
        )
        return token

    if not _env_bool("KABU_REGISTER_BOOTSTRAP_TOKEN_ON_INSTALL", True):
        token, source = _canonical_token()
        logger.warning(
            "[KABUSAPI TOKEN RETRY REGISTER] bootstrap disabled; using existing token source=%s token_len=%d",
            source,
            len(token),
        )
        return token

    try:
        import token_manager
        refresh = getattr(token_manager, "refresh_token", None)
        if callable(refresh):
            # Deliberately call the public refresh wrapper, not _original, so the
            # parent/child guard in token_manager cannot be bypassed.
            token = _safe_str(refresh())
            if token:
                _publish_token(token)
                os.environ["KABU_REGISTER_BOOTSTRAP_DONE"] = "1"
                logger.warning(
                    "[KABUSAPI TOKEN RETRY REGISTER] parent startup bootstrap token refreshed/saved before register token_len=%d",
                    len(token),
                )
                return token
            logger.error("[KABUSAPI TOKEN RETRY REGISTER] startup bootstrap returned empty token before register")
        else:
            logger.error("[KABUSAPI TOKEN RETRY REGISTER] token_manager.refresh_token missing; cannot bootstrap before register")
    except Exception:
        logger.exception("[KABUSAPI TOKEN RETRY REGISTER] startup bootstrap token refresh failed before register")

    token, source = _canonical_token()
    logger.warning(
        "[KABUSAPI TOKEN RETRY REGISTER] fallback to existing token after bootstrap failure source=%s token_len=%d",
        source,
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

        if getattr(ops, "_kabusapi_token_retry_register_v5", False):
            _INSTALLED = True
            return True

        orig_resolve = getattr(ops, "_resolve_api_key", None)
        orig_http = getattr(ops, "_http_json_request", None)
        if not callable(orig_resolve) or not callable(orig_http):
            return False

        _bootstrap_token_once()

        def _resolve_api_key_patched(*args: Any, **kwargs: Any) -> str:
            token, source = _canonical_token()
            if token:
                logger.info("[KABUSAPI TOKEN RETRY REGISTER] resolved canonical token source=%s token_len=%d", source, len(token))
                return token
            try:
                token = _safe_str(orig_resolve(*args, **kwargs))
                if token:
                    logger.warning("[KABUSAPI TOKEN RETRY REGISTER] canonical token missing; fallback original resolver token_len=%d", len(token))
                    return _publish_token(token)
            except Exception:
                logger.debug("[KABUSAPI TOKEN RETRY REGISTER] original _resolve_api_key failed", exc_info=True)
            logger.warning("[KABUSAPI TOKEN RETRY REGISTER] no startup/settings.ini token available; runtime refresh disabled")
            return ""

        def _http_json_request_patched(*, url: str, method: str, payload: Any, api_key: str, timeout: float = 10.0):
            token, source = _canonical_token()
            arg_len = len(_safe_str(api_key))
            if not token:
                token = _safe_str(api_key) or _resolve_api_key_patched()
                source = "api_key_arg_or_resolver"
            if arg_len and _safe_str(api_key) != token:
                logger.warning(
                    "[KABUSAPI TOKEN RETRY REGISTER] overriding stale api_key arg for %s %s arg_len=%d canonical_source=%s canonical_len=%d",
                    method,
                    url,
                    arg_len,
                    source,
                    len(token),
                )
            else:
                logger.info(
                    "[KABUSAPI TOKEN RETRY REGISTER] using canonical token for %s %s source=%s token_len=%d",
                    method,
                    url,
                    source,
                    len(token),
                )
            ok, content = orig_http(url=url, method=method, payload=payload, api_key=token, timeout=timeout)
            if ok or not _is_api_key_mismatch(content):
                return ok, content
            logger.error(
                "[KABUSAPI TOKEN RETRY REGISTER] API key mismatch method=%s url=%s canonical_source=%s token_len=%d arg_len=%d runtime_refresh_disabled content=%r",
                method,
                url,
                source,
                len(token),
                arg_len,
                content,
            )
            return ok, content

        _resolve_api_key_patched._kabusapi_token_retry_register_v5 = True  # type: ignore[attr-defined]
        _resolve_api_key_patched._original = orig_resolve  # type: ignore[attr-defined]
        _http_json_request_patched._kabusapi_token_retry_register_v5 = True  # type: ignore[attr-defined]
        _http_json_request_patched._original = orig_http  # type: ignore[attr-defined]

        ops._resolve_api_key = _resolve_api_key_patched
        ops._http_json_request = _http_json_request_patched
        ops._kabusapi_token_retry_register_v5 = True
        _INSTALLED = True
        logger.warning("[KABUSAPI TOKEN RETRY REGISTER] installed v5 canonical_token=True child_no_bootstrap=True no_runtime_refresh=True")
        return True


def install() -> bool:
    return _install_now()


try:
    install()
except Exception:
    logger.exception("[KABUSAPI TOKEN RETRY REGISTER] auto install failed")


__all__ = ["install"]
