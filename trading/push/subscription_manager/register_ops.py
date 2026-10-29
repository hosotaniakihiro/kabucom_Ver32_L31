# ============================================================
# File   : trading/push/subscription_manager/register_ops.py
# Version: PRODUCTION-STABLE-REV2.1-ABORT-REGISTER-ON-CLEAR-FAIL
# Function:
#   - register / unregister / clear の実行を担当する
#   - kabu Station 公式ひな形に合わせて HTTP PUT /kabusapi/register を使う
#   - WebSocket は受信専用、登録は HTTP API に分離する
#   - /kabusapi/unregister/all は公式ひな形に合わせて body なし PUT で送る
#   - unregister_all -> wait -> register の一連処理を直列化し、4002006 を抑止する
#   - clear/unregister が失敗した場合は register へ進まず即中断する
# ============================================================

from __future__ import annotations

import configparser
import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.error
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from kabu_api import kabu_http

from .globals_access import safe_get_global_data, safe_getattr
from .symbols import dedupe_keep_order, normalize_symbol
from .transport import is_transport_error, mark_transport_broken

logger = logging.getLogger(__name__)

REGISTER_CHUNK_SIZE = 50
# 2026-10-22: 枠 50 のうち既定で 2 つ空けて 48 までしか登録しない。
#   後場の実測で、kabu の RegistList が 48 件なのに 2 件の登録が 4002006、全解除の直後(RegistList=[])でも
#   50 件の登録が 4002006 になった(一覧に出ない形で枠が 1〜2 個使われている瞬間がある)。
#   そのたびに全解除 → 再登録も失敗 → 次の周期まで登録ゼロで、12:35 と 12:46 に PUSH が約 3 分ずつ全部止まった。
#   PUSH_REGISTER_HEADROOM=0 で従来(50)に戻る。
REGISTER_HEADROOM = max(0, min(10, int(os.environ.get("PUSH_REGISTER_HEADROOM", "2") or 0)))
REGISTER_LIMIT = 50 - REGISTER_HEADROOM
# 全解除のあとの登録も 4002006 で断られたとき、次の周期まで待たずに何件減らして入れ直すか(0 で入れ直さない)。
REGISTER_COUNT_ERROR_SHRINK = max(0, int(os.environ.get("PUSH_REGISTER_COUNT_ERROR_SHRINK", "5") or 0))
DEFAULT_EXCHANGE = 1

DEFAULT_UNREGISTER_TO_REGISTER_WAIT_SEC = float(os.environ.get("KABU_REGISTER_UNREGISTER_WAIT_SEC", "0.5"))
REGISTER_COUNT_ERROR_RETRY_WAIT_SEC = float(os.environ.get("KABU_REGISTER_COUNT_ERROR_RETRY_WAIT_SEC", "1.0"))

DEFAULT_BASE_URL = "http://localhost:18080/kabusapi"
DEFAULT_REGISTER_URL = f"{DEFAULT_BASE_URL}/register"
DEFAULT_UNREGISTER_ALL_URL = f"{DEFAULT_BASE_URL}/unregister/all"
DEFAULT_UNREGISTER_URL = f"{DEFAULT_BASE_URL}/unregister"

_SEQUENCE_LOCK = threading.RLock()

# 2026-10-22: kabu の応答(register / unregister / unregister/all の RegistList)を「今の登録」の正とする。
#   差分登録はこれまでプログラム側の記憶(last_registered_symbols)を current にしていたが、
#   部分失敗を成功扱いにした後などに実際の登録とずれ、10/21 は
#   「current=1 なのに kabu には 46 登録 → add=49 で 4002006」「RegistList mismatch → 全解除」が起きていた
#   (4002006 131 回・4001019 49 回・unregister/all 242 回。全解除のたびに全銘柄の PUSH が止まる)。
#   応答の一覧を覚えておき、次の差分の current に使う。PUSH_REGISTER_VENDOR_LIST_ENABLED=0 で従来どおり。
_VENDOR_LOCK = threading.Lock()
_VENDOR_REGISTERED: Optional[list] = None


def _note_vendor_list(content: Any) -> None:
    global _VENDOR_REGISTERED
    if isinstance(content, dict) and isinstance(content.get("RegistList"), list):
        syms = _registered_symbols_from_content(content)
        with _VENDOR_LOCK:
            _VENDOR_REGISTERED = list(syms)


def vendor_registered_symbols() -> Optional[list]:
    """直近の kabu 応答に載っていた登録銘柄。まだ応答が無い・無効なら None。"""
    if not _env_bool("PUSH_REGISTER_VENDOR_LIST_ENABLED", True):
        return None
    with _VENDOR_LOCK:
        return None if _VENDOR_REGISTERED is None else list(_VENDOR_REGISTERED)
_FILE_LOCK_HANDLE: Any = None


def _safe_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v).strip()
    except Exception:
        return ""


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = False) -> bool:
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


def _normalize_symbols(symbols: Sequence[str]) -> list[str]:
    out = [normalize_symbol(x) for x in symbols or []]
    out = [s for s in out if s]
    return dedupe_keep_order(out)


def _truncate_to_register_limit(symbols: Sequence[str]) -> list[str]:
    normalized = _normalize_symbols(symbols)
    if len(normalized) > REGISTER_LIMIT:
        logger.warning(
            "[SUB MANAGER] register target truncated limit=%s requested=%s dropped=%s",
            REGISTER_LIMIT,
            len(normalized),
            len(normalized) - REGISTER_LIMIT,
        )
        return normalized[:REGISTER_LIMIT]
    return normalized


def make_symbol_objects(chunk: Sequence[str], exchange: int = DEFAULT_EXCHANGE) -> list[dict]:
    ex = _safe_int(exchange, DEFAULT_EXCHANGE) or DEFAULT_EXCHANGE
    return [{"Symbol": str(s), "Exchange": ex} for s in chunk if s]


def _clean_api_key(v: Any) -> str:
    s = _safe_str(v)
    if not s:
        return ""
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s.strip().strip('"').strip("'").strip()


def _extract_api_key_like(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in (
            "X-API-KEY", "x-api-key", "api_key", "apikey", "kabu_api_key",
            "kabusapi_api_key", "token", "kabu_token", "kabusapi_token",
        ):
            if k in v:
                s = _clean_api_key(v.get(k))
                if s:
                    return s
    return _clean_api_key(v)


def _candidate_ini_paths() -> list[str]:
    here = os.getcwd()
    candidates = [
        os.path.join(here, "settings.ini"),
        os.path.join(here, "config", "settings.ini"),
    ]
    for env_name in ("SETTINGS_INI_PATH", "KABU_SETTINGS_INI"):
        p = _safe_str(os.environ.get(env_name))
        if p:
            candidates.insert(0, p)
    gd = safe_get_global_data()
    if gd is not None:
        for name in ("setting_ini_path", "settings_path", "config_path", "ini_path"):
            p = _safe_str(safe_getattr(gd, name, None))
            if p and os.path.basename(p).lower() == "settings.ini":
                candidates.insert(0, p)
    out: list[str] = []
    seen = set()
    for p in candidates:
        if p and p not in seen:
            out.append(p)
            seen.add(p)
    return out


def _read_aukabu_ini() -> tuple[str, str]:
    for path in _candidate_ini_paths():
        try:
            if not os.path.exists(path):
                continue
            cp = configparser.ConfigParser()
            cp.read(path, encoding="utf-8-sig")
            sec = "aukabu" if cp.has_section("aukabu") else "kabusapi" if cp.has_section("kabusapi") else ""
            if not sec:
                continue
            token = _clean_api_key(cp.get(sec, "token", fallback=""))
            apipassword = _safe_str(cp.get(sec, "apipassword", fallback=""))
            if token or apipassword:
                return token, apipassword
        except Exception:
            logger.exception("[SUB MANAGER] settings.ini read failed path=%s", path)
    return "", ""


def _get_global_api_key() -> str:
    gd = safe_get_global_data()
    if gd is None:
        return ""
    candidates = (
        "kabu_api_key", "kabusapi_api_key", "api_key", "apikey", "token",
        "kabu_token", "kabusapi_token", "X_API_KEY", "x_api_key", "headers",
        "request_headers", "settings", "config",
    )
    for name in candidates:
        try:
            s = _extract_api_key_like(safe_getattr(gd, name, None))
            if s:
                return s
        except Exception:
            continue
    try:
        if isinstance(gd, dict):
            for name in candidates:
                s = _extract_api_key_like(gd.get(name))
                if s:
                    return s
    except Exception:
        pass
    return ""


def _get_env_api_key() -> str:
    for key in (
        "KABU_API_KEY", "KABUSAPI_API_KEY", "X_API_KEY", "KABU_TOKEN",
        "KABUSAPI_TOKEN", "KABU_API_TOKEN", "AUKABU_TOKEN", "API_TOKEN", "TOKEN",
    ):
        s = _extract_api_key_like(os.environ.get(key))
        if s:
            return s
    return ""


def _get_settings_api_key() -> str:
    for module_name in ("settings", "config", "core.settings", "core.config"):
        try:
            mod = __import__(module_name, fromlist=["*"])
            for name in (
                "KABU_API_KEY", "KABUSAPI_API_KEY", "X_API_KEY", "KABU_TOKEN",
                "KABUSAPI_TOKEN", "KABU_API_TOKEN", "AUKABU_TOKEN", "API_TOKEN", "TOKEN", "HEADERS",
            ):
                if hasattr(mod, name):
                    s = _extract_api_key_like(getattr(mod, name))
                    if s:
                        logger.info("[SUB MANAGER] API key resolved from %s.%s", module_name, name)
                        return s
        except Exception:
            continue
    return ""


def _get_ini_api_key() -> str:
    token, _apipassword = _read_aukabu_ini()
    return token or ""


# ============================================================
# canonical token resolution (旧 core/startup/kabusapi_token_retry_register_patch.py)
#
# 運用方針: token は起動時に親プロセス(main_database.py/main.py)が一度だけ取得して
# settings.ini/runtime cacheに保存する。子プロセス(push_receiver_runner.py等)は
# /token を呼ばず、または token_manager の子プロセスガードを迂回してはならない。
# register/unregister は常にこの canonical token を使い、4001009/APIキー不一致は
# 実際の認証失敗としてログに残す。
# ============================================================

_KABUSAPI_TOKEN_ENV_KEYS = (
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

_KABUSAPI_CHILD_MARKERS = (
    "push_receiver_runner.py",
    "ranking_collector_runner.py",
    "summary_database_runner.py",
    "yahoo_complement_runner.py",
    "db_prepare_runner.py",
)

_KABUSAPI_TOKEN_BOOTSTRAPPED = False
_KABUSAPI_TOKEN_LOCK = threading.RLock()


def _kabusapi_token_env_bool(name: str, default: bool) -> bool:
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


def _kabusapi_token_argv_text() -> str:
    try:
        return " ".join(str(x) for x in getattr(sys, "argv", [])).lower()
    except Exception:
        return ""


def _kabusapi_token_is_child_process() -> bool:
    txt = _kabusapi_token_argv_text()
    if any(marker in txt for marker in _KABUSAPI_CHILD_MARKERS):
        return True
    for key in ("DATA_COLLECTOR_CHILD", "KABU_CHILD_PROCESS", "IS_CHILD_PROCESS"):
        if _kabusapi_token_env_bool(key, False):
            return True
    return False


def _kabusapi_token_is_parent_process() -> bool:
    txt = _kabusapi_token_argv_text()
    return "main_database.py" in txt or "main.py" in txt


def _kabusapi_token_publish(token: str) -> str:
    token = _safe_str(token)
    if not token:
        return ""
    for k in _KABUSAPI_TOKEN_ENV_KEYS:
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


def _kabusapi_token_settings_paths() -> list[Path]:
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
        here = Path(__file__).resolve().parents[3]
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


def _kabusapi_token_read_settings_ini() -> str:
    for path in _kabusapi_token_settings_paths():
        try:
            if not path.exists():
                continue
            cp = configparser.ConfigParser()
            cp.read(path, encoding="utf-8-sig")
            for sec in ("aukabu", "kabusapi"):
                if cp.has_section(sec):
                    token = _safe_str(cp.get(sec, "token", fallback=""))
                    if token:
                        return _kabusapi_token_publish(token)
        except Exception:
            logger.debug("[SUB MANAGER] canonical token settings.ini read failed path=%s", path, exc_info=True)
    return ""


def _kabusapi_canonical_token() -> tuple[str, str]:
    """Return the canonical token and source for register/unregister HTTP calls.

    This intentionally ignores the api_key argument callers may hold, because older
    recovery paths may carry stale env/global tokens.  The canonical order is:
      1. token_manager.API_TOKEN, which should be populated by parent startup/preflight
      2. settings.ini [aukabu]/[kabusapi] token
      3. explicit token environment variables as last fallback
    """
    try:
        import token_manager
        token = _safe_str(getattr(token_manager, "API_TOKEN", None))
        if token:
            return _kabusapi_token_publish(token), "token_manager.API_TOKEN"
    except Exception:
        pass

    token = _kabusapi_token_read_settings_ini()
    if token:
        return token, "settings.ini"

    for key in _KABUSAPI_TOKEN_ENV_KEYS:
        token = _safe_str(os.environ.get(key))
        if token:
            return _kabusapi_token_publish(token), f"env.{key}"
    return "", "none"


def _kabusapi_token_bootstrap_once() -> str:
    """Acquire no token in child processes; children only read the parent-issued token.

    Calling token_manager.refresh_token._original() directly here would bypass
    token_manager's child-process guard and let a child call /token after the
    parent preflight, which can make the token used by other child processes
    inconsistent. Only the public refresh_token() wrapper is used, and only
    when this is genuinely the parent process.
    """
    global _KABUSAPI_TOKEN_BOOTSTRAPPED
    with _KABUSAPI_TOKEN_LOCK:
        if _KABUSAPI_TOKEN_BOOTSTRAPPED:
            token, _source = _kabusapi_canonical_token()
            return token
        _KABUSAPI_TOKEN_BOOTSTRAPPED = True

        if _kabusapi_token_is_child_process() and not _kabusapi_token_env_bool("KABU_REGISTER_ALLOW_CHILD_BOOTSTRAP_TOKEN", False):
            token, source = _kabusapi_canonical_token()
            logger.warning(
                "[SUB MANAGER] child process detected; /token bootstrap skipped source=%s token_len=%d argv=%s",
                source, len(token), _kabusapi_token_argv_text(),
            )
            return token

        if (not _kabusapi_token_is_parent_process()) and not _kabusapi_token_env_bool("KABU_REGISTER_ALLOW_NONPARENT_BOOTSTRAP_TOKEN", False):
            token, source = _kabusapi_canonical_token()
            logger.warning(
                "[SUB MANAGER] non-parent process; /token bootstrap skipped source=%s token_len=%d argv=%s",
                source, len(token), _kabusapi_token_argv_text(),
            )
            return token

        if not _kabusapi_token_env_bool("KABU_REGISTER_BOOTSTRAP_TOKEN_ON_INSTALL", True):
            token, source = _kabusapi_canonical_token()
            logger.warning(
                "[SUB MANAGER] bootstrap disabled; using existing token source=%s token_len=%d",
                source, len(token),
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
                    _kabusapi_token_publish(token)
                    os.environ["KABU_REGISTER_BOOTSTRAP_DONE"] = "1"
                    logger.warning(
                        "[SUB MANAGER] parent startup bootstrap token refreshed/saved before register token_len=%d",
                        len(token),
                    )
                    return token
                logger.error("[SUB MANAGER] startup bootstrap returned empty token before register")
            else:
                logger.error("[SUB MANAGER] token_manager.refresh_token missing; cannot bootstrap before register")
        except Exception:
            logger.exception("[SUB MANAGER] startup bootstrap token refresh failed before register")

        token, source = _kabusapi_canonical_token()
        logger.warning(
            "[SUB MANAGER] fallback to existing token after bootstrap failure source=%s token_len=%d",
            source, len(token),
        )
        return token


def _resolve_api_key() -> str:
    _kabusapi_token_bootstrap_once()
    token, source = _kabusapi_canonical_token()
    if token:
        logger.info("[SUB MANAGER] resolved canonical token source=%s token_len=%d", source, len(token))
        return token

    for getter in (_get_global_api_key, _get_env_api_key, _get_settings_api_key, _get_ini_api_key):
        api_key = getter()
        if api_key:
            logger.warning("[SUB MANAGER] canonical token missing; fallback original resolver token_len=%d", len(api_key))
            return _kabusapi_token_publish(api_key)
    logger.error("[SUB MANAGER] API key unavailable")
    logger.warning("[SUB MANAGER] no startup/settings.ini token available; runtime refresh disabled")
    return ""


def _normalize_http_url(url: Any) -> str:
    s = _safe_str(url)
    if not s:
        return ""
    if s.lower().startswith(("http://", "https://")):
        return s.rstrip("/")
    return ""


def _normalize_base_url(url: Any) -> str:
    return _normalize_http_url(url)


def _get_global_base_url() -> str:
    gd = safe_get_global_data()
    if gd is None:
        return ""
    for name in ("kabusapi_base_url", "kabu_station_api_url", "push_api_base_url", "api_base_url", "base_url"):
        v = _normalize_base_url(safe_getattr(gd, name, None))
        if v:
            return v
    for name in ("kabusapi_websocket_url", "push_websocket_url", "websocket_url", "ws_url"):
        ws = _safe_str(safe_getattr(gd, name, None))
        if not ws:
            continue
        s = ws.strip()
        if s.startswith("ws://"):
            s = "http://" + s[len("ws://"):]
        elif s.startswith("wss://"):
            s = "https://" + s[len("wss://"):]
        if s.endswith("/websocket"):
            s = s[: -len("/websocket")]
        s = s.rstrip("/")
        if s.lower().startswith(("http://", "https://")):
            return s
    return ""


def _get_env_base_url() -> str:
    for key in ("KABUSAPI_BASE_URL", "KABU_STATION_API_URL", "KABUSAPI_URL", "PUSH_API_BASE_URL", "API_BASE_URL"):
        s = _normalize_base_url(os.environ.get(key))
        if s:
            return s
    for key in ("KABUSAPI_WEBSOCKET_URL", "PUSH_WEBSOCKET_URL", "WEBSOCKET_URL", "WS_URL"):
        ws = _safe_str(os.environ.get(key))
        if not ws:
            continue
        s = ws.strip()
        if s.startswith("ws://"):
            s = "http://" + s[len("ws://"):]
        elif s.startswith("wss://"):
            s = "https://" + s[len("wss://"):]
        if s.endswith("/websocket"):
            s = s[: -len("/websocket")]
        s = s.rstrip("/")
        if s.lower().startswith(("http://", "https://")):
            return s
    return ""


def _resolve_base_url() -> str:
    return _get_global_base_url() or _get_env_base_url() or DEFAULT_BASE_URL


def _join_url(base_url: str, endpoint: str) -> str:
    base = _normalize_base_url(base_url)
    ep = _safe_str(endpoint)
    if not base or not ep:
        return ""
    if not ep.startswith("/"):
        ep = "/" + ep
    return f"{base}{ep}"


def _resolve_url_from_candidates(*, global_names: Sequence[str], env_names: Sequence[str], endpoint: str, default_url: str) -> str:
    gd = safe_get_global_data()
    if gd is not None:
        for name in global_names:
            v = _normalize_http_url(safe_getattr(gd, name, None))
            if v:
                return v
    for key in env_names:
        v = _normalize_http_url(os.environ.get(key))
        if v:
            return v
    return _join_url(_resolve_base_url(), endpoint) or _normalize_http_url(default_url)


def _resolve_register_url() -> str:
    return _resolve_url_from_candidates(
        global_names=("kabusapi_register_url", "push_register_url", "register_url"),
        env_names=("KABUSAPI_REGISTER_URL", "PUSH_REGISTER_URL", "REGISTER_URL"),
        endpoint="/register",
        default_url=DEFAULT_REGISTER_URL,
    )


def _resolve_unregister_all_url() -> str:
    return _resolve_url_from_candidates(
        global_names=("kabusapi_unregister_all_url", "push_unregister_all_url", "unregister_all_url"),
        env_names=("KABUSAPI_UNREGISTER_ALL_URL", "PUSH_UNREGISTER_ALL_URL", "UNREGISTER_ALL_URL"),
        endpoint="/unregister/all",
        default_url=DEFAULT_UNREGISTER_ALL_URL,
    )


def _resolve_unregister_url() -> str:
    return _resolve_url_from_candidates(
        global_names=("kabusapi_unregister_url", "push_unregister_url", "unregister_url"),
        env_names=("KABUSAPI_UNREGISTER_URL", "PUSH_UNREGISTER_URL", "UNREGISTER_URL"),
        endpoint="/unregister",
        default_url=DEFAULT_UNREGISTER_URL,
    )


def _registration_endpoint(url: str) -> bool:
    u = str(url or "").lower()
    return "/register" in u or "/unregister" in u


def _guard_registration_owner(url: str) -> tuple[bool, Any]:
    """PUSH WebSocket 非所有プロセスからの register/unregister を止める。

    戻り値: (続行してよいか, 拒否時のレスポンス相当)
    """
    if not _registration_endpoint(url):
        return True, None
    try:
        from trading.push.push_stream.ws_owner import registration_allowed
    except Exception:
        return True, None
    try:
        allowed, holder = registration_allowed("register_ops")
    except Exception:
        logger.debug("[SUB MANAGER] registration owner check failed -> allow", exc_info=True)
        return True, None
    if allowed:
        return True, None
    logger.error(
        "[SUB MANAGER][MULTI_WS_DETECTED] register/unregister skipped: "
        "このプロセスは PUSH WebSocket の所有者ではない pid=%s url=%s holder=%s "
        "(register リストはセッション共有資源。所有プロセスだけが操作する)",
        os.getpid(), url, holder,
    )
    return False, {"Code": "SKIPPED_NOT_PUSH_WS_OWNER", "Message": "not push ws owner"}


def _http_json_request(*, url: str, method: str, payload: Optional[dict], api_key: str, timeout: float = 10.0) -> tuple[bool, Any]:
    ok_to_run, blocked = _guard_registration_owner(url)
    if not ok_to_run:
        return False, blocked
    """canonical token で api_key を上書きし、認証ミスマッチを診断ログに残す。

    旧 core/startup/kabusapi_token_retry_register_patch.py の
    _http_json_request_patched をここへ統合。実際のHTTP送受信は
    _http_json_request_impl が担う (元の _http_json_request 本体)。
    """
    token, source = _kabusapi_canonical_token()
    arg_len = len(_safe_str(api_key))
    if not token:
        token = _safe_str(api_key) or _resolve_api_key()
        source = "api_key_arg_or_resolver"
    if arg_len and _safe_str(api_key) != token:
        logger.warning(
            "[SUB MANAGER] overriding stale api_key arg for %s %s arg_len=%d canonical_source=%s canonical_len=%d",
            method, url, arg_len, source, len(token),
        )
    else:
        logger.info(
            "[SUB MANAGER] using canonical token for %s %s source=%s token_len=%d",
            method, url, source, len(token),
        )
    ok, content = _http_json_request_impl(url=url, method=method, payload=payload, api_key=token, timeout=timeout)
    if ok or not _is_api_key_mismatch(content):
        return ok, content
    logger.error(
        "[SUB MANAGER] API key mismatch method=%s url=%s canonical_source=%s token_len=%d arg_len=%d runtime_refresh_disabled content=%r",
        method, url, source, len(token), arg_len, content,
    )
    return ok, content


def _http_json_request_impl(*, url: str, method: str, payload: Optional[dict], api_key: str, timeout: float = 10.0) -> tuple[bool, Any]:
    url = _normalize_http_url(url)
    method = (_safe_str(method) or "PUT").upper()
    if not url:
        logger.warning("[SUB MANAGER] HTTP %s skipped: invalid url=%r", method, url)
        return False, "invalid_url"
    # 送信は kabu_api.kabu_http に集約する。urllib.request は毎回
    # "Connection: close" を送り、kabu Station はそれに応答を返さない。
    # PUT /kabusapi/register が常に "timed out" になっていた原因はこれで、
    # kabu Station 側の不調ではなかった (詳細は kabu_api/kabu_http.py)。
    try:
        content = kabu_http.request_json(
            url,
            method=method,
            payload=payload,
            api_key=api_key,
            timeout=timeout,
        )
        return True, content
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
            content = json.loads(raw.decode("utf-8")) if raw else str(e)
        except Exception:
            content = str(e)
        return False, content
    except Exception as e:
        logger.warning("[SUB MANAGER] HTTP %s failed url=%s err=%s", method, url, e)
        if is_transport_error(e):
            mark_transport_broken(reason=f"http_{method.lower()}", exc=e)
        return False, str(e)


def _is_register_count_error(content: Any) -> bool:
    try:
        if isinstance(content, dict):
            code = str(content.get("Code") or "")
            msg = str(content.get("Message") or "")
            return code == "4002006" or "レジスト数" in msg or ("register" in msg.lower() and "count" in msg.lower())
        s = str(content)
        return "4002006" in s or "レジスト数" in s
    except Exception:
        return False


def _is_partial_register_error(content: Any) -> bool:
    try:
        if isinstance(content, dict):
            code = str(content.get("Code") or "")
            msg = str(content.get("Message") or "")
            return code == "4001019" or "一部の銘柄が登録できませんでした" in msg
        s = str(content)
        return "4001019" in s or "一部の銘柄が登録できませんでした" in s
    except Exception:
        return False


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


def _abort_on_clear_failure() -> bool:
    return _env_bool("PUSH_REGISTER_ABORT_IF_CLEAR_FAILED", True)


def _skip_clear_when_empty() -> bool:
    """登録済みが 0 件のとき unregister/all を省略するか。

    PUSH_REGISTER_SKIP_CLEAR_WHEN_EMPTY=0 で従来動作 (必ず呼ぶ)。
    """
    return _env_bool("PUSH_REGISTER_SKIP_CLEAR_WHEN_EMPTY", True)


def _sequence_lock_path() -> str:
    return os.getenv("KABU_REGISTER_SEQUENCE_LOCK_PATH") or os.path.join(
        tempfile.gettempdir(), "autostock_kabustation_register_sequence.lock"
    )


@contextlib.contextmanager
def _registration_sequence_lock(reason: str = "unknown") -> Iterator[None]:
    file_lock_enabled = _env_bool("KABU_REGISTER_SEQUENCE_FILE_LOCK", True)
    timeout_sec = max(0.5, _safe_float(os.getenv("KABU_REGISTER_SEQUENCE_LOCK_TIMEOUT_SEC"), 20.0))
    poll_sec = max(0.05, _safe_float(os.getenv("KABU_REGISTER_SEQUENCE_LOCK_POLL_SEC"), 0.10))
    path = _sequence_lock_path()
    fh = None
    started = time.monotonic()
    with _SEQUENCE_LOCK:
        if file_lock_enabled:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
            except Exception:
                pass
            try:
                fh = open(path, "a+", encoding="utf-8")
                while True:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            fh.seek(0)
                            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fh.seek(0)
                        fh.truncate()
                        fh.write(f"pid={os.getpid()} reason={reason} acquired_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                        fh.flush()
                        logger.info("[SUB MANAGER] register sequence lock acquired reason=%s path=%s", reason, path)
                        break
                    except OSError:
                        if time.monotonic() - started >= timeout_sec:
                            logger.warning("[SUB MANAGER] register sequence lock timeout reason=%s path=%s -> continue without file lock", reason, path)
                            try:
                                fh.close()
                            except Exception:
                                pass
                            fh = None
                            break
                        time.sleep(poll_sec)
            except Exception:
                logger.exception("[SUB MANAGER] register sequence file lock failed reason=%s path=%s -> continue", reason, path)
                try:
                    if fh is not None:
                        fh.close()
                except Exception:
                    pass
                fh = None
        try:
            yield
        finally:
            if fh is not None:
                try:
                    if os.name == "nt":
                        import msvcrt
                        fh.seek(0)
                        try:
                            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                        except Exception:
                            pass
                    else:
                        import fcntl
                        try:
                            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                        except Exception:
                            pass
                    logger.info("[SUB MANAGER] register sequence lock released reason=%s path=%s", reason, path)
                finally:
                    try:
                        fh.close()
                    except Exception:
                        pass


def run_unregister_all() -> bool:
    api_key = _resolve_api_key()
    url = _resolve_unregister_all_url()
    if not url:
        logger.warning("[SUB MANAGER] unregister_all skipped: URL unavailable")
        return True
    if not api_key:
        logger.warning("[SUB MANAGER] unregister_all skipped: API key unavailable")
        return False
    ok, content = _http_json_request(url=url, method="PUT", payload=None, api_key=api_key)
    logger.info("[SUB MANAGER] unregister_all done vendor_body=None ok=%s content=%r", ok, content)
    if ok:
        _note_vendor_list(content)
    return ok


def run_unregister_symbols(symbols: Sequence[str]) -> bool:
    normalized = _normalize_symbols(symbols)
    if not normalized:
        return True
    api_key = _resolve_api_key()
    url = _resolve_unregister_url()
    if not url:
        logger.warning("[SUB MANAGER] unregister_symbols skipped: URL unavailable")
        return True
    if not api_key:
        logger.warning("[SUB MANAGER] unregister_symbols skipped: API key unavailable")
        return False
    payload = {"Symbols": make_symbol_objects(normalized)}
    ok, content = _http_json_request(url=url, method="PUT", payload=payload, api_key=api_key)
    logger.info("[SUB MANAGER] unregister_symbols done count=%d ok=%s content=%r", len(normalized), ok, content)
    if ok:
        _note_vendor_list(content)
    return ok


def _register_once_with_content(symbols: Sequence[str], *, exchange: int = DEFAULT_EXCHANGE) -> tuple[bool, Any]:
    normalized = _truncate_to_register_limit(symbols)
    if not normalized:
        return True, {"skipped": "empty"}
    api_key = _resolve_api_key()
    url = _resolve_register_url()
    if not api_key or not url:
        return False, "api_key_or_url_unavailable"
    payload = {"Symbols": make_symbol_objects(normalized, exchange=exchange)}
    ok, content = _http_json_request(url=url, method="PUT", payload=payload, api_key=api_key)
    logger.info("[SUB MANAGER] register done size=%d ok=%s content=%r", len(normalized), ok, content)
    if ok:
        _note_vendor_list(content)
    if not ok:
        # 2026-09-16: 4001019 (一部の銘柄が登録できませんでした) は応答に銘柄が含まれない。
        #   どの銘柄が弾かれたかを後から特定できるよう、送った銘柄を必ず残す
        #   (次に成功した register の RegistList と突き合わせる)。
        try:
            logger.warning("[SUB MANAGER][REGISTER NG SYMBOLS] size=%d content=%r symbols=%s",
                           len(normalized), content, sorted(normalized))
        except Exception:
            pass
    return ok, content


def _run_register_chunks_core(symbols: Sequence[str], *, exchange: int = DEFAULT_EXCHANGE, chunk_size: int = REGISTER_CHUNK_SIZE) -> bool:
    del chunk_size
    normalized = _truncate_to_register_limit(symbols)
    if not normalized:
        logger.info("[SUB MANAGER] register skipped empty target")
        return True
    with _registration_sequence_lock("register_chunks"):
        ok, content = _register_once_with_content(normalized, exchange=exchange)
    if ok:
        return True
    if _is_partial_register_error(content):
        logger.warning("[SUB MANAGER] register partial accepted as usable size=%d content=%r", len(normalized), content)
        return True
    return False


def _registered_symbols_from_content(content: Any) -> list[str]:
    """register/unregister のレスポンス RegistList から銘柄コード一覧を取り出す。"""
    try:
        if not isinstance(content, dict):
            return []
        items = content.get("RegistList")
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for it in items:
            if isinstance(it, dict):
                sym = str(it.get("Symbol") or "").strip()
            else:
                sym = str(it or "").strip()
            if sym:
                out.append(sym)
        return _normalize_symbols(out)
    except Exception:
        return []


def _try_differential_register(
    current_symbols: Sequence[str],
    target_symbols: Sequence[str],
    *,
    exchange: int,
) -> Optional[bool]:
    """差分だけ unregister/register する。

    kabu Station の PUT /register は「追加」、PUT /unregister は「指定解除」なので、
    A -> B の入れ替えは (A-B) を unregister し (B-A) を register すれば足りる。
    毎回 /unregister/all を撃つと登録ゼロの瞬間ができ、
    固定枠の銘柄まで PUSH が途切れる。

    戻り値:
      True/False -> 差分登録を実施した (成否)
      None       -> 差分登録を使わない (呼び出し側は従来の全解除→全登録へ)
    """
    if not _env_bool("PUSH_REGISTER_DIFFERENTIAL_ENABLED", True):
        return None

    current = _normalize_symbols(current_symbols)
    target = _truncate_to_register_limit(target_symbols)
    vendor = vendor_registered_symbols()
    if vendor is not None and set(vendor) != set(current):
        logger.warning(
            "[SUB MANAGER][DIFF REGISTER] current を kabu の直近 RegistList に合わせる program=%d vendor=%d "
            "only_program=%s only_vendor=%s",
            len(current), len(vendor), sorted(set(current) - set(vendor))[:10], sorted(set(vendor) - set(current))[:10],
        )
        current = vendor
    if not current or not target:
        return None

    cur_set = set(current)
    tgt_set = set(target)
    to_remove = [s for s in current if s not in tgt_set]
    to_add = [s for s in target if s not in cur_set]
    keep = [s for s in current if s in tgt_set]

    if not to_remove and not to_add:
        logger.info(
            "[SUB MANAGER][DIFF REGISTER] no change current=%d target=%d keep=%d",
            len(current), len(target), len(keep),
        )
        return True

    logger.info(
        "[SUB MANAGER][DIFF REGISTER] current=%d target=%d keep=%d remove=%d add=%d "
        "(全解除せず差分だけ更新する)",
        len(current), len(target), len(keep), len(to_remove), len(to_add),
    )

    # 2026-10-22: 解除→登録は待たずに続けて撃つ(kabu は解除の応答を返した時点で RegistList から消えている)。
    #   解除の応答の一覧で追加分を計算し直し、枠(50)を超えないことを確かめてから登録する。
    #
    # 2026-10-29: 入れ替えで登録が 0 件になる瞬間を作らない。
    #   残す銘柄 (keep) が 0 件のときは、解除する銘柄を 1 つだけ残して先に入れ替え、最後にその 1 つを入れ替える。
    trim = _diff_trim_extra_enabled()
    held_back: list[str] = []
    if trim and vendor is not None and to_remove and to_add and not keep:
        held_back = [to_remove[-1]]
        to_remove = to_remove[:-1]
        logger.info(
            "[SUB MANAGER][DIFF REGISTER] keep=0 のため 1 銘柄 %s を残して入れ替える (登録 0 件の瞬間を作らない)",
            held_back[0],
        )

    t0 = time.perf_counter()
    t_unreg = t0
    trimmed: list[str] = []
    if to_remove:
        if not run_unregister_symbols(to_remove):
            logger.warning(
                "[SUB MANAGER][DIFF REGISTER] unregister diff failed size=%d -> 全解除→全登録へフォールバック",
                len(to_remove),
            )
            return None
        t_unreg = time.perf_counter()
        after = vendor_registered_symbols()
        if after is not None:
            after_set = set(after)
            to_add = [s for s in target if s not in after_set]
            if len(after) + len(to_add) > REGISTER_LIMIT:
                if not trim:
                    logger.warning(
                        "[SUB MANAGER][DIFF REGISTER] 解除後も枠が足りない registered=%d add=%d limit=%d -> 全解除→全登録へ",
                        len(after), len(to_add), REGISTER_LIMIT,
                    )
                    return None
                # 2026-10-29: 解除の応答に、目標に無い銘柄が増えていることがある
                #   (10/29 前場の 154 件はすべて「直前の一覧に無かった銘柄が増えた」形で、こちらの解除は効いていた。
                #    この プロセス以外で登録できるのは kabu の /board・/symbol の自動登録だけ)。
                #   旧実装はここで全解除→全登録へ落ちて、登録 0 件の瞬間を作っていた (10/29 前場 171 回)。
                #   目標に無い銘柄 (はみ出し) だけを銘柄ごとに解除してから追加する。
                r = _unregister_extras_for_room(after, tgt_set, held=set(held_back), stage="after_unregister")
                if r is None:
                    return None
                trimmed += r
                after = vendor_registered_symbols() or []
                after_set = set(after)
                to_add = [s for s in target if s not in after_set]
                room = REGISTER_LIMIT - len(after)
                if len(to_add) > room:
                    # はみ出しを外しても足りない (=残しておいた 1 銘柄の分)。入るだけ入れ、残りは後段で入れる。
                    (logger.info if held_back else logger.warning)(
                        "[SUB MANAGER][DIFF REGISTER] はみ出しを外しても枠が足りない registered=%d add=%d limit=%d "
                        "-> 入る %d 件だけ先に登録する (全解除しない)",
                        len(after), len(to_add), REGISTER_LIMIT, max(0, room),
                    )
                    to_add = to_add[: max(0, room)]
    elif held_back:
        t_unreg = time.perf_counter()

    if held_back:
        # 残した 1 銘柄の分は、最初の登録では枠に入らないことがある。入るだけ先に入れる。
        cur_now = vendor_registered_symbols()
        if cur_now is not None:
            room = REGISTER_LIMIT - len(cur_now)
            to_add = [s for s in to_add if s not in set(cur_now)][: max(0, room)]

    if not to_add and not held_back:
        if trimmed:
            logger.info(
                "[SUB MANAGER][DIFF REGISTER] done removed=%d trimmed=%d added=0", len(to_remove), len(trimmed),
            )
        return True

    t_reg0 = time.perf_counter()
    if to_add:
        ok, content = _register_once_with_content(to_add, exchange=exchange)
    else:
        ok, content = True, {"RegistList": [{"Symbol": s} for s in (vendor_registered_symbols() or [])]}
    if ok and held_back:
        # 2 段目: 残しておいた 1 銘柄を解除し、まだ入っていない目標を登録する。
        if not run_unregister_symbols(held_back):
            logger.warning(
                "[SUB MANAGER][DIFF REGISTER] held-back unregister failed symbol=%s -> 全解除→全登録へフォールバック",
                held_back[0],
            )
            return None
        to_remove = to_remove + held_back
        cur_now = vendor_registered_symbols() or []
        rest = [s for s in target if s not in set(cur_now)]
        rest = rest[: max(0, REGISTER_LIMIT - len(cur_now))]
        if rest:
            ok, content = _register_once_with_content(rest, exchange=exchange)
            to_add = list(to_add) + rest
        else:
            content = {"RegistList": [{"Symbol": s} for s in cur_now]}
    t_reg1 = time.perf_counter()
    logger.info(
        "[SUB MANAGER][DIFF REGISTER][TIMING] unregister_ms=%.1f gap_ms=%.1f register_ms=%.1f "
        "unregister_to_register_done_ms=%.1f remove=%d add=%d ok=%s",
        (t_unreg - t0) * 1000, (t_reg0 - t_unreg) * 1000, (t_reg1 - t_reg0) * 1000,
        (t_reg1 - t0) * 1000, len(to_remove), len(to_add), ok,
    )
    if not ok:
        if _is_partial_register_error(content):
            logger.warning(
                "[SUB MANAGER][DIFF REGISTER] partial register accepted size=%d content=%r",
                len(to_add), content,
            )
            return True
        logger.warning(
            "[SUB MANAGER][DIFF REGISTER] register diff failed size=%d content=%r -> 全解除→全登録へフォールバック",
            len(to_add), content,
        )
        return None

    # vendor が返した実際の登録一覧と突き合わせる。
    # ここがズレたまま差分を続けると 4002006 レジスト数エラーになるので、
    # ズレたら一度だけ全解除→全登録へ落とす。
    # 2026-10-29: 目標に無い銘柄 (はみ出し) は銘柄ごとに外す。足りない銘柄は記録だけして次の周期に任せる
    #   (次の周期の current は kabu の一覧なので、足りない分は add に入る)。全解除はしない。
    actual = _registered_symbols_from_content(content)
    if actual and set(actual) != tgt_set:
        missing = sorted(tgt_set - set(actual))
        extra = sorted(set(actual) - tgt_set)
        if not trim:
            logger.warning(
                "[SUB MANAGER][DIFF REGISTER] vendor RegistList mismatch actual=%d target=%d "
                "missing=%s extra=%s -> 全解除→全登録で作り直す",
                len(actual), len(target), missing[:10], extra[:10],
            )
            return None
        logger.warning(
            "[SUB MANAGER][DIFF REGISTER] vendor RegistList mismatch actual=%d target=%d "
            "missing=%s extra=%s -> はみ出しだけ銘柄ごとに解除する (全解除しない)",
            len(actual), len(target), missing[:10], extra[:10],
        )
        if extra:
            r = _unregister_extras_for_room(actual, tgt_set, held=set(), stage="after_register")
            if r is None:
                return None
            trimmed += r

    logger.info(
        "[SUB MANAGER][DIFF REGISTER] done removed=%d trimmed=%d added=%d registered=%d",
        len(to_remove), len(trimmed), len(to_add),
        len(vendor_registered_symbols() or []) if actual else len(target),
    )
    return True


def _diff_trim_extra_enabled() -> bool:
    """差分登録で枠が足りない・一覧がずれたとき、全解除ではなく「はみ出し」の銘柄ごとの解除で収めるか。

    PUSH_REGISTER_DIFF_TRIM_EXTRA=0 で 2026-10-28 以前の動き (全解除→全登録へ落とす) に戻る。
    """
    return _env_bool("PUSH_REGISTER_DIFF_TRIM_EXTRA", True)


def _unregister_extras_for_room(
    registered: Sequence[str],
    target_set: set,
    *,
    held: set,
    stage: str,
) -> Optional[list]:
    """kabu の一覧のうち目標に無い銘柄 (held は除く) を銘柄ごとに解除する。

    戻り値: 解除した銘柄 (無ければ [])。解除に失敗したら None (呼び出し側は従来どおり全解除へ落とす)。
    """
    extra = [s for s in _normalize_symbols(registered) if s not in target_set and s not in held]
    if not extra:
        return []
    logger.warning(
        "[SUB MANAGER][DIFF REGISTER][TRIM] 目標に無い銘柄を銘柄ごとに解除する stage=%s registered=%d extra=%d head=%s "
        "(kabu の /board・/symbol の自動登録と見られる。全解除はしない)",
        stage, len(registered), len(extra), extra[:10],
    )
    if not run_unregister_symbols(extra):
        logger.warning(
            "[SUB MANAGER][DIFF REGISTER][TRIM] はみ出しの解除に失敗 stage=%s size=%d -> 全解除→全登録へフォールバック",
            stage, len(extra),
        )
        return None
    return extra


def _reg_state():
    """登録状態の正規所有者。取得できなければ None。"""
    try:
        from trading.push import registration_state

        return registration_state
    except Exception:
        logger.debug("[SUB MANAGER] registration_state unavailable", exc_info=True)
        return None


def _restore_last_good_batch(reason: str, *, exchange: int) -> bool:
    """unregister は通ったが register に失敗した状態からの復旧。

    直前に kabu が受理していたバッチを register し直す。
    ここで復旧できないと registered_count=0 のまま PUSH が止まる。
    """
    if not _env_bool("PUSH_REGISTER_RESTORE_LAST_GOOD", True):
        return False
    st = _reg_state()
    if st is None:
        return False
    last_good = st.last_good_registered_symbols()
    if not last_good:
        logger.error(
            "[SUB MANAGER][RESTORE] last_good が無いため復旧できない reason=%s "
            "(registered_count=0 のままになる)",
            reason,
        )
        return False

    target = _truncate_to_register_limit(last_good)
    logger.warning(
        "[SUB MANAGER][RESTORE] register に失敗したので直前の正常バッチを再登録する "
        "reason=%s size=%d head=%s",
        reason, len(target), target[:10],
    )
    ok, content = _register_once_with_content(target, exchange=exchange)
    if ok or _is_partial_register_error(content):
        st.mark_registered_confirmed(target, label="restore", source=reason)
        logger.warning(
            "[SUB MANAGER][RESTORE] 直前バッチの再登録に成功 size=%d reason=%s", len(target), reason
        )
        return True
    logger.error(
        "[SUB MANAGER][RESTORE] 直前バッチの再登録も失敗 reason=%s content=%r", reason, content
    )
    return False


def _run_refresh_sequence_core(
    current_symbols: Sequence[str],
    target_symbols: Sequence[str],
    *,
    clear_first: bool = False,
    unregister_first: bool = False,
    exchange: int = DEFAULT_EXCHANGE,
    chunk_size: int = REGISTER_CHUNK_SIZE,
    wait_after_clear_sec: float = DEFAULT_UNREGISTER_TO_REGISTER_WAIT_SEC,
    unregister_wait_sec: Optional[float] = None,
) -> bool:
    del chunk_size
    normalized_target = _truncate_to_register_limit(target_symbols)
    wait_sec = DEFAULT_UNREGISTER_TO_REGISTER_WAIT_SEC
    if unregister_wait_sec is not None:
        wait_sec = _safe_float(unregister_wait_sec, DEFAULT_UNREGISTER_TO_REGISTER_WAIT_SEC)
    elif wait_after_clear_sec is not None:
        wait_sec = _safe_float(wait_after_clear_sec, DEFAULT_UNREGISTER_TO_REGISTER_WAIT_SEC)
    wait_sec = max(0.0, float(wait_sec))

    reason = "clear_register" if clear_first else "unregister_register" if unregister_first else "register"

    # 事前条件 (2026-09-01):
    # 登録候補が空のときに unregister だけ通すと registered_count=0 になり、
    # PUSH が止まって Summary も Ranking 候補も作れなくなる循環に入る。
    # 空なら「今の登録を維持したまま」何もしない。
    if not normalized_target:
        st = _reg_state()
        confirmed = st.registered_count() if st is not None else -1
        logger.error(
            "[SUB MANAGER][ROTATION SKIP] 登録候補が空なので unregister/register を行わない "
            "reason=%s current=%d confirmed=%s (現在の登録を維持する)",
            reason, len(current_symbols or []), confirmed,
        )
        return False

    with _registration_sequence_lock(reason):
        logger.info(
            "[SUB MANAGER] refresh sequence start current=%d target=%d clear_first=%s unregister_first=%s wait_after_clear=%.3fs locked=True",
            len(current_symbols or []),
            len(normalized_target),
            bool(clear_first),
            bool(unregister_first),
            wait_sec,
        )

        if clear_first or unregister_first:
            diff_result = _try_differential_register(
                current_symbols, normalized_target, exchange=exchange,
            )
            if diff_result is True:
                return True
            if diff_result is False:
                return False

        if clear_first:
            # 登録済みが 0 件なら消すものが無い。
            #
            # 2026-08-31 14:21-14:37 の実測:
            #   [REGISTER AUDIT] refresh start current=0 target=50 clear_first=True
            #   PUT /kabusapi/unregister/all err=timed out            (10 秒)
            #   clear_first failed -> abort register                  (abort_if_clear_failed=True)
            # これが 4 回連続し、register done が 1 回も出ないまま
            # 銘柄が 1 つも登録されず PUSH 配信がゼロ (received_total=0) になっていた。
            #
            # 空リストに対する unregister/all は本来不要な REST 呼び出しで、
            # それが登録シーケンス全体を落としていた。呼ばずに register へ進む。
            # timeout を延ばすのでも、失敗を無視して進むのでもない
            # (current_symbols がある場合は従来どおり失敗で中止する)。
            if not current_symbols and _skip_clear_when_empty():
                logger.warning(
                    "[SUB MANAGER] clear_first skipped: 登録済み 0 件なので unregister/all を呼ばない "
                    "target=%d (空リストへの PUT がタイムアウトして register 全体を中止させていた)",
                    len(normalized_target),
                )
            else:
                ok = run_unregister_all()
                if not ok:
                    logger.error("[SUB MANAGER] clear_first failed -> abort register abort_if_clear_failed=%s current=%d", _abort_on_clear_failure(), len(current_symbols or []))
                    if _abort_on_clear_failure():
                        return False
            if wait_sec > 0:
                logger.info("[SUB MANAGER] wait after unregister all %.3fs before register size=%d", wait_sec, len(normalized_target))
                time.sleep(wait_sec)
        elif unregister_first and current_symbols:
            ok = run_unregister_symbols(current_symbols)
            if not ok:
                logger.error("[SUB MANAGER] unregister_first failed -> abort register abort_if_clear_failed=%s", _abort_on_clear_failure())
                if _abort_on_clear_failure():
                    return False
            if wait_sec > 0:
                logger.info("[SUB MANAGER] wait after unregister symbols %.3fs before register size=%d", wait_sec, len(normalized_target))
                time.sleep(wait_sec)

        st = _reg_state()
        if st is not None:
            st.set_requested(normalized_target, label=reason)

        ok, content = _register_once_with_content(normalized_target, exchange=exchange)
        if ok:
            if st is not None:
                st.mark_registered_confirmed(normalized_target, label=reason, source="refresh_sequence")
            return True

        if _is_partial_register_error(content):
            logger.warning(
                "[SUB MANAGER] register partial accepted as usable -> hold rotation size=%d content=%r",
                len(normalized_target),
                content,
            )
            if st is not None:
                st.mark_registered_confirmed(normalized_target, label=reason, source="refresh_partial")
            return True

        if _is_register_count_error(content):
            logger.warning(
                "[SUB MANAGER] register count error -> unregister_all and retry once wait=%.3fs content=%r locked=True",
                REGISTER_COUNT_ERROR_RETRY_WAIT_SEC,
                content,
            )
            clear_ok = run_unregister_all()
            if not clear_ok:
                logger.error("[SUB MANAGER] register count recovery unregister_all failed -> abort retry")
                return False
            if REGISTER_COUNT_ERROR_RETRY_WAIT_SEC > 0:
                time.sleep(REGISTER_COUNT_ERROR_RETRY_WAIT_SEC)
            ok2, content2 = _register_once_with_content(normalized_target, exchange=exchange)
            logger.info("[SUB MANAGER] register retry after clear ok=%s content=%r", ok2, content2)
            if ok2:
                if st is not None:
                    st.mark_registered_confirmed(normalized_target, label=reason, source="count_error_retry")
                return True
            if _is_partial_register_error(content2):
                logger.warning(
                    "[SUB MANAGER] register retry partial accepted as usable -> hold rotation size=%d content=%r",
                    len(normalized_target),
                    content2,
                )
                if st is not None:
                    st.mark_registered_confirmed(normalized_target, label=reason, source="count_error_retry_partial")
                return True
            # 2026-10-22: ここで諦めると次の周期(約 3 分後)まで登録ゼロのままになる。
            #   件数を減らしてすぐ入れ直す(先頭は保有・発注中の固定銘柄なので、落ちるのは順位の低い末尾)。
            if _is_register_count_error(content2) and REGISTER_COUNT_ERROR_SHRINK > 0 and len(normalized_target) > 1:
                shrunk = normalized_target[: max(1, len(normalized_target) - REGISTER_COUNT_ERROR_SHRINK)]
                ok3, content3 = _register_once_with_content(shrunk, exchange=exchange)
                logger.warning(
                    "[SUB MANAGER] register retry shrunk size=%d->%d ok=%s content=%r",
                    len(normalized_target), len(shrunk), ok3, str(content3)[:200],
                )
                if ok3 or _is_partial_register_error(content3):
                    if st is not None:
                        st.mark_registered_confirmed(shrunk, label=reason, source="count_error_retry_shrunk")
                    return True
            if st is not None:
                st.note_register_failed(label=reason, reason="register_count_error_retry_failed")
            _restore_last_good_batch(f"{reason}/count_error", exchange=exchange)
            return False

        if _is_api_key_mismatch(content):
            logger.error("[SUB MANAGER] register failed by API key mismatch; check startup token bootstrap/settings.ini content=%r", content)
        if st is not None:
            st.note_register_failed(label=reason, reason=str(content)[:120])
        # unregister は通ったのに register が落ちた = 登録 0 件の状態。
        # 直前の正常バッチへ戻して PUSH を止めない。
        _restore_last_good_batch(reason, exchange=exchange)
        return False


# ats/ats_register_payload.py からも同じプロセス間ロックを取れるよう公開名を付ける。
# (Kabu Station の register リストはセッション全体で 50 銘柄上限の共有資源なので、
#  main.py 側の ATS 登録と push_receiver 側の rotation は同じロックで直列化する必要がある)
registration_sequence_lock = _registration_sequence_lock

__all__ = [
    "registration_sequence_lock",
    "REGISTER_CHUNK_SIZE",
    "REGISTER_LIMIT",
    "REGISTER_HEADROOM",
    "DEFAULT_EXCHANGE",
    "DEFAULT_UNREGISTER_TO_REGISTER_WAIT_SEC",
    "make_symbol_objects",
    "run_unregister_all",
    "run_unregister_symbols",
    "run_register_chunks",
    "run_refresh_sequence",
]


# ============================================================
# 旧 core/startup/push_registration_recovery_patch._patch_register_ops を本文化。
# 中身は監査ログのみで、戻り値は元関数のまま。
#
# 旧パッチは ro._http_json_request には触れない方針だった
# (トークン処理は register_ops 側の正規実装が持つ)。その方針も変えていない。
# _PUSH_REGISTER_RECOVERY_HTTP_DISABLED_V4 などのフラグは読み手が無いので移していない。
#
# 適用スコープは旧 _safe_install の allowed 集合
# {main, main_database, push_receiver} を _prr_scope_enabled() で再現。
# ============================================================

_PRR_CONTEXTS = {"main", "main_database", "push_receiver"}


def _prr_argv_context() -> str:
    """core/startup/runtime_env/installer.py の _argv_context() と同一。"""
    try:
        text = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in text:
            return "main_database"
        if text.endswith("main.py") or "/main.py" in text:
            return "main"
        if "db_prepare_runner.py" in text:
            return "db_prepare"
        if "push_receiver_runner.py" in text:
            return "push_receiver"
        if "summary_database_runner.py" in text:
            return "summary_database"
        if "ranking_collector_runner.py" in text:
            return "ranking_collector"
        if "yahoo_complement_runner.py" in text:
            return "yahoo_complement"
        if "data_collectors_runner.py" in text:
            return "main_database"
        if text:
            return "helper"
    except Exception:
        pass
    return "unknown"


def _prr_scope_enabled() -> bool:
    if str(os.environ.get("DISABLE_PUSH_REGISTER_RECOVERY_PATCH", "")).strip() == "1":
        return False
    return _prr_argv_context() in _PRR_CONTEXTS


def _prr_env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _prr_dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items or []:
        s = str(x or "").strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _prr_split_ab_counts(symbols: Sequence[str]) -> tuple[int, int, list[str], list[str]]:
    chunk = max(1, _prr_env_int("PUSH_REGISTER_CHUNK_SIZE", 50))
    a = list(symbols or [])[:chunk]
    b = list(symbols or [])[chunk:chunk * 2]
    return len(a), len(b), a[:10], b[:10]


def run_register_chunks(symbols, *args, **kwargs) -> bool:
    if not _prr_scope_enabled():
        return _run_register_chunks_core(symbols, *args, **kwargs)
    target = _prr_dedupe([str(x) for x in list(symbols or [])])
    a_count, b_count, a_head, b_head = _prr_split_ab_counts(target)
    t0 = time.monotonic()
    logger.warning(
        "[PUSH REGISTER RECOVERY][REGISTER AUDIT] register_chunks start target=%s A=%s B=%s head=%s A_head=%s B_head=%s",
        len(target), a_count, b_count, target[:10], a_head, b_head,
    )
    ok = _run_register_chunks_core(symbols, *args, **kwargs)
    logger.warning(
        "[PUSH REGISTER RECOVERY][REGISTER AUDIT] register_chunks done ok=%s elapsed=%.3fs target=%s A=%s B=%s",
        ok, time.monotonic() - t0, len(target), a_count, b_count,
    )
    return ok


def run_refresh_sequence(current_symbols, target_symbols, *args, **kwargs) -> bool:
    if not _prr_scope_enabled():
        return _run_refresh_sequence_core(current_symbols, target_symbols, *args, **kwargs)
    target = _prr_dedupe([str(x) for x in list(target_symbols or [])])
    a_count, b_count, a_head, b_head = _prr_split_ab_counts(target)
    t0 = time.monotonic()
    logger.warning(
        "[PUSH REGISTER RECOVERY][REGISTER AUDIT] refresh start current=%s target=%s A=%s B=%s clear_first=%s unregister_first=%s target_head=%s A_head=%s B_head=%s",
        len(current_symbols or []), len(target), a_count, b_count,
        kwargs.get("clear_first"), kwargs.get("unregister_first"), target[:10], a_head, b_head,
    )
    ok = _run_refresh_sequence_core(current_symbols, target_symbols, *args, **kwargs)
    logger.warning(
        "[PUSH REGISTER RECOVERY][REGISTER AUDIT] refresh done ok=%s elapsed=%.3fs target=%s A=%s B=%s",
        ok, time.monotonic() - t0, len(target), a_count, b_count,
    )
    return ok
