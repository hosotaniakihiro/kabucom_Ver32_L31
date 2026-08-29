# ============================================================
# token_manager.py（Ver34-STARTUP-STALE-TOKEN-RECOVERY）
# ------------------------------------------------------------
# ・API認証設定は settings.ini に集約する
# ・token 保存も settings.ini または config/settings.ini にだけ行う
# ・/token 呼び出しは原則 main.py / main_database.py の親プロセスだけ許可する
# ・push_receiver / ranking_collector / summary_database / yahoo_complement / db_prepare 等の
#   子プロセスでは /token を呼ばず、settings.ini の token を読むだけにする
# ・親プロセス同士でも短時間に /token を再発行しない
# ・別親プロセスが後から /token を呼んで既存子プロセスの token を失効させる事故を防ぐ
#
# Ver32:
# ・親が /token で取得した token を settings.ini に保存した後、必ず読み直して同一確認する。
# ・保存失敗や保存後不一致を debug で握りつぶさず、例外として起動を止める。
# ・settings.ini の探索に AUTOSTOCK_SETTINGS_INI / AUTOSTOCK_SETTINGS_INI_PATH も追加する。
# ・token_tail をログに出し、親取得 token と settings.ini token のズレを確認できるようにする。
#
# Ver33:
# ・Windowsで settings.ini への os.replace が WinError 5 になる環境向けに、
#   同一ファイルへの直接上書き fallback を追加する。
# ・atomic replace が失敗しても、直接書き込みで保存検証まで通れば起動を継続する。
#
# Ver34 (STARTUP-STALE-TOKEN-RECOVERY):
# ・/token 失敗を型で区別する:
#     TokenApiPasswordMismatch = /token 自体が 401 / Code=4001009 (apipassword 不一致)
#     TokenApiTransportError   = kabu Station へ到達できない
#     TokenApiError            = それ以外
#   これで「cached token が古い」と「apipassword が違う」を混同しない。
# ・request_fresh_token(): TTL / startup-once / parent-only の抑止層を明示的に
#   バイパスして /token を 1 回だけ叩き、settings.ini へ保存検証してから
#   token_manager / env / global_data / headers へ publish する。
#   呼ぶのは kabu_api/token_bootstrap.py の起動時ステートマシンだけ。
# ・TTL は「ファイルが新しい」ことしか証明しない。サーバ側で有効かどうかは
#   read-only preflight でしか分からないので、起動時の有効判定は
#   kabu_api/token_bootstrap.bootstrap_startup_token() が持つ。
# ・TOKEN_SOURCE / TOKEN_ACTION をログに出す。token 値と apipassword 値は出さない
#   (token_len / token_tail / api_password_source / password_present / password_len のみ)。
# ・token_api_call_count() で「本当に /token を叩いたか」を機械的に確認できる。
# ============================================================

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

import requests
from configparser import ConfigParser
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"
CONFIG_PATH = "settings.ini"
API_TOKEN = None
API_PASSWORD = None
_CONFIG_FILE_PATH: str | None = None
_REQUESTS_PATCHED = False
_REQUESTS_PATCH_ORIGINAL = None

# Ver34 observability: what produced the token currently held, and how many times
# POST /token was actually issued by this process.  The old logs said
# "parent startup bootstrap token refreshed/saved" even when nothing was fetched,
# so the counter is the ground truth callers should log.
LAST_TOKEN_SOURCE: str = ""
LAST_TOKEN_ACTION: str = ""
_TOKEN_API_CALL_COUNT = 0

# TOKEN_SOURCE / TOKEN_ACTION vocabulary (shared with kabu_api.token_bootstrap).
TOKEN_SOURCE_SETTINGS_INI_CACHED = "settings_ini_cached"
TOKEN_SOURCE_FRESH_TOKEN_API = "fresh_token_api"
TOKEN_SOURCE_STARTUP_401_RECOVERY = "startup_401_recovery"
TOKEN_SOURCE_INHERITED_PARENT = "inherited_parent"

TOKEN_ACTION_REUSED = "reused"
TOKEN_ACTION_API_REQUESTED = "api_requested"
TOKEN_ACTION_API_RECEIVED = "api_received"
TOKEN_ACTION_SAVED = "saved"
TOKEN_ACTION_PREFLIGHT_OK = "preflight_ok"
TOKEN_ACTION_PREFLIGHT_NG = "preflight_ng"

_APIPASSWORD_ENV_NAMES = (
    "KABUSAPI_APIPASSWORD",
    "AUKABU_APIPASSWORD",
    "KABU_API_PASSWORD",
    "KABUSAPI_PASSWORD",
)


class TokenApiError(RuntimeError):
    """POST /token failed for a reason that is not an API password mismatch."""

    kind = "token_api_error"

    def __init__(self, message: str, status: int | None = None, code: str = "", body: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body


class TokenApiPasswordMismatch(TokenApiError):
    """POST /token itself was rejected (401/403 or Code=4001009).

    This is case B: the apipassword does not match kabu Station.  It must never be
    reported as a stale cached token (case A).
    """

    kind = "api_password_mismatch"


class TokenApiTransportError(TokenApiError):
    """POST /token never reached kabu Station (not running, port closed, timeout)."""

    kind = "token_api_transport_error"


def token_api_call_count() -> int:
    """How many times this process actually issued POST /token."""
    return int(_TOKEN_API_CALL_COUNT)


def _mark_token_state(source: str, action: str) -> None:
    global LAST_TOKEN_SOURCE, LAST_TOKEN_ACTION
    LAST_TOKEN_SOURCE = str(source or "")
    LAST_TOKEN_ACTION = str(action or "")


_CHILD_MARKERS = (
    "push_receiver_runner.py",
    "ranking_collector_runner.py",
    "summary_database_runner.py",
    "yahoo_complement_runner.py",
    "db_prepare_runner.py",
)


def _token_tail(token: Any) -> str:
    s = str(token or "").strip()
    return s[-4:] if s else ""


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip().replace(",", ""))
    except Exception:
        return float(default)


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


def _project_root_candidates() -> list[Path]:
    out: list[Path] = []
    for env_name in ("KABU_PROJECT_ROOT", "PROJECT_ROOT", "APP_ROOT"):
        v = os.getenv(env_name)
        if v:
            out.append(Path(v))
    try:
        out.append(Path(__file__).resolve().parent)
    except Exception:
        pass
    out.append(Path.cwd())

    uniq: list[Path] = []
    seen: set[str] = set()
    for p in out:
        try:
            key = str(p.resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _config_candidates() -> list[Path]:
    out: list[Path] = []

    # token_startup_once_policy_patch.py と探索名を合わせる。
    for env_name in (
        "AUTOSTOCK_SETTINGS_INI",
        "AUTOSTOCK_SETTINGS_INI_PATH",
        "SETTINGS_INI",
        "SETTINGS_INI_PATH",
        "KABU_SETTINGS_INI",
    ):
        v = os.getenv(env_name)
        if v:
            out.append(Path(v))

    for root in _project_root_candidates():
        out.extend([root / "settings.ini", root / "config" / "settings.ini"])

    uniq: list[Path] = []
    seen: set[str] = set()
    for p in out:
        try:
            key = str(p.resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _get_section(conf: ConfigParser) -> str | None:
    # ConfigParser is case-insensitive by default, so [aukabu] / [AuKabu] are equivalent.
    if conf.has_section("aukabu"):
        return "aukabu"
    if conf.has_section("kabusapi"):
        return "kabusapi"
    return None


def _diagnostic(conf: ConfigParser) -> tuple[str, str]:
    try:
        existing = conf.get("__diagnostic__", "existing_files", fallback="")
        tried = conf.get("__diagnostic__", "tried", fallback="")
        return existing, tried
    except Exception:
        return "", ""


def _load_settings() -> ConfigParser:
    global _CONFIG_FILE_PATH
    last_existing: list[str] = []
    for path in _config_candidates():
        try:
            if not path.exists():
                continue
            conf = ConfigParser()
            conf.read(path, encoding="utf-8-sig")
            last_existing.append(str(path))
            sec = _get_section(conf)
            if sec:
                _CONFIG_FILE_PATH = str(path)
                return conf
        except Exception:
            continue

    conf = ConfigParser()
    _CONFIG_FILE_PATH = None
    tried = [str(p) for p in _config_candidates()]
    conf["__diagnostic__"] = {
        "existing_files": " | ".join(last_existing),
        "tried": " | ".join(tried),
    }
    return conf


def _require_section(conf: ConfigParser) -> str:
    sec = _get_section(conf)
    if sec:
        return sec
    existing, tried = _diagnostic(conf)
    raise ValueError(
        "[aukabu] or [kabusapi] がある settings.ini が見つかりません。"
        " API認証は settings.ini の [kabusapi] または [aukabu] に集約してください。"
        " 必要なら環境変数 SETTINGS_INI_PATH で settings.ini を明示指定できます。"
        f" existing={existing} tried={tried}"
    )


def resolve_apipassword_with_source(
    conf: ConfigParser | None = None, sec: str | None = None, apipassword=None
) -> tuple[str | None, str]:
    """Resolve the API password and report where it came from.

    The value itself is never returned to logs -- only the source label, presence
    and length are ever logged (see _log_apipassword_diagnostics).

    Order changed in Ver34: settings.ini now beats the in-process API_PASSWORD
    cache.  A stale runtime cache used to shadow a corrected settings.ini, which
    made "API password mismatch" impossible to fix without restarting everything.
    """
    global API_PASSWORD
    if apipassword:
        API_PASSWORD = str(apipassword)
        return API_PASSWORD, "argument"

    if conf is None:
        conf = _load_settings()
        sec = _get_section(conf)
    if sec:
        try:
            v = conf.get(sec, "apipassword", fallback=None)
            if v:
                API_PASSWORD = str(v)
                return API_PASSWORD, f"settings.ini[{sec}]"
        except Exception:
            pass

    for env_name in _APIPASSWORD_ENV_NAMES:
        v = os.getenv(env_name)
        if v:
            API_PASSWORD = str(v)
            return API_PASSWORD, f"env:{env_name}"

    if API_PASSWORD:
        return str(API_PASSWORD), "runtime_cache"
    return None, "none"


def _resolve_apipassword(conf: ConfigParser, sec: str | None, apipassword=None) -> str | None:
    password, _source = resolve_apipassword_with_source(conf, sec, apipassword)
    return password


def _log_apipassword_diagnostics(source: str, password: Any, reason: str) -> None:
    """Log where the API password came from without ever logging the value."""
    logger.warning(
        "[TOKEN MANAGER] api_password_source=%s password_present=%s password_len=%d reason=%s",
        source,
        bool(password),
        len(str(password or "")),
        reason,
    )


def read_settings_token() -> str:
    """Read the token straight out of settings.ini (no env, no runtime cache)."""
    conf = _load_settings()
    sec = _get_section(conf)
    if not sec:
        return ""
    try:
        return str(conf.get(sec, "token", fallback="") or "").strip()
    except Exception:
        return ""


def _settings_file_age_sec() -> float | None:
    try:
        path = _CONFIG_FILE_PATH or CONFIG_PATH
        if not path:
            return None
        return max(0.0, time.time() - os.path.getmtime(path))
    except Exception:
        return None


def _read_token_from_config_path(path: str | Path) -> str:
    try:
        conf = ConfigParser()
        conf.read(str(path), encoding="utf-8-sig")
        sec = _get_section(conf)
        if not sec:
            return ""
        return str(conf.get(sec, "token", fallback="") or "").strip()
    except Exception:
        return ""


def _direct_write_config(conf: ConfigParser, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        conf.write(f)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass


def _atomic_write_config(conf: ConfigParser, path: str | Path) -> None:
    """Write settings.ini.

    Preferred path is atomic temp-file + os.replace.  On some Windows setups
    os.replace can fail with WinError 5 even though direct write is allowed
    (for example, antivirus/editor/file-indexer interference).  In that case,
    fallback to direct overwrite and let _save_token verify the contents.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            conf.write(f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        try:
            os.replace(tmp_name, path)
            return
        except PermissionError as e:
            logger.warning(
                "[TOKEN MANAGER] atomic settings.ini replace denied; fallback direct write path=%s err=%s",
                path,
                e,
            )
            _direct_write_config(conf, path)
            return
    except Exception:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass
        raise
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass


def _save_token(token) -> bool:
    token_s = str(token or "").strip()
    if not token_s:
        raise ValueError("empty token cannot be saved")

    conf = _load_settings()
    sec = _get_section(conf)
    if not sec:
        existing, tried = _diagnostic(conf)
        raise ValueError(
            "settings.ini に [aukabu] または [kabusapi] がないため token を保存できません。"
            f" existing={existing} tried={tried}"
        )

    conf.set(sec, "token", token_s)
    path = _CONFIG_FILE_PATH or CONFIG_PATH
    _atomic_write_config(conf, path)

    saved = _read_token_from_config_path(path)
    if saved != token_s:
        raise RuntimeError(
            "settings.ini token save verification failed "
            f"path={path} expected_tail={_token_tail(token_s)} actual_tail={_token_tail(saved)} "
            f"expected_len={len(token_s)} actual_len={len(saved)}"
        )

    os.environ["AUTOSTOCK_SETTINGS_INI_ACTIVE"] = str(path)
    logger.warning(
        "[TOKEN MANAGER] settings.ini token saved/verified path=%s section=%s token_len=%d token_tail=%s",
        path,
        sec,
        len(token_s),
        _token_tail(token_s),
    )
    return True


def _publish_token(token: Any) -> str | None:
    global API_TOKEN
    if not token:
        return None
    token_s = str(token).strip()
    if not token_s:
        return None
    API_TOKEN = token_s

    for name in (
        "KABU_API_TOKEN",
        "KABUSAPI_TOKEN",
        "AUKABU_TOKEN",
        "API_TOKEN",
        "TOKEN",
        "KABU_API_KEY",
        "KABUSAPI_API_KEY",
        "X_API_KEY",
        "KABU_TOKEN",
    ):
        try:
            os.environ[name] = token_s
        except Exception:
            pass

    for module_name in ("global_data", "config.global_data", "core.global_data", "trading.global_data"):
        try:
            mod = __import__(module_name, fromlist=["*"])
            for attr in ("API_TOKEN", "TOKEN", "token", "api_token"):
                try:
                    setattr(mod, attr, token_s)
                except Exception:
                    pass
        except Exception:
            pass
    return token_s


def _parse_kabu_error(raw: str) -> tuple[str, str]:
    """Extract (Code, Message) from a kabu Station error body."""
    try:
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict):
            return str(data.get("Code") or ""), str(data.get("Message") or "")
    except Exception:
        pass
    return "", str(raw or "")


def _post_token_api(api_password: str, timeout: float = 5.0) -> str:
    """POST /token exactly once and classify every failure mode.

    Raises:
        TokenApiPasswordMismatch -- /token itself rejected the password (case B)
        TokenApiTransportError   -- kabu Station unreachable
        TokenApiError            -- any other /token failure
    """
    global _TOKEN_API_CALL_COUNT

    url = f"{API_URL}/token"
    headers = {"Content-Type": "application/json"}
    data = json.dumps({"APIPassword": str(api_password)}).encode()
    # urllib.request は毎回 "Connection: close" を送る。kabu Station は
    # このヘッダーが付いた POST /token に応答を返さず、5 秒でタイムアウト
    # させる (2026-08-29 実測。誤ったパスワードでも keep-alive なら
    # 0.01 秒で 4001013 が返る)。requests は keep-alive で送るので通る。
    _TOKEN_API_CALL_COUNT += 1
    try:
        res = requests.post(url, data=data, headers=headers, timeout=timeout)
    except Exception as e:
        raise TokenApiTransportError(
            f"POST /token could not reach kabu Station url={url} error={e}"
        ) from e

    status = int(res.status_code)
    raw = res.text or ""

    if status >= 400:
        code, message = _parse_kabu_error(raw)
        if status in (401, 403) or code == "4001009" or "APIキー不一致" in message:
            raise TokenApiPasswordMismatch(
                f"POST /token rejected the APIPassword status={status} code={code} message={message}",
                status=status,
                code=code,
                body=raw,
            )
        raise TokenApiError(
            f"POST /token failed status={status} code={code} message={message}",
            status=status,
            code=code,
            body=raw,
        )

    try:
        result = json.loads(raw) if raw else {}
    except Exception as e:
        raise TokenApiError(f"POST /token returned a non-JSON body len={len(raw)}") from e

    token = str((result or {}).get("Token") or "").strip()
    if token:
        return token

    code, message = _parse_kabu_error(raw)
    if code == "4001009" or "APIキー不一致" in message:
        raise TokenApiPasswordMismatch(
            f"POST /token returned no token code={code} message={message}", code=code, body=raw
        )
    raise TokenApiError(
        f"POST /token returned no token code={code} message={message}", code=code, body=raw
    )


def _publish_token_all_layers(token: str, name: str = "request_fresh_token") -> str:
    """Publish one token to every holder: token_manager, env, global_data, headers.

    Mirrors what refresh_token()'s wrapper layers do, so a token obtained outside
    the layered refresh_token() path is not left half-published.
    """
    _publish_token(token)
    _ps = _pipeline_stability()
    if _ps is not None:
        try:
            _ps.publish_token_result(name, token)
        except Exception:
            logger.debug("[TOKEN MANAGER] pipeline_stability publish failed", exc_info=True)
    _ecu = _entry_count_unblock()
    if _ecu is not None:
        try:
            _ecu.refresh_token_publish(token)
        except Exception:
            logger.debug("[TOKEN MANAGER] entry_count_unblock publish failed", exc_info=True)
    return token


def request_fresh_token(apipassword=None, reason: str = "explicit") -> str:
    """Issue POST /token once, save to settings.ini, publish everywhere.

    Deliberately bypasses the TTL / startup-once / parent-only suppression layers:
    the caller (the startup state machine in kabu_api.token_bootstrap) has already
    proved with a read-only preflight that the cached token is dead, so those
    guards would only keep the process unrecoverable.  This is the ONLY sanctioned
    way to obtain a token outside the normal startup refresh, and the startup state
    machine calls it at most once per process.
    """
    global API_PASSWORD

    conf = _load_settings()
    sec = _get_section(conf)
    if not sec:
        existing, tried = _diagnostic(conf)
        raise TokenApiError(
            "settings.ini に [aukabu] または [kabusapi] がありません。"
            f" path={_CONFIG_FILE_PATH} existing={existing} tried={tried}"
        )

    api_password, pw_source = resolve_apipassword_with_source(conf, sec, apipassword)
    _log_apipassword_diagnostics(pw_source, api_password, reason)
    if not api_password:
        raise TokenApiPasswordMismatch(
            "settings.ini に apipassword がありません。"
            f" path={_CONFIG_FILE_PATH} section={sec} api_password_source={pw_source}"
        )

    _mark_token_state(TOKEN_SOURCE_FRESH_TOKEN_API, TOKEN_ACTION_API_REQUESTED)
    logger.warning(
        "[TOKEN MANAGER] TOKEN_SOURCE=%s TOKEN_ACTION=%s reason=%s api_password_source=%s",
        TOKEN_SOURCE_FRESH_TOKEN_API,
        TOKEN_ACTION_API_REQUESTED,
        reason,
        pw_source,
    )

    token = _post_token_api(api_password)

    _mark_token_state(TOKEN_SOURCE_FRESH_TOKEN_API, TOKEN_ACTION_API_RECEIVED)
    logger.warning(
        "[TOKEN MANAGER] TOKEN_SOURCE=%s TOKEN_ACTION=%s token_len=%d token_tail=%s",
        TOKEN_SOURCE_FRESH_TOKEN_API,
        TOKEN_ACTION_API_RECEIVED,
        len(token),
        _token_tail(token),
    )

    # settings.ini is canonical: verify the save before publishing, otherwise the
    # requests hook can re-inject the old settings.ini token and 401 again.
    _save_token(token)
    _mark_token_state(TOKEN_SOURCE_FRESH_TOKEN_API, TOKEN_ACTION_SAVED)
    logger.warning(
        "[TOKEN MANAGER] TOKEN_SOURCE=%s TOKEN_ACTION=%s path=%s token_len=%d token_tail=%s",
        TOKEN_SOURCE_FRESH_TOKEN_API,
        TOKEN_ACTION_SAVED,
        _CONFIG_FILE_PATH,
        len(token),
        _token_tail(token),
    )

    _publish_token_all_layers(token)
    API_PASSWORD = str(api_password)
    return token


def _get_valid_token_core():
    global API_TOKEN
    if API_TOKEN:
        return API_TOKEN
    conf = _load_settings()
    sec = _require_section(conf)
    token = conf.get(sec, "token", fallback=None)
    return _publish_token(token)


def _refresh_token_core(apipassword=None):
    """Refresh token only when it is safe to issue a new kabu Station token.

    Child runners must not call /token. Parent processes also avoid issuing a new
    token when settings.ini was updated recently, because a second parent started a
    few minutes later can invalidate the token used by already-running children.
    """
    global API_TOKEN, API_PASSWORD

    if _is_child_process() and not _env_bool("KABU_TOKEN_ALLOW_CHILD_REFRESH", False):
        token = get_valid_token()
        _mark_token_state(TOKEN_SOURCE_SETTINGS_INI_CACHED, TOKEN_ACTION_REUSED)
        logger.warning(
            "[TOKEN MANAGER] TOKEN_SOURCE=%s TOKEN_ACTION=%s child refresh suppressed token_len=%d token_tail=%s argv=%s",
            TOKEN_SOURCE_SETTINGS_INI_CACHED,
            TOKEN_ACTION_REUSED,
            len(str(token or "")),
            _token_tail(token),
            _argv_text(),
        )
        return token

    if (not _is_parent_process()) and _env_bool("KABU_TOKEN_PARENT_ONLY_REFRESH", True) and not _env_bool("KABU_TOKEN_ALLOW_NONPARENT_REFRESH", False):
        token = get_valid_token()
        _mark_token_state(TOKEN_SOURCE_SETTINGS_INI_CACHED, TOKEN_ACTION_REUSED)
        logger.warning(
            "[TOKEN MANAGER] TOKEN_SOURCE=%s TOKEN_ACTION=%s non-parent refresh suppressed token_len=%d token_tail=%s argv=%s",
            TOKEN_SOURCE_SETTINGS_INI_CACHED,
            TOKEN_ACTION_REUSED,
            len(str(token or "")),
            _token_tail(token),
            _argv_text(),
        )
        return token

    conf = _load_settings()
    sec = _get_section(conf)
    if not sec:
        existing, tried = _diagnostic(conf)
        raise ValueError(
            "settings.ini に [aukabu] または [kabusapi] がありません。"
            f" path={_CONFIG_FILE_PATH} existing={existing} tried={tried}"
        )

    existing_token = str(conf.get(sec, "token", fallback="") or "").strip()
    ttl_sec = max(0.0, _env_float("KABU_TOKEN_PARENT_REFRESH_TTL_SEC", 3600.0))
    age_sec = _settings_file_age_sec()
    force_parent_refresh = _env_bool("KABU_TOKEN_FORCE_PARENT_REFRESH", False) or _env_bool("KABU_TOKEN_FORCE_REFRESH", False)

    if existing_token and not force_parent_refresh and ttl_sec > 0 and age_sec is not None and age_sec <= ttl_sec:
        token = _publish_token(existing_token)
        _mark_token_state(TOKEN_SOURCE_SETTINGS_INI_CACHED, TOKEN_ACTION_REUSED)
        # NOTE: TTL only says the local file is young.  It cannot say whether kabu
        # Station still accepts this token -- only a preflight can.  The startup
        # state machine in kabu_api.token_bootstrap owns that decision now.
        logger.warning(
            "[TOKEN MANAGER] TOKEN_SOURCE=%s TOKEN_ACTION=%s parent refresh skipped by ttl "
            "(ttl proves file age only, not server-side validity) token_len=%d token_tail=%s age=%.1fs ttl=%.1fs argv=%s",
            TOKEN_SOURCE_SETTINGS_INI_CACHED,
            TOKEN_ACTION_REUSED,
            len(str(token or "")),
            _token_tail(token),
            age_sec,
            ttl_sec,
            _argv_text(),
        )
        return token

    api_password, pw_source = resolve_apipassword_with_source(conf, sec, apipassword)
    _log_apipassword_diagnostics(pw_source, api_password, "parent_refresh")

    if not api_password:
        existing, tried = _diagnostic(conf)
        raise ValueError(
            "settings.ini に apipassword がありません。"
            f" path={_CONFIG_FILE_PATH} section={sec} api_password_source={pw_source}"
            f" existing={existing} tried={tried}"
        )

    _mark_token_state(TOKEN_SOURCE_FRESH_TOKEN_API, TOKEN_ACTION_API_REQUESTED)
    logger.warning(
        "[TOKEN MANAGER] TOKEN_SOURCE=%s TOKEN_ACTION=%s reason=parent_refresh api_password_source=%s",
        TOKEN_SOURCE_FRESH_TOKEN_API,
        TOKEN_ACTION_API_REQUESTED,
        pw_source,
    )

    token = _post_token_api(api_password)

    _mark_token_state(TOKEN_SOURCE_FRESH_TOKEN_API, TOKEN_ACTION_API_RECEIVED)

    # 重要: settings.ini を canonical source にしているため、publish より先に保存検証する。
    # 保存に失敗した状態で API_TOKEN だけ更新すると、直後の requests フックが古い settings.ini token を再注入して 401 になる。
    _save_token(token)
    _publish_token(token)
    API_PASSWORD = str(api_password)
    _mark_token_state(TOKEN_SOURCE_FRESH_TOKEN_API, TOKEN_ACTION_SAVED)

    logger.warning(
        "[TOKEN MANAGER] TOKEN_SOURCE=%s TOKEN_ACTION=%s parent token refreshed/saved "
        "token_len=%d token_tail=%s settings=%s argv=%s",
        TOKEN_SOURCE_FRESH_TOKEN_API,
        TOKEN_ACTION_SAVED,
        len(str(token or "")),
        _token_tail(token),
        os.environ.get("AUTOSTOCK_SETTINGS_INI_ACTIVE", _CONFIG_FILE_PATH or ""),
        _argv_text(),
    )
    return token


def _is_kabu_api_url(url: Any) -> bool:
    try:
        s = str(url or "").lower()
        return "localhost:18080/kabusapi" in s or "127.0.0.1:18080/kabusapi" in s
    except Exception:
        return False


def _is_token_url(url: Any) -> bool:
    try:
        return str(url or "").lower().rstrip("/").endswith("/token")
    except Exception:
        return False


def _merge_api_key(headers: Any, token: str | None) -> dict:
    out = {}
    try:
        if headers:
            out.update(dict(headers))
    except Exception:
        pass
    if token:
        out["X-API-KEY"] = str(token)
    return out


def install_requests_401_retry_patch() -> bool:
    """Patch requests with token injection, but no automatic child refresh."""
    global _REQUESTS_PATCHED, _REQUESTS_PATCH_ORIGINAL
    if _REQUESTS_PATCHED:
        return True
    try:
        import requests  # type: ignore
    except Exception:
        return False

    try:
        original = requests.sessions.Session.request
        if getattr(original, "_kabu_token_401_retry_patch_v2_parent_only", False):
            _REQUESTS_PATCHED = True
            return True

        def _patched_request(self, method, url, **kwargs):
            is_kabu = _is_kabu_api_url(url) and not _is_token_url(url)
            token = None
            if is_kabu:
                try:
                    token = get_valid_token()
                    kwargs["headers"] = _merge_api_key(kwargs.get("headers"), token)
                except Exception:
                    pass

            resp = original(self, method, url, **kwargs)
            if not is_kabu or getattr(resp, "status_code", None) != 401:
                return resp

            if _is_child_process() or not _env_bool("KABU_TOKEN_ALLOW_RUNTIME_REFRESH", False):
                logger.warning(
                    "[KABU API REQUESTS 401 RETRY] 401 refresh suppressed url=%s child=%s token_len=%d token_tail=%s",
                    url,
                    _is_child_process(),
                    len(str(token or "")),
                    _token_tail(token),
                )
                return resp

            try:
                new_token = refresh_token()
                retry_kwargs = dict(kwargs)
                retry_kwargs["headers"] = _merge_api_key(retry_kwargs.get("headers"), new_token)
                retry_kwargs.setdefault("timeout", kwargs.get("timeout", None))
                retry_resp = original(self, method, url, **retry_kwargs)
                logger.warning(
                    "[KABU API REQUESTS 401 RETRY] url=%s status_before=401 status_after=%s token_len=%s token_tail=%s",
                    str(url),
                    getattr(retry_resp, "status_code", None),
                    len(str(new_token or "")),
                    _token_tail(new_token),
                )
                return retry_resp
            except Exception:
                logger.exception("[KABU API REQUESTS 401 RETRY] refresh/retry failed url=%s", url)
                return resp

        _patched_request._kabu_token_401_retry_patch_v2_parent_only = True  # type: ignore[attr-defined]
        _patched_request._original = original  # type: ignore[attr-defined]
        requests.sessions.Session.request = _patched_request
        _REQUESTS_PATCH_ORIGINAL = original
        _REQUESTS_PATCHED = True
        logger.warning("[KABU API REQUESTS 401 RETRY] installed parent_only=True")
        return True
    except Exception:
        logger.exception("[KABU API REQUESTS 401 RETRY] install failed")
        return False


try:
    install_requests_401_retry_patch()
except Exception:
    pass


# ============================================================
# TOKEN STARTUP ONCE POLICY
# (旧 core/startup/token_startup_once_policy_patch.py を本文化)
# ------------------------------------------------------------
# 起動時に 1 回だけ取得したトークンを使い、実行中の再取得は原則抑止する。
# 旧 install() は refresh_token をこの層で**置き換えて**いた
# (KABU_TOKEN_ALLOW_RUNTIME_REFRESH / KABU_TOKEN_FORCE_REFRESH が真の
#  ときだけ素の実装を呼ぶ)。素の実装は _refresh_token_core として残す。
#
# この層は最内周。旧構成では実 install 順で
#   _refresh_token_core
#     <- この層 (startup once)
#       <- _refresh_token_publish        (full_pipeline_stability)
#         <- refresh_token               (entry_count_unblock)
# となっており、外側 2 つも本文化済み (下の 2 節)。
#
# ポリシーの有効化 (env 既定値・起動時ブートストラップ・requests の
# 401 再取得抑止) は kabu_api/token_startup_policy.py の
# enable_token_startup_policy() が行う。
# ============================================================


def _refresh_token_startup_once(apipassword=None, *args, **kwargs):
    try:
        from kabu_api import token_startup_policy as _tsp
    except Exception:
        return _refresh_token_core(apipassword)
    if not _tsp.startup_policy_enabled():
        return _refresh_token_core(apipassword)
    return _tsp.refresh_token_startup_once(_refresh_token_core, apipassword, *args, **kwargs)


# ------------------------------------------------------------
# FULL PIPELINE STABILITY の token publish 層
# (旧 core/startup/full_pipeline_stability_runtime_patch._install_token_unifier)
# ------------------------------------------------------------
# 取得結果を legacy モジュール群 (global_data / register_ops など) へ
# 即座に配る。旧構成では startup once の 1 枚外側に入っていた。
# ------------------------------------------------------------


def _pipeline_stability():
    try:
        from kabu_api import pipeline_stability as _ps

        return _ps if _ps.pipeline_stability_enabled() else None
    except Exception:
        return None


def _refresh_token_publish(apipassword=None, *args, **kwargs):
    ret = _refresh_token_startup_once(apipassword, *args, **kwargs)
    _ps = _pipeline_stability()
    if _ps is None:
        return ret
    return _ps.publish_token_result("refresh_token", ret)


def _get_valid_token_publish(*args, **kwargs):
    ret = _get_valid_token_core(*args, **kwargs)
    _ps = _pipeline_stability()
    if _ps is None:
        return ret
    return _ps.publish_token_result("get_valid_token", ret)


# ------------------------------------------------------------
# ENTRY COUNT UNBLOCK の token 層 (最外周)
# (旧 core/startup/entry_count_unblock_runtime_patch._patch_token_manager)
# ------------------------------------------------------------
# refresh_token: 取れたトークンを global_data 等へ配る
# get_valid_token: env に十分な長さのトークンがあればそれを優先する
#
# 旧構成の実 install 順は runtime_env/installer の
#   _install_full_pipeline_stability -> _install_entry_count_unblock
# だったので、この層が publish 層のさらに外側になる。
# ------------------------------------------------------------


def _entry_count_unblock():
    try:
        from trading.entry import entry_count_unblock as _ecu

        return _ecu if _ecu.entry_count_unblock_enabled() else None
    except Exception:
        return None


def refresh_token(apipassword=None, *args, **kwargs):
    ret = _refresh_token_publish(apipassword, *args, **kwargs)
    _ecu = _entry_count_unblock()
    if _ecu is None:
        return ret
    return _ecu.refresh_token_publish(ret)


def get_valid_token(*args, **kwargs):
    _ecu = _entry_count_unblock()
    if _ecu is None:
        return _get_valid_token_publish(*args, **kwargs)
    return _ecu.get_valid_token_env_first(_get_valid_token_publish, *args, **kwargs)
