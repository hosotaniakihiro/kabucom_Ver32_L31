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
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Iterator, Optional, Sequence

from .globals_access import safe_get_global_data, safe_getattr
from .symbols import dedupe_keep_order, normalize_symbol
from .transport import is_transport_error, mark_transport_broken

logger = logging.getLogger(__name__)

REGISTER_CHUNK_SIZE = 50
REGISTER_LIMIT = 50
DEFAULT_EXCHANGE = 1

DEFAULT_UNREGISTER_TO_REGISTER_WAIT_SEC = float(os.environ.get("KABU_REGISTER_UNREGISTER_WAIT_SEC", "0.5"))
REGISTER_COUNT_ERROR_RETRY_WAIT_SEC = float(os.environ.get("KABU_REGISTER_COUNT_ERROR_RETRY_WAIT_SEC", "1.0"))

DEFAULT_BASE_URL = "http://localhost:18080/kabusapi"
DEFAULT_REGISTER_URL = f"{DEFAULT_BASE_URL}/register"
DEFAULT_UNREGISTER_ALL_URL = f"{DEFAULT_BASE_URL}/unregister/all"
DEFAULT_UNREGISTER_URL = f"{DEFAULT_BASE_URL}/unregister"

_SEQUENCE_LOCK = threading.RLock()
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


def _resolve_api_key() -> str:
    for getter in (_get_global_api_key, _get_env_api_key, _get_settings_api_key, _get_ini_api_key):
        api_key = getter()
        if api_key:
            return api_key
    logger.error("[SUB MANAGER] API key unavailable")
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


def _build_request(*, url: str, method: str, payload: Optional[dict], api_key: str) -> urllib.request.Request:
    method = (_safe_str(method) or "PUT").upper()
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, body, method=method)
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("X-API-KEY", api_key)
    return req


def _http_json_request(*, url: str, method: str, payload: Optional[dict], api_key: str, timeout: float = 10.0) -> tuple[bool, Any]:
    url = _normalize_http_url(url)
    method = (_safe_str(method) or "PUT").upper()
    if not url:
        logger.warning("[SUB MANAGER] HTTP %s skipped: invalid url=%r", method, url)
        return False, "invalid_url"
    try:
        req = _build_request(url=url, method=method, payload=payload, api_key=api_key)
    except Exception as e:
        logger.exception("[SUB MANAGER] HTTP %s request build failed url=%r", method, url)
        if is_transport_error(e):
            mark_transport_broken(reason=f"http_{method.lower()}_build", exc=e)
        return False, str(e)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            try:
                content = json.loads(raw.decode("utf-8"))
            except Exception:
                content = raw.decode("utf-8", errors="ignore")
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
    return ok, content


def run_register_chunks(symbols: Sequence[str], *, exchange: int = DEFAULT_EXCHANGE, chunk_size: int = REGISTER_CHUNK_SIZE) -> bool:
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


def run_refresh_sequence(
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
    with _registration_sequence_lock(reason):
        logger.info(
            "[SUB MANAGER] refresh sequence start current=%d target=%d clear_first=%s unregister_first=%s wait_after_clear=%.3fs locked=True",
            len(current_symbols or []),
            len(normalized_target),
            bool(clear_first),
            bool(unregister_first),
            wait_sec,
        )

        if clear_first:
            ok = run_unregister_all()
            if not ok:
                logger.error("[SUB MANAGER] clear_first failed -> abort register abort_if_clear_failed=%s", _abort_on_clear_failure())
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

        ok, content = _register_once_with_content(normalized_target, exchange=exchange)
        if ok:
            return True

        if _is_partial_register_error(content):
            logger.warning(
                "[SUB MANAGER] register partial accepted as usable -> hold rotation size=%d content=%r",
                len(normalized_target),
                content,
            )
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
                return True
            if _is_partial_register_error(content2):
                logger.warning(
                    "[SUB MANAGER] register retry partial accepted as usable -> hold rotation size=%d content=%r",
                    len(normalized_target),
                    content2,
                )
                return True
            return False

        if _is_api_key_mismatch(content):
            logger.error("[SUB MANAGER] register failed by API key mismatch; check startup token bootstrap/settings.ini content=%r", content)
        return False


__all__ = [
    "REGISTER_CHUNK_SIZE",
    "REGISTER_LIMIT",
    "DEFAULT_EXCHANGE",
    "DEFAULT_UNREGISTER_TO_REGISTER_WAIT_SEC",
    "make_symbol_objects",
    "run_unregister_all",
    "run_unregister_symbols",
    "run_register_chunks",
    "run_refresh_sequence",
]
