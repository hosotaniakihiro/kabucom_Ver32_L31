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
import socket
import time
import urllib.error
import urllib.request

import requests
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

# R18: 「TCP すら繋がらない」と「繋がったのに応答が返らない」を区別する。
#
# 2026-08-28 23:3x の実測 (kabu Station は起動・ログイン済み):
#   誤パスワードの POST /token       -> HTTP 401  (99 ms)
#   dummy キーの GET /wallet/cash    -> HTTP 401  (6 ms)
#   失効トークンの GET /wallet/cash  -> 12 秒ハング後に RST (WinError 10054)
#   正パスワードの POST /token       -> 12 秒ハング後に RST
# 「門前払いは速いが実処理だけがハングする」状態があり、
# 失効トークンが 401 ではなく RST として返ってくる。
# 旧実装はこれを「kabu Station に繋がらない」と扱って abort し、
# トークン再取得を試みないため **自力で復旧できなかった**。
PF_NO_RESPONSE = "no_response"

# http_get_json が transport 失敗の内容へ付ける接頭辞。
# 独自の request_fn を渡す既存呼び出しは接頭辞を持たないので、
# その場合は従来どおり unreachable 扱いになる (後方互換)。
TRANSPORT_UNREACHABLE = "unreachable"
TRANSPORT_NO_RESPONSE = "no_response"
PF_EMPTY_TOKEN = "empty_token"

# ---- bootstrap failure kinds -------------------------------------------
FAIL_NONE = ""
FAIL_NO_TOKEN = "no_token"
FAIL_API_PASSWORD_MISMATCH = "api_password_mismatch"
FAIL_TOKEN_API_TRANSPORT = "token_api_transport_error"
FAIL_TOKEN_API_ERROR = "token_api_error"
FAIL_PREFLIGHT_TRANSPORT = "preflight_transport_error"
FAIL_PREFLIGHT_NO_RESPONSE = "preflight_no_response"
FAIL_FRESH_TOKEN_REJECTED = "fresh_token_rejected"
FAIL_RECOVERY_DISABLED = "cached_token_rejected_recovery_disabled"
FAIL_RECOVERY_ALREADY_USED = "cached_token_rejected_recovery_already_used"

_LOG_PREFIX = "[TOKEN BOOTSTRAP]"

# Process-wide guard: startup recovery (= a POST /token triggered by a preflight
# rejection) is allowed once per process.  Both the import-time policy bootstrap
# and main_database.main() run the state machine, and they must share this.
_STARTUP_RECOVERY_ATTEMPTED = False

# R18: 上のフラグは「/token が成功した」ときだけ立てる。
# 失敗した試行で食い潰すと、後続の本番 bootstrap が 1 度も呼べなくなる
# (2026-08-29 10:11 に main_database が実際にこれで abort した)。
# ループ防止は試行回数の上限で行う。
_STARTUP_TOKEN_CALLS = 0
_DEFAULT_STARTUP_TOKEN_MAX_CALLS = 2


def startup_token_max_calls() -> int:
    """1 プロセスで許す POST /token の最大試行回数。"""
    try:
        v = os.getenv("KABU_TOKEN_STARTUP_MAX_CALLS")
        if v and str(v).strip():
            return max(1, int(str(v).strip()))
    except Exception:
        pass
    return _DEFAULT_STARTUP_TOKEN_MAX_CALLS


def startup_token_calls() -> int:
    return int(_STARTUP_TOKEN_CALLS)


# このプロセスで最後に /token から取り直したトークン。
# 「今しがた自分で発行したトークンが弾かれた」のか
# 「別の (古い) トークンが弾かれた」のかを区別するために持つ。
_LAST_RECOVERED_TOKEN = ""


def _startup_recovery_exhausted(cached: str = "") -> bool:
    """もう /token を呼んではいけないか。

    - 既に成功して取得済み          -> True (取り直す理由がない)
    - 試行回数が上限に達している    -> True (ループ防止)

    「1 プロセスにつき startup の POST /token は 1 回」というモジュール契約は
    変えない。2026-09-01 の障害はここを緩めるのではなく、
    発行直後のトークンが弾かれるウォームアップを preflight 側で吸収して直す
    (_warmup_retry_count を参照)。cached 引数は診断用に受けるだけ。
    """
    if _STARTUP_RECOVERY_ATTEMPTED:
        return True
    return _STARTUP_TOKEN_CALLS >= startup_token_max_calls()


# ------------------------------------------------------------
# 発行直後のトークンが弾かれたときの短いリトライ
# ------------------------------------------------------------
# 当初は「kabu Station 起動直後はトークンを発行しても暫く通さない
# (ウォームアップ)」と考えて 5 回 x 3 秒のリトライを入れていたが、
# 2026-09-01 10:24 の最小テストでこの前提は否定された:
#
#   settings.ini の token (tail=7ac1) -> /positions 401 4001009
#   POST /token -> 200 (0.00s) 新 token tail=a463
#   その token で /positions, /wallet/cash -> 発行 0.0 秒後に both 200
#
# つまりウォームアップ待ちは不要で、発行直後の token は即座に通る。
# 08:48 の 401 の正体は「settings.ini の token が kabu Station の
# 現行トークンではなかった」= プロセス間のトークン発行競合だった。
# 根本対処は token_manager 側のプロセス間ロックと
# 「ロック取得後に settings.ini を読み直す」ことで行う。
#
# ここのリトライは競合の瞬間を跨ぐためのごく短い保険だけ残す。
# 長く回すと「本当に失効している」ケースの発見を遅らせるだけになる。

def _warmup_window_sec() -> float:
    """発行から何秒までを「ウォームアップ中かもしれない」とみなすか。"""
    return max(0.0, _env_float("KABU_TOKEN_WARMUP_WINDOW_SEC", 120.0))


def _token_recently_issued(token: str) -> tuple[bool, float | None]:
    """この token が (どのプロセスであれ) つい先ほど発行されたものか。

    _LAST_RECOVERED_TOKEN はモジュール変数なので、自分が発行した場合しか
    分からない。main.py が発行した直後に main_database.py が起動すると
    「自分は発行していない新しい token」を掴むことになり、
    ウォームアップ中の 401 を失効と誤断定していた。
    settings.ini の隣に置いた発行メタデータで、発行元プロセスに関係なく
    判定できるようにする。
    """
    try:
        import token_manager  # type: ignore

        age = token_manager.token_age_sec(token)
    except Exception:
        return False, None
    if age is None:
        return False, None
    return (age <= _warmup_window_sec()), age


def _warmup_retry_count() -> int:
    try:
        v = os.environ.get("KABU_STARTUP_PREFLIGHT_WARMUP_RETRIES")
        if v is not None and str(v).strip() != "":
            return max(0, int(str(v).strip()))
    except Exception:
        pass
    # 最小テストでウォームアップ待ちは不要と判明したので 1 回だけにする。
    return 1


def _warmup_retry_wait_sec() -> float:
    return max(0.0, _env_float("KABU_STARTUP_PREFLIGHT_WARMUP_WAIT_SEC", 1.0))


def token_tail(token: Any) -> str:
    s = str(token or "").strip()
    return s[-4:] if s else ""


def token_len(token: Any) -> int:
    return len(str(token or "").strip())


def startup_recovery_attempted() -> bool:
    return bool(_STARTUP_RECOVERY_ATTEMPTED)


def reset_startup_state() -> None:
    """Test hook. Never called by production code."""
    global _STARTUP_RECOVERY_ATTEMPTED, _STARTUP_TOKEN_CALLS, _LAST_RECOVERED_TOKEN
    _STARTUP_RECOVERY_ATTEMPTED = False
    _STARTUP_TOKEN_CALLS = 0
    _LAST_RECOVERED_TOKEN = ""


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


def classify_transport_error(exc: BaseException) -> str:
    """transport 例外を unreachable / no_response に分類する。

    unreachable  : listen していない (接続拒否・名前解決失敗)
    no_response  : 接続はできたが応答が返らない (タイムアウト・接続リセット)
    """
    reason = getattr(exc, "reason", None)
    cause = reason if isinstance(reason, BaseException) else exc

    if isinstance(cause, ConnectionRefusedError):
        return TRANSPORT_UNREACHABLE
    if isinstance(cause, (ConnectionResetError, TimeoutError, socket.timeout)):
        return TRANSPORT_NO_RESPONSE
    if isinstance(cause, socket.gaierror):
        return TRANSPORT_UNREACHABLE

    text = str(cause).lower()
    if ("timed out" in text or "timeout" in text
            or "reset" in text or "10054" in text or "10060" in text):
        return TRANSPORT_NO_RESPONSE
    if ("refused" in text or "10061" in text
            or "getaddrinfo" in text or "name or service" in text):
        return TRANSPORT_UNREACHABLE
    return TRANSPORT_UNREACHABLE


def http_get_json(url: str, token: str, timeout: float = 5.0) -> tuple[bool, int | None, Any]:
    """GET url with X-API-KEY.

    Returns (ok, status, content).  status is None when the request never reached
    kabu Station (transport error).
    """
    # urllib.request は毎回 "Connection: close" を送る。kabu Station は
    # このヘッダーを付けたリクエストに応答を返さず、接続を掴んだまま
    # タイムアウトさせる (2026-08-29 実測)。
    #
    #   最小ヘッダー                -> HTTP 401  0.01s
    #   + Content-Type              -> HTTP 401  0.00s
    #   + Accept-Encoding: identity -> HTTP 401  0.00s
    #   + Connection: close         -> 応答なし  5.00s  ★これだけが原因
    #
    # そのため起動時の preflight は必ず no_response となり、
    # main_database.py が毎回 abort していた。
    # requests は keep-alive で送るので影響を受けない。実際
    # kabu_api/positions.py など他の呼び出しは requests で正常に動いている。
    try:
        # X-Autostock-Token-Pin: この token をそのまま送る、という指示。
        # token_startup_policy の requests フックは kabu API 宛の X-API-KEY を
        # settings.ini の値へ差し替えるので、preflight では必ず pin する。
        # pin しないと「7ac1 を検証したつもりが別の token を送っていた」
        # という取り違えが起きる。
        res = requests.get(
            url,
            headers={
                "Content-Type": "application/json",
                "X-API-KEY": str(token or ""),
                "X-Autostock-Token-Pin": "1",
            },
            timeout=timeout,
        )
    except Exception as e:  # ConnectionError / Timeout / socket error
        # R18: 呼び出し側が unreachable / no_response を判別できるよう接頭辞を付ける
        return False, None, "%s: %s" % (classify_transport_error(e), e)

    raw = res.text or ""
    try:
        content = json.loads(raw) if raw else {}
    except Exception:
        content = raw

    if 200 <= res.status_code < 300:
        return True, int(res.status_code), content
    return False, int(res.status_code), content


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
        # no_response も transport の一種 (既存呼び出しの互換のため True のまま)
        return self.kind in (PF_TRANSPORT_ERROR, PF_NO_RESPONSE)

    @property
    def is_no_response(self) -> bool:
        """接続はできたが応答が返らない (listen はしている)。"""
        return self.kind == PF_NO_RESPONSE

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
            logger.warning(
                "[TOKEN PREFLIGHT] pid=%s endpoint=%s status=%s token_tail=%s label=%s",
                os.getpid(), endpoint, status, token_tail(token), label or "-",
            )
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

        kind = (PF_NO_RESPONSE
                if str(content or "").startswith(TRANSPORT_NO_RESPONSE + ":")
                else PF_TRANSPORT_ERROR)
        transport_failure = PreflightResult(
            ok=False, kind=kind, endpoint=endpoint, status=None, content=content
        )
        logger.error(
            "%s %s TOKEN_ACTION=%s endpoint=%s kind=%s transport_error=%s",
            _LOG_PREFIX, label, ACTION_PREFLIGHT_NG, endpoint, kind,
            safe_content_text(content),
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


def _reread_settings_token() -> str:
    """settings.ini から token を読み直す (env / メモリキャッシュを見ない)。"""
    try:
        import token_manager  # type: ignore

        return str(token_manager.read_settings_token() or "").strip()
    except Exception:
        logger.debug("%s settings.ini re-read failed", _LOG_PREFIX, exc_info=True)
        return ""


def _log_token_settings(context: str) -> None:
    try:
        import token_manager  # type: ignore

        token_manager.log_token_settings_state(context)
    except Exception:
        logger.debug("%s token settings log failed", _LOG_PREFIX, exc_info=True)


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
    global _STARTUP_RECOVERY_ATTEMPTED, _STARTUP_TOKEN_CALLS, _LAST_RECOVERED_TOKEN

    read_cached = read_cached_token or _default_read_cached_token
    do_request = request_fresh_token or _default_request_fresh_token
    do_preflight = preflight or (lambda token, lbl: preflight_token(token, label=lbl))

    Mismatch, Transport, Generic = _token_api_exceptions()

    _log_token_settings(f"bootstrap_start/{label}")

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

        if pf.is_no_response:
            # R18: TCP は繋がっているのに応答が返らない。
            # 失効トークンに対して 401 ではなく RST / タイムアウトを返す実機が
            # あるため、「失効かもしれない」として再取得の対象にする。
            # (kabu Station が listen していることは認証エラーの即答で分かる)
            logger.error(
                "%s %s cached token got NO RESPONSE (connected but reset/timeout). %s "
                "kabu Station は listen しているので失効トークンの可能性がある。"
                "トークン再取得を 1 回だけ試みる。",
                _LOG_PREFIX, label, pf.detail(),
            )
            no_recovery_kind = FAIL_PREFLIGHT_NO_RESPONSE

        elif pf.is_transport_error:
            # TCP すら繋がらない = kabu Station が listen していない。
            # ここでトークンを取り直しても意味がないので従来どおり abort する。
            logger.error(
                "%s %s abort: kabu API preflight could not connect (TCP unreachable). %s "
                "Confirm kabu Station API is listening on localhost:18080.",
                _LOG_PREFIX, label, pf.detail(),
            )
            return BootstrapResult(
                ok=False, token=cached, source=cached_source, token_api_calls=0,
                failure_kind=FAIL_PREFLIGHT_TRANSPORT, detail=pf.detail(), preflight=pf,
            )

        else:
            # 発行直後のトークンが弾かれた場合は kabu Station のウォームアップ中
            # である可能性が高い。preflight だけを短時間で数回やり直す。
            recently_issued, token_age = _token_recently_issued(cached)
            self_issued = bool(cached) and cached == _LAST_RECOVERED_TOKEN
            if cached and (self_issued or recently_issued) and _warmup_retry_count() > 0:
                retries = _warmup_retry_count()
                wait = _warmup_retry_wait_sec()
                logger.warning(
                    "%s %s 発行直後のトークンが弾かれた -> kabu Station ウォームアップ中とみなし "
                    "preflight のみ最大 %d 回 (%.1fs 間隔) 再試行する。/token は叩き直さない。"
                    " token_tail=%s self_issued=%s token_age=%s window=%.0fs %s",
                    _LOG_PREFIX, label, retries, wait, token_tail(cached),
                    self_issued,
                    "-" if token_age is None else ("%.1fs" % token_age),
                    _warmup_window_sec(),
                    pf.detail(),
                )
                for i in range(1, retries + 1):
                    time.sleep(wait)
                    pf_w = do_preflight(cached, f"{label}/warmup{i}")
                    if pf_w.ok:
                        logger.warning(
                            "%s %s RESULT TOKEN_SOURCE=%s TOKEN_ACTION=%s token_api_calls=0 "
                            "warmup_retry=%d token_len=%d token_tail=%s "
                            "(起動直後の 401 はウォームアップだった)",
                            _LOG_PREFIX, label, cached_source, ACTION_PREFLIGHT_OK, i,
                            token_len(cached), token_tail(cached),
                        )
                        return BootstrapResult(
                            ok=True, token=cached, source=cached_source,
                            token_api_calls=0, preflight=pf_w,
                        )
                    if pf_w.is_transport_error:
                        pf = pf_w
                        break
                    pf = pf_w
                logger.error(
                    "%s %s ウォームアップ再試行 %d 回でも通らなかった -> 失効として扱う。 %s",
                    _LOG_PREFIX, label, retries, pf.detail(),
                )

            # Auth rejected -> the cached token is stale on the server side.
            logger.error(
                "%s %s cached token rejected by kabu API (stale cached token). %s",
                _LOG_PREFIX, label, pf.detail(),
            )
            no_recovery_kind = FAIL_RECOVERY_DISABLED

        if not allow_startup_recovery:
            return BootstrapResult(
                ok=False, token=cached, source=cached_source, token_api_calls=0,
                failure_kind=no_recovery_kind, detail=pf.detail(), preflight=pf,
            )
        if _startup_recovery_exhausted(cached):
            # 諦める前に settings.ini をもう一度読む。
            # 別プロセスが /token を発行し直していれば、こちらは叩かずに
            # その token を使えばよい (叩くと相手の token を殺してしまう)。
            reread = _reread_settings_token()
            if reread and reread != cached:
                logger.warning(
                    "%s %s recovery は使用済みだが settings.ini の token が更新されていた "
                    "old_tail=%s new_tail=%s -> 新しい token で preflight をやり直す "
                    "(/token は叩かない)",
                    _LOG_PREFIX, label, token_tail(cached), token_tail(reread),
                )
                pf_r = do_preflight(reread, f"{label}/reread")
                if pf_r.ok:
                    logger.warning(
                        "%s %s RESULT TOKEN_SOURCE=%s TOKEN_ACTION=%s token_api_calls=0 "
                        "token_len=%d token_tail=%s (別プロセスが発行した token を採用)",
                        _LOG_PREFIX, label, SOURCE_SETTINGS_INI_CACHED, ACTION_PREFLIGHT_OK,
                        token_len(reread), token_tail(reread),
                    )
                    try:
                        import token_manager  # type: ignore

                        token_manager._publish_token_all_layers(reread)
                    except Exception:
                        logger.debug("%s token publish after reread failed", _LOG_PREFIX, exc_info=True)
                    return BootstrapResult(
                        ok=True, token=reread, source=SOURCE_SETTINGS_INI_CACHED,
                        token_api_calls=0, preflight=pf_r,
                    )
                pf = pf_r

            logger.error(
                "%s %s startup token recovery exhausted in this process "
                "(succeeded=%s calls=%d max=%d); not calling /token again.",
                _LOG_PREFIX, label, _STARTUP_RECOVERY_ATTEMPTED,
                _STARTUP_TOKEN_CALLS, startup_token_max_calls(),
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
        if _startup_recovery_exhausted():
            logger.error(
                "%s %s no cached token and startup /token exhausted "
                "(succeeded=%s calls=%d max=%d).",
                _LOG_PREFIX, label, _STARTUP_RECOVERY_ATTEMPTED,
                _STARTUP_TOKEN_CALLS, startup_token_max_calls(),
            )
            return BootstrapResult(
                ok=False, token="", source="", token_api_calls=0,
                failure_kind=FAIL_RECOVERY_ALREADY_USED, detail="no cached token",
            )

    # ---- POST /token --------------------------------------------------
    # R18: フラグは「成功したら」立てる。ここでは試行回数だけ進める。
    _STARTUP_TOKEN_CALLS += 1
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

    # R18: ここまで来たら /token は成功している。以後この プロセスで
    # 取り直す理由は無いのでフラグを立てる (ループ防止は従来どおり)。
    _STARTUP_RECOVERY_ATTEMPTED = True
    _LAST_RECOVERED_TOKEN = fresh

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
    "PF_NO_RESPONSE",
    "TRANSPORT_UNREACHABLE",
    "TRANSPORT_NO_RESPONSE",
    "FAIL_PREFLIGHT_NO_RESPONSE",
    "classify_transport_error",
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
