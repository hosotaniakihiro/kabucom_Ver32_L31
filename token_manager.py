# ============================================================
# token_manager.py（Ver32-STARTUP-TOKEN-SAVE-VERIFY）
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
# ============================================================

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
import urllib.request
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


def _resolve_apipassword(conf: ConfigParser, sec: str | None, apipassword=None) -> str | None:
    global API_PASSWORD
    if apipassword:
        API_PASSWORD = str(apipassword)
        return API_PASSWORD
    if API_PASSWORD:
        return str(API_PASSWORD)
    if sec:
        try:
            v = conf.get(sec, "apipassword", fallback=None)
            if v:
                API_PASSWORD = str(v)
                return API_PASSWORD
        except Exception:
            pass
    for env_name in ("KABUSAPI_APIPASSWORD", "AUKABU_APIPASSWORD", "KABU_API_PASSWORD", "KABUSAPI_PASSWORD"):
        v = os.getenv(env_name)
        if v:
            API_PASSWORD = str(v)
            return API_PASSWORD
    return None


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


def _atomic_write_config(conf: ConfigParser, path: str | Path) -> None:
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
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass
        raise


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


def get_valid_token():
    global API_TOKEN
    if API_TOKEN:
        return API_TOKEN
    conf = _load_settings()
    sec = _require_section(conf)
    token = conf.get(sec, "token", fallback=None)
    return _publish_token(token)


def refresh_token(apipassword=None):
    """Refresh token only when it is safe to issue a new kabu Station token.

    Child runners must not call /token. Parent processes also avoid issuing a new
    token when settings.ini was updated recently, because a second parent started a
    few minutes later can invalidate the token used by already-running children.
    """
    global API_TOKEN, API_PASSWORD

    if _is_child_process() and not _env_bool("KABU_TOKEN_ALLOW_CHILD_REFRESH", False):
        token = get_valid_token()
        logger.warning(
            "[TOKEN MANAGER] child refresh suppressed; using settings.ini token token_len=%d token_tail=%s argv=%s",
            len(str(token or "")),
            _token_tail(token),
            _argv_text(),
        )
        return token

    if (not _is_parent_process()) and _env_bool("KABU_TOKEN_PARENT_ONLY_REFRESH", True) and not _env_bool("KABU_TOKEN_ALLOW_NONPARENT_REFRESH", False):
        token = get_valid_token()
        logger.warning(
            "[TOKEN MANAGER] non-parent refresh suppressed; using settings.ini token token_len=%d token_tail=%s argv=%s",
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
        logger.warning(
            "[TOKEN MANAGER] parent refresh skipped by ttl; using recent settings.ini token token_len=%d token_tail=%s age=%.1fs ttl=%.1fs argv=%s",
            len(str(token or "")),
            _token_tail(token),
            age_sec,
            ttl_sec,
            _argv_text(),
        )
        return token

    api_password = _resolve_apipassword(conf, sec, apipassword)

    if not api_password:
        existing, tried = _diagnostic(conf)
        raise ValueError(
            "settings.ini に apipassword がありません。"
            f" path={_CONFIG_FILE_PATH} section={sec} existing={existing} tried={tried}"
        )

    url = f"{API_URL}/token"
    headers = {"Content-Type": "application/json"}
    data = json.dumps({"APIPassword": api_password}).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    with urllib.request.urlopen(req, timeout=5) as res:
        raw = res.read().decode()
        result = json.loads(raw)

    token = str(result.get("Token") or "").strip()
    if not token:
        raise ValueError("token を取得できませんでした")

    # 重要: settings.ini を canonical source にしているため、publish より先に保存検証する。
    # 保存に失敗した状態で API_TOKEN だけ更新すると、直後の requests フックが古い settings.ini token を再注入して 401 になる。
    _save_token(token)
    _publish_token(token)
    API_PASSWORD = str(api_password)

    logger.warning(
        "[TOKEN MANAGER] parent token refreshed/saved token_len=%d token_tail=%s settings=%s argv=%s",
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
