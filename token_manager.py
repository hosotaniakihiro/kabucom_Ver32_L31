# ============================================================
# token_manager.py（Ver29-REQUESTS-401-RETRY）
# ------------------------------------------------------------
# ・main.py / startup.py から安全に import RefreshToken が可能
# ・循環importなし
# ・API認証設定は settings.ini に集約する
# ・settings.local.ini / setting.ini / kabusapi.ini / aukabu.ini は読まない
# ・[aukabu] / [kabusapi] は settings.ini または config/settings.ini からのみ読む
# ・startup_config から渡された apipassword をメモリキャッシュし、
#   force_cancel_loop などの 401 後 refresh_token() 引数なし呼び出しでも再利用する
# ・token 保存も settings.ini または config/settings.ini にだけ行う
# ・requests 経由の kabu API 401 は refresh_token 後に X-API-KEY を差し替えて1回だけ再試行する
# ============================================================

from __future__ import annotations

import json
import logging
import os
import urllib.request
from configparser import ConfigParser
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"
CONFIG_PATH = "settings.ini"  # API認証の唯一の標準ファイル名
API_TOKEN = None
API_PASSWORD = None
_CONFIG_FILE_PATH: str | None = None
_REQUESTS_PATCHED = False
_REQUESTS_PATCH_ORIGINAL = None


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
            key = str(p.resolve())
        except Exception:
            key = str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _config_candidates() -> list[Path]:
    """settings.ini だけを候補にする。

    以前は settings.local.ini / setting.ini / kabusapi.ini / aukabu.ini も候補にしていたが、
    複数ファイルに古い token が残ると APIキー不一致の原因になる。
    そのため、API認証は settings.ini に完全集約する。
    """
    out: list[Path] = []

    # 明示指定も settings.ini 系だけ許可する。
    for env_name in (
        "SETTINGS_INI_PATH",
        "KABU_SETTINGS_INI",
    ):
        v = os.getenv(env_name)
        if v:
            out.append(Path(v))

    for root in _project_root_candidates():
        out.extend([
            root / "settings.ini",
            root / "config" / "settings.ini",
        ])

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


def _get_section(conf: ConfigParser) -> str | None:
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


# ============================================================
# 設定ファイル読込
# ============================================================
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

    # APIセクションが無い場合でも、互換のため空ConfigParserを返す。
    # refresh_token(apipassword=...) はこの状態でも実行できる。
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
    """startup_config が渡した apipassword をキャッシュし、後続の refresh_token() でも再利用する。"""
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

    for env_name in (
        "KABUSAPI_APIPASSWORD",
        "AUKABU_APIPASSWORD",
        "KABU_API_PASSWORD",
        "KABUSAPI_PASSWORD",
    ):
        v = os.getenv(env_name)
        if v:
            API_PASSWORD = str(v)
            return API_PASSWORD

    return None


# ============================================================
# Token 保存 / 反映
# ============================================================
def _save_token(token) -> bool:
    """settings.ini にだけ token を保存する。"""
    conf = _load_settings()
    sec = _get_section(conf)
    if not sec:
        return False

    conf.set(sec, "token", token)
    path = _CONFIG_FILE_PATH or CONFIG_PATH
    with open(path, "w", encoding="utf-8") as f:
        conf.write(f)
    return True


def _publish_token(token: Any) -> str | None:
    """Refresh後のtokenを、既存runtimeが参照しやすい場所へ同期する。"""
    global API_TOKEN
    if not token:
        return None
    token_s = str(token)
    API_TOKEN = token_s

    for name in (
        "KABU_API_TOKEN",
        "KABUSAPI_TOKEN",
        "AUKABU_TOKEN",
        "API_TOKEN",
        "TOKEN",
    ):
        try:
            os.environ[name] = token_s
        except Exception:
            pass

    # 既存コードが global_data / config 等のモジュール変数を直接見る場合に備える。
    for module_name in (
        "global_data",
        "config.global_data",
        "core.global_data",
        "trading.global_data",
    ):
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


# ============================================================
# Token 再取得
# ============================================================
def refresh_token(apipassword=None):
    global API_TOKEN, API_PASSWORD

    conf = _load_settings()
    sec = _get_section(conf)
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

    token = result.get("Token")
    if not token:
        raise ValueError("token を取得できませんでした")

    _publish_token(token)
    API_PASSWORD = str(api_password)

    # settings.ini がある場合だけ保存。保存失敗でも runtime token refresh 自体は成功させる。
    try:
        _save_token(token)
    except Exception:
        pass

    return token


# ============================================================
# Token 取得（refresh しない）
# ============================================================
def get_valid_token():
    global API_TOKEN

    if API_TOKEN:
        return API_TOKEN

    conf = _load_settings()
    sec = _require_section(conf)

    token = conf.get(sec, "token", fallback=None)
    return _publish_token(token)


# ============================================================
# requests 経由 kabu API 401 自動再試行
# ============================================================
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
    """Patch requests so get_orders/positions do not retry with stale X-API-KEY after 401."""
    global _REQUESTS_PATCHED, _REQUESTS_PATCH_ORIGINAL
    if _REQUESTS_PATCHED:
        return True
    try:
        import requests  # type: ignore
    except Exception:
        return False

    try:
        original = requests.sessions.Session.request
        if getattr(original, "_kabu_token_401_retry_patch_v1", False):
            _REQUESTS_PATCHED = True
            return True

        def _patched_request(self, method, url, **kwargs):
            is_kabu = _is_kabu_api_url(url) and not _is_token_url(url)
            if is_kabu:
                try:
                    token = get_valid_token()
                    kwargs["headers"] = _merge_api_key(kwargs.get("headers"), token)
                except Exception:
                    pass

            resp = original(self, method, url, **kwargs)
            if not is_kabu or getattr(resp, "status_code", None) != 401:
                return resp

            try:
                new_token = refresh_token()
                retry_kwargs = dict(kwargs)
                retry_kwargs["headers"] = _merge_api_key(retry_kwargs.get("headers"), new_token)
                retry_kwargs.setdefault("timeout", kwargs.get("timeout", None))
                retry_resp = original(self, method, url, **retry_kwargs)
                logger.warning(
                    "[KABU API REQUESTS 401 RETRY] url=%s status_before=401 status_after=%s token_len=%s",
                    str(url),
                    getattr(retry_resp, "status_code", None),
                    len(str(new_token or "")),
                )
                return retry_resp
            except Exception:
                logger.exception("[KABU API REQUESTS 401 RETRY] refresh/retry failed url=%s", url)
                return resp

        _patched_request._kabu_token_401_retry_patch_v1 = True  # type: ignore[attr-defined]
        _patched_request._original = original  # type: ignore[attr-defined]
        requests.sessions.Session.request = _patched_request
        _REQUESTS_PATCH_ORIGINAL = original
        _REQUESTS_PATCHED = True
        logger.warning("[KABU API REQUESTS 401 RETRY] installed")
        return True
    except Exception:
        logger.exception("[KABU API REQUESTS 401 RETRY] install failed")
        return False


# token_manager は全プロセスで早期importされるため、requestsパッチもここで軽く入れる。
try:
    install_requests_401_retry_patch()
except Exception:
    pass
