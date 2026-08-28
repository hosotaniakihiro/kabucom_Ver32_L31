# -*- coding: utf-8 -*-
"""
Startup token bootstrap state machine (kabu Station).

Why this module exists
----------------------
Before this module, startup validity of the settings.ini token was decided by a
TTL only (KABU_TOKEN_PARENT_REFRESH_TTL_SEC, default 3600s measured from the
settings.ini mtime).  When kabu Station was restarted, the server side token was
invalidated while the local file was still "young", so:

    settings.ini token age < TTL
      -> no /token call
      -> preflight /positions -> 401 Code=4001009
      -> runtime refresh is intentionally forbidden
      -> no recovery path -> main_database.py aborts with exit code 1

TTL cannot know whether the server still accepts the token.  Only a real API call
can.  So startup validity is now decided by a read-only preflight, and a stale
cached token is recovered by exactly one /token call.

Policy
------
* startup stale token recovery = ON   -- a startup preflight rejection may
  trigger a single /token call.
* runtime arbitrary refresh = OFF     -- unchanged; nothing here loosens the
  runtime 401 policy in token_manager / token_startup_policy.
* At most one POST /token per process at startup (zero when the cached token
  preflights OK).  _STARTUP_RECOVERY_ATTEMPTED makes that a process-wide
  guarantee even when several entrypoints run the state machine.

Two failures that must never be confused
----------------------------------------
A. cached token stale   : /positions -> 401, then POST /token -> 200
   => recovery succeeds; no failure is reported.
B. API password mismatch: POST /token itself -> 401 / Code=4001009
   => failure_kind = "api_password_mismatch" (TOKEN REQUEST FAILED).

Logging contract
----------------
Neither the token nor the API password is ever logged.  Only:
token_len / token_tail (last 4 chars) and
api_password_source / password_present / password_len.

TOKEN_SOURCE : settings_ini_cached | fresh_token_api | startup_401_recovery | inherited_parent
TOKEN_ACTION : reused | api_requested | api_received | saved | preflight_ok | preflight_ng
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)

VERSION = "V1-STARTUP-PREFLIGHT-STALE-TOKEN-RECOVERY"

KABU_API_BASE_URL = "http://localhost:18080/kabusapi"
DEFAULT_PREFLIGHT_ENDPOINTS: tuple[str, ...] = ("/positions", "/wallet/cash")

# ---- TOKEN_SOURCE -------------------------------------------------------
SOURCE_SETTINGS_INI_CACHED = "settings_ini_cached"
SOURCE_FRESH_TOKEN_API = "fresh_token_api"
SOURCE_STARTUP_401_RECOVERY = "startup_401_recovery"
SOURCE_INHERITED_PARENT = "inherited_parent"

# ---- TOKEN_ACTION -------------------------------------------------------
ACTION_REUSED = "reused"
ACTION_API_REQUESTED = "api_requested"
ACTION_API_RECEIVED = "api_received"
ACTION_SAVED = "saved"
ACTION_PREFLIGHT_OK = "preflight_ok"
ACTION_PREFLIGHT_NG = "preflight_ng"

# ---- preflight result kinds --------------------------------------------
PF_OK = "ok"
PF_ACCEPTED_NON_AUTH = "accepted_non_auth"
PF_AUTH_REJECTED = "auth_rejected"
PF_TRANSPORT_ERROR = "transport_error"
PF_EMPTY_TOKEN = "empty_token"

# ---- bootstrap failure kinds -------------------------------------------
FAIL_NONE = ""
FAIL_NO_TOKEN = "no_token"
FAIL_API_PASSWORD_MISMATCH = "api_password_mismatch"
FAIL_TOKEN_API_TRANSPORT = "token_api_transport_error"
FAIL_TOKEN_API_ERROR = "token_api_error"
FAIL_PREFLIGHT_TRANSPORT = "preflight_transport_error"
FAIL_FRESH_TOKEN_REJECTED = "fresh_token_rejected"
FAIL_RECOVERY_DISABLED = "cached_token_rejected_recovery_disabled"
FAIL_RECOVERY_ALREADY_USED = "cached_token_rejected_recovery_already_used"

_LOG_PREFIX = "[TOKEN BOOTSTRAP]"

# Process-wide guard: startup recovery (= a POST /token triggered by a preflight
# rejection) is allowed once per process.  Both the import-time policy bootstrap
# and main_database.main() run the state machine, and they must share this.
_STARTUP_RECOVERY_ATTEMPTED = False


def token_tail(token: Any) -> str:
    s = str(token or "").strip()
    return s[-4:] if s else ""


def token_len(token: Any) -> int:
    return len(str(token or "").strip())


def startup_recovery_attempted() -> bool:
    return bool(_STARTUP_RECOVERY_ATTEMPTED)


def reset_startup_state() -> None:
    """Test hook. Never called by production code."""
    global _STARTUP_RECOVERY_ATTEMPTED
    _STARTUP_RECOVERY_ATTEMPTED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def safe_content_text(value: Any, limit: int = 240) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except Exception:
        text = str(value)
    text = str(text).replace("\r", " ").replace("\n", " ")
    return text[:limit]


def is_api_key_mismatch(content: Any) -> bool:
    """True when kabu Station answered APIキー不一致 (Code=4001009)."""
    try:
        if isinstance(content, dict):
            code = str(content.get("Code") or "")
            msg = str(content.get("Message") or "")
            return code == "4001009" or "APIキー不一致" in msg
        s = str(content)
        return "4001009" in s or "APIキー不一致" in s
    except Exception:
        return False


# ============================================================
# preflight (read-only endpoints only -- never places an order)
# ============================================================


def http_get_json(url: str, token: str, timeout: float = 5.0) -> tuple[bool, int | None, Any]:
    """GET url with X-API-KEY.

    Returns (ok, status, content).  status is None when the request never reached
    kabu Station (transport error).
    """
    req = urllib.request.Request(
        url,
        headers={
            "Content-Type": "application/json",
            "X-API-KEY": str(token or ""),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", errors="ignore")
            try:
                content = json.loads(raw) if raw else {}
            except Exception:
                content = raw
            return True, int(getattr(res, "status", 200) or 200), content
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", errors="ignore")
        except Exception:
            raw = ""
        try:
            content = json.loads(raw) if raw else str(e)
        except Exception:
            content = raw or str(e)
        return False, int(getattr(e, "code", 0) or 0), content
    except Exception as e:  # URLError / timeout / socket error
        return False, None, str(e)


@dataclass
class PreflightResult:
    ok: bool
    kind: str
    endpoint: str | None = None
    status: int | None = None
    content: Any = None

    @property
    def is_auth_rejected(self) -> bool:
        return self.kind == PF_AUTH_REJECTED

    @property
    def is_transport_error(self) -> bool:
        return self.kind == PF_TRANSPORT_ERROR

    def detail(self) -> str:
        return (
            f"kind={self.kind} endpoint={self.endpoint} status={self.status} "
            f"content={safe_content_text(self.content)}"
        )


def preflight_token(
    token: str,
    *,
    base_url: str = KABU_API_BASE_URL,
    endpoints: Sequence[str] | None = None,
    request_fn: Callable[[str, str, float], tuple[bool, int | None, Any]] | None = None,
    timeout: float | None = None,
    label: str = "",
) -> PreflightResult:
    """Validate token against read-only kabu endpoints.

    Rules (identical to the previous main_database._preflight_kabu_token):
      * 2xx                       -> ok
      * 401 / 403 / Code=4001009  -> auth rejected (try the next endpoint first)
      * any other HTTP status     -> the request reached kabu Station and was NOT
                                     rejected by API key, which is enough to prove
                                     the token is accepted -> ok
      * no status at all          -> transport error (kabu Station unreachable)
    """
    token = str(token or "").strip()
    if not token:
        logger.error("%s %s TOKEN_ACTION=%s reason=empty_token", _LOG_PREFIX, label, ACTION_PREFLIGHT_NG)
        return PreflightResult(ok=False, kind=PF_EMPTY_TOKEN)

    eps = tuple(endpoints) if endpoints else DEFAULT_PREFLIGHT_ENDPOINTS
    if timeout is None:
        timeout = _env_float("KABU_TOKEN_PREFLIGHT_TIMEOUT_SEC", 5.0)
    fn = request_fn or (lambda url, tok, to: http_get_json(url, tok, to))

    auth_failure: PreflightResult | None = None
    transport_failure: PreflightResult | None = None

    for endpoint in eps:
        url = f"{base_url}{endpoint}"
        try:
            ok, status, content = fn(url, token, timeout)
        except Exception as e:  # a broken request_fn must not look like auth failure
            transport_failure = PreflightResult(
                ok=False, kind=PF_TRANSPORT_ERROR, endpoint=endpoint, status=None, content=str(e)
            )
            logger.error(
                "%s %s TOKEN_ACTION=%s endpoint=%s transport_exception=%s",
                _LOG_PREFIX, label, ACTION_PREFLIGHT_NG, endpoint, safe_content_text(str(e)),
            )
            continue

        if ok:
            logger.warning(
                "%s %s TOKEN_ACTION=%s endpoint=%s status=%s token_len=%d token_tail=%s",
                _LOG_PREFIX, label, ACTION_PREFLIGHT_OK, endpoint, status,
                token_len(token), token_tail(token),
            )
            return PreflightResult(ok=True, kind=PF_OK, endpoint=endpoint, status=status, content=content)

        if status in (401, 403) or is_api_key_mismatch(content):
            auth_failure = PreflightResult(
                ok=False, kind=PF_AUTH_REJECTED, endpoint=endpoint, status=status, content=content
            )
            logger.error(
                "%s %s TOKEN_ACTION=%s endpoint=%s status=%s content=%s token_len=%d token_tail=%s",
                _LOG_PREFIX, label, ACTION_PREFLIGHT_NG, endpoint, status,
                safe_content_text(content), token_len(token), token_tail(token),
            )
            continue

        if status is not None:
            # 400/404/500 -- reached kabu Station, not an API-key rejection.
            logger.warning(
                "%s %s TOKEN_ACTION=%s endpoint=%s status=%s content=%s note=accepted_non_auth_error",
                _LOG_PREFIX, label, ACTION_PREFLIGHT_OK, endpoint, status, safe_content_text(content),
            )
            return PreflightResult(
                ok=True, kind=PF_ACCEPTED_NON_AUTH, endpoint=endpoint, status=status, content=content
            )

        transport_failure = PreflightResult(
            ok=False, kind=PF_TRANSPORT_ERROR, endpoint=endpoint, status=None, content=content
        )
        logger.error(
            "%s %s TOKEN_ACTION=%s endpoint=%s transport_error=%s",
            _LOG_PREFIX, label, ACTION_PREFLIGHT_NG, endpoint, safe_content_text(content),
        )

    if auth_failure is not None:
        return auth_failure
    if transport_failure is not None:
        return transport_failure
    return PreflightResult(
        ok=False, kind=PF_TRANSPORT_ERROR, endpoint=None, status=None, content="no endpoint tried"
    )


# ============================================================
# bootstrap state machine
# ============================================================


@dataclass
class BootstrapResult:
    ok: bool
    token: str = ""
    source: str = ""
    token_api_calls: int = 0
    failure_kind: str = FAIL_NONE
    detail: str = ""
    preflight: PreflightResult | None = field(default=None, repr=False)

    @property
    def is_password_mismatch(self) -> bool:
        return self.failure_kind == FAIL_API_PASSWORD_MISMATCH

    def summary(self) -> str:
        return (
            f"ok={self.ok} TOKEN_SOURCE={self.source or '-'} token_api_calls={self.token_api_calls} "
            f"token_len={token_len(self.token)} token_tail={token_tail(self.token)} "
            f"failure_kind={self.failure_kind or '-'} detail={safe_content_text(self.detail)}"
        )


def _default_read_cached_token() -> tuple[str, str]:
    """Return (token, source) for the token we start from.

    settings.ini is the canonical store.  Only when it has no token do we accept a
    token inherited through the environment from a parent process.
    """
    try:
        import token_manager  # type: ignore

        token = str(token_manager.read_settings_token() or "").strip()
        if token:
            return token, SOURCE_SETTINGS_INI_CACHED
    except Exception:
        logger.debug("%s settings.ini token read failed", _LOG_PREFIX, exc_info=True)

    for key in (
        "KABU_API_TOKEN", "KABUSAPI_TOKEN", "AUKABU_TOKEN", "API_TOKEN",
        "TOKEN", "KABU_API_KEY", "KABUSAPI_API_KEY", "X_API_KEY", "KABU_TOKEN",
    ):
        v = str(os.environ.get(key) or "").strip()
        if v:
            return v, SOURCE_INHERITED_PARENT
    return "", ""


def _default_request_fresh_token(apipassword: Any = None, reason: str = "") -> str:
    import token_manager  # type: ignore

    return str(token_manager.request_fresh_token(apipassword=apipassword, reason=reason) or "").strip()


def _token_api_exceptions() -> tuple[type, type, type]:
    """(mismatch, transport, generic) exception classes from token_manager."""
    try:
        import token_manager  # type: ignore

        return (
            token_manager.TokenApiPasswordMismatch,
            token_manager.TokenApiTransportError,
            token_manager.TokenApiError,
        )
    except Exception:
        class _Never(Exception):
            pass

        return (_Never, _Never, _Never)


def bootstrap_startup_token(
    *,
    apipassword: Any = None,
    read_cached_token: Callable[[], Any] | None = None,
    request_fresh_token: Callable[..., str] | None = None,
    preflight: Callable[[str, str], PreflightResult] | None = None,
    allow_startup_recovery: bool = True,
    label: str = "startup",
) -> BootstrapResult:
    """Decide the startup token by preflight, recovering a stale cache once.

    Contract:
      * cached token that preflights OK -> zero /token calls
      * cached token rejected (401/403/4001009) -> exactly one /token call, then
        exactly one more preflight.  No retry loop.
      * cached token unusable because kabu Station is unreachable -> fail-safe
        abort without burning a /token call.
      * POST /token itself rejected -> api_password_mismatch (case B), which is
        never reported as a stale-cache problem (case A).
    """
    global _STARTUP_RECOVERY_ATTEMPTED

    read_cached = read_cached_token or _default_read_cached_token
    do_request = request_fresh_token or _default_request_fresh_token
    do_preflight = preflight or (lambda token, lbl: preflight_token(token, label=lbl))

    Mismatch, Transport, Generic = _token_api_exceptions()

    cached_raw = read_cached()
    if isinstance(cached_raw, tuple):
        cached, cached_source = cached_raw
    else:
        cached, cached_source = cached_raw, SOURCE_SETTINGS_INI_CACHED
    cached = str(cached or "").strip()
    cached_source = cached_source or SOURCE_SETTINGS_INI_CACHED

    recovery_source = SOURCE_FRESH_TOKEN_API

    if cached:
        logger.warning(
            "%s %s TOKEN_SOURCE=%s TOKEN_ACTION=%s token_len=%d token_tail=%s",
            _LOG_PREFIX, label, cached_source, ACTION_REUSED, token_len(cached), token_tail(cached),
        )
        pf = do_preflight(cached, f"{label}/cached")
        if pf.ok:
            logger.warning(
                "%s %s RESULT TOKEN_SOURCE=%s TOKEN_ACTION=%s token_api_calls=0 token_len=%d token_tail=%s",
                _LOG_PREFIX, label, cached_source, ACTION_PREFLIGHT_OK,
                token_len(cached), token_tail(cached),
            )
            return BootstrapResult(
                ok=True, token=cached, source=cached_source, token_api_calls=0, preflight=pf
            )

        if pf.is_transport_error:
            logger.error(
                "%s %s abort: kabu API preflight could not connect. %s "
                "Confirm kabu Station API is listening on localhost:18080.",
                _LOG_PREFIX, label, pf.detail(),
            )
            return BootstrapResult(
                ok=False, token=cached, source=cached_source, token_api_calls=0,
                failure_kind=FAIL_PREFLIGHT_TRANSPORT, detail=pf.detail(), preflight=pf,
            )

        # Auth rejected -> the cached token is stale on the server side.
        logger.error(
            "%s %s cached token rejected by kabu API (stale cached token). %s",
            _LOG_PREFIX, label, pf.detail(),
        )
        if not allow_startup_recovery:
            return BootstrapResult(
                ok=False, token=cached, source=cached_source, token_api_calls=0,
                failure_kind=FAIL_RECOVERY_DISABLED, detail=pf.detail(), preflight=pf,
            )
        if _STARTUP_RECOVERY_ATTEMPTED:
            logger.error(
                "%s %s startup token recovery already used once in this process; not calling /token again.",
                _LOG_PREFIX, label,
            )
            return BootstrapResult(
                ok=False, token=cached, source=cached_source, token_api_calls=0,
                failure_kind=FAIL_RECOVERY_ALREADY_USED, detail=pf.detail(), preflight=pf,
            )
        recovery_source = SOURCE_STARTUP_401_RECOVERY
    else:
        logger.warning("%s %s no cached token; requesting a fresh one", _LOG_PREFIX, label)
        if not allow_startup_recovery:
            return BootstrapResult(
                ok=False, token="", source="", token_api_calls=0,
                failure_kind=FAIL_NO_TOKEN, detail="no cached token and recovery disabled",
            )
        if _STARTUP_RECOVERY_ATTEMPTED:
            logger.error(
                "%s %s no cached token and startup /token already used once in this process.",
                _LOG_PREFIX, label,
            )
            return BootstrapResult(
                ok=False, token="", source="", token_api_calls=0,
                failure_kind=FAIL_RECOVERY_ALREADY_USED, detail="no cached token",
            )

    # ---- exactly one POST /token --------------------------------------
    _STARTUP_RECOVERY_ATTEMPTED = True
    logger.warning(
        "%s %s TOKEN_SOURCE=%s TOKEN_ACTION=%s",
        _LOG_PREFIX, label, recovery_source, ACTION_API_REQUESTED,
    )
    try:
        fresh = str(do_request(apipassword=apipassword, reason=recovery_source) or "").strip()
    except Mismatch as e:
        logger.error(
            "%s %s TOKEN REQUEST FAILED kind=%s -- POST /token itself was rejected. "
            "This is an API password mismatch, NOT a stale cached token. detail=%s",
            _LOG_PREFIX, label, FAIL_API_PASSWORD_MISMATCH, safe_content_text(str(e)),
        )
        return BootstrapResult(
            ok=False, token="", source=recovery_source, token_api_calls=1,
            failure_kind=FAIL_API_PASSWORD_MISMATCH, detail=str(e),
        )
    except Transport as e:
        logger.error(
            "%s %s TOKEN REQUEST FAILED kind=%s -- POST /token could not reach kabu Station. detail=%s",
            _LOG_PREFIX, label, FAIL_TOKEN_API_TRANSPORT, safe_content_text(str(e)),
        )
        return BootstrapResult(
            ok=False, token="", source=recovery_source, token_api_calls=1,
            failure_kind=FAIL_TOKEN_API_TRANSPORT, detail=str(e),
        )
    except Generic as e:
        logger.error(
            "%s %s TOKEN REQUEST FAILED kind=%s detail=%s",
            _LOG_PREFIX, label, FAIL_TOKEN_API_ERROR, safe_content_text(str(e)),
        )
        return BootstrapResult(
            ok=False, token="", source=recovery_source, token_api_calls=1,
            failure_kind=FAIL_TOKEN_API_ERROR, detail=str(e),
        )
    except Exception as e:
        logger.exception("%s %s TOKEN REQUEST FAILED kind=%s", _LOG_PREFIX, label, FAIL_TOKEN_API_ERROR)
        return BootstrapResult(
            ok=False, token="", source=recovery_source, token_api_calls=1,
            failure_kind=FAIL_TOKEN_API_ERROR, detail=str(e),
        )

    if not fresh:
        logger.error(
            "%s %s TOKEN REQUEST FAILED kind=%s -- /token returned an empty token",
            _LOG_PREFIX, label, FAIL_TOKEN_API_ERROR,
        )
        return BootstrapResult(
            ok=False, token="", source=recovery_source, token_api_calls=1,
            failure_kind=FAIL_TOKEN_API_ERROR, detail="empty token returned by /token",
        )

    logger.warning(
        "%s %s TOKEN_SOURCE=%s TOKEN_ACTION=%s token_len=%d token_tail=%s",
        _LOG_PREFIX, label, recovery_source, ACTION_API_RECEIVED, token_len(fresh), token_tail(fresh),
    )

    # ---- exactly one more preflight; never a retry loop ---------------
    pf2 = do_preflight(fresh, f"{label}/fresh")
    if pf2.ok:
        logger.warning(
            "%s %s RESULT TOKEN_SOURCE=%s TOKEN_ACTION=%s token_api_calls=1 token_len=%d token_tail=%s",
            _LOG_PREFIX, label, recovery_source, ACTION_PREFLIGHT_OK, token_len(fresh), token_tail(fresh),
        )
        return BootstrapResult(
            ok=True, token=fresh, source=recovery_source, token_api_calls=1, preflight=pf2
        )

    failure_kind = FAIL_PREFLIGHT_TRANSPORT if pf2.is_transport_error else FAIL_FRESH_TOKEN_REJECTED
    logger.error(
        "%s %s abort: a freshly issued token was still rejected; not retrying. kind=%s %s "
        "Restart kabu Station, enable the API, and confirm settings.ini apipassword.",
        _LOG_PREFIX, label, failure_kind, pf2.detail(),
    )
    return BootstrapResult(
        ok=False, token=fresh, source=recovery_source, token_api_calls=1,
        failure_kind=failure_kind, detail=pf2.detail(), preflight=pf2,
    )


__all__ = [
    "VERSION",
    "KABU_API_BASE_URL",
    "DEFAULT_PREFLIGHT_ENDPOINTS",
    "SOURCE_SETTINGS_INI_CACHED",
    "SOURCE_FRESH_TOKEN_API",
    "SOURCE_STARTUP_401_RECOVERY",
    "SOURCE_INHERITED_PARENT",
    "ACTION_REUSED",
    "ACTION_API_REQUESTED",
    "ACTION_API_RECEIVED",
    "ACTION_SAVED",
    "ACTION_PREFLIGHT_OK",
    "ACTION_PREFLIGHT_NG",
    "PF_OK",
    "PF_ACCEPTED_NON_AUTH",
    "PF_AUTH_REJECTED",
    "PF_TRANSPORT_ERROR",
    "PF_EMPTY_TOKEN",
    "FAIL_NONE",
    "FAIL_NO_TOKEN",
    "FAIL_API_PASSWORD_MISMATCH",
    "FAIL_TOKEN_API_TRANSPORT",
    "FAIL_TOKEN_API_ERROR",
    "FAIL_PREFLIGHT_TRANSPORT",
    "FAIL_FRESH_TOKEN_REJECTED",
    "FAIL_RECOVERY_DISABLED",
    "FAIL_RECOVERY_ALREADY_USED",
    "PreflightResult",
    "BootstrapResult",
    "preflight_token",
    "http_get_json",
    "is_api_key_mismatch",
    "safe_content_text",
    "bootstrap_startup_token",
    "startup_recovery_attempted",
    "reset_startup_state",
    "token_tail",
    "token_len",
]
