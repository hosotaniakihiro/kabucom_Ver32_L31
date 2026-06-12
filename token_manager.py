# ============================================================
# token_manager.py（Ver27-SETTING-INI-FIRST-API-AUTH）
# ------------------------------------------------------------
# ・main.py / startup.py から安全に import RefreshToken が可能
# ・循環importなし
# ・API認証設定は setting.ini を最優先で読む
# ・古い settings.ini が残っていても、setting.ini があればそちらを優先する
# ・[aukabu] / [kabusapi] を含む設定ファイルを候補から順に探す
# ・startup_config から渡された apipassword をメモリキャッシュし、
#   force_cancel_loop などの 401 後 refresh_token() 引数なし呼び出しでも再利用する
# ・token 保存は API セクションが見つかった設定ファイルへだけ行う
# ============================================================

from __future__ import annotations

import json
import os
import urllib.request
from configparser import ConfigParser
from pathlib import Path

API_URL = "http://localhost:18080/kabusapi"
CONFIG_PATH = "setting.ini"  # API認証の標準ファイル名
API_TOKEN = None
API_PASSWORD = None
_CONFIG_FILE_PATH: str | None = None


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
    out: list[Path] = []

    # Explicit API config path has highest priority.
    # SETTING_INI_PATH / KABU_SETTING_INI を先にして、setting.ini 指定を最優先にする。
    for env_name in (
        "SETTING_INI_PATH",
        "KABU_SETTING_INI",
        "KABUSAPI_SETTING_INI",
        "AUKABU_SETTING_INI",
        "KABU_API_SETTING_INI",
    ):
        v = os.getenv(env_name)
        if v:
            out.append(Path(v))

    for root in _project_root_candidates():
        out.extend([
            # local override: setting.ini 系を最優先
            root / "setting.local.ini",
            root / "settings.local.ini",
            root / "config" / "setting.local.ini",
            root / "config" / "settings.local.ini",

            # dedicated API files
            root / "kabusapi.ini",
            root / "aukabu.ini",
            root / "config" / "kabusapi.ini",
            root / "config" / "aukabu.ini",

            # standard API auth file: setting.ini first
            root / "setting.ini",
            root / "config" / "setting.ini",

            # legacy fallback only. ここに古いtokenが残っていても setting.ini があれば使わない。
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
        "[aukabu] or [kabusapi] があるAPI設定ファイルが見つかりません。"
        " API認証は setting.ini の [kabusapi] または [aukabu] に集約してください。"
        " 必要なら環境変数 SETTING_INI_PATH で明示指定できます。"
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
# Token 保存
# ============================================================
def _save_token(token) -> bool:
    """APIセクションが見つかる場合だけ token を保存する。"""
    conf = _load_settings()
    sec = _get_section(conf)
    if not sec:
        return False

    conf.set(sec, "token", token)
    path = _CONFIG_FILE_PATH or CONFIG_PATH
    with open(path, "w", encoding="utf-8") as f:
        conf.write(f)
    return True


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
            "API設定ファイルに apipassword がありません。"
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

    API_TOKEN = token
    API_PASSWORD = str(api_password)

    # API ini がある場合だけ保存。無い構成でも 401 後リカバリは成功させる。
    try:
        _save_token(token)
    except Exception:
        # 保存失敗で runtime token refresh 自体を失敗させない。
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
    API_TOKEN = token
    return API_TOKEN
