# ============================================================
# token_manager.py（Ver25-API-SETTINGS-CANDIDATES）
# ------------------------------------------------------------
# ・main.py / startup.py から安全に import RefreshToken が可能
# ・循環importなし
# ・プロジェクト共通 settings.ini に [trade] だけがある場合でも停止しない
# ・[aukabu] / [kabusapi] を含む設定ファイルを候補から順に探す
# ・token 保存は API セクションが見つかった設定ファイルへ行う
# ============================================================

from __future__ import annotations

import json
import os
import urllib.request
from configparser import ConfigParser
from pathlib import Path

API_URL = "http://localhost:18080/kabusapi"
CONFIG_PATH = "settings.ini"  # backward compatible default name
API_TOKEN = None
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
    for env_name in ("KABUSAPI_SETTING_INI", "AUKABU_SETTING_INI", "KABU_API_SETTING_INI", "SETTING_INI_PATH", "KABU_SETTING_INI"):
        v = os.getenv(env_name)
        if v:
            out.append(Path(v))

    for root in _project_root_candidates():
        out.extend([
            root / "settings.local.ini",
            root / "setting.local.ini",
            root / "kabusapi.ini",
            root / "aukabu.ini",
            root / "config" / "settings.local.ini",
            root / "config" / "setting.local.ini",
            root / "config" / "kabusapi.ini",
            root / "config" / "aukabu.ini",
            root / "settings.ini",
            root / "setting.ini",
            root / "config" / "settings.ini",
            root / "config" / "setting.ini",
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
    # _require_section() が分かりやすいエラーを出す。
    conf = ConfigParser()
    _CONFIG_FILE_PATH = None
    tried = [str(p) for p in _config_candidates()]
    conf["__diagnostic__"] = {"existing_files": " | ".join(last_existing), "tried": " | ".join(tried)}
    return conf


def _require_section(conf: ConfigParser) -> str:
    sec = _get_section(conf)
    if sec:
        return sec
    existing = ""
    tried = ""
    try:
        existing = conf.get("__diagnostic__", "existing_files", fallback="")
        tried = conf.get("__diagnostic__", "tried", fallback="")
    except Exception:
        pass
    raise ValueError(
        "[aukabu] or [kabusapi] があるAPI設定ファイルが見つかりません。"
        " settings.ini は [trade] 用に使えるため、API認証は settings.local.ini / kabusapi.ini / aukabu.ini "
        "または環境変数 KABUSAPI_SETTING_INI で指定してください。"
        f" existing={existing} tried={tried}"
    )


# ============================================================
# Token 保存
# ============================================================
def _save_token(token):
    conf = _load_settings()
    sec = _require_section(conf)

    conf.set(sec, "token", token)
    path = _CONFIG_FILE_PATH or CONFIG_PATH
    with open(path, "w", encoding="utf-8") as f:
        conf.write(f)


# ============================================================
# Token 再取得
# ============================================================
def refresh_token(apipassword=None):
    global API_TOKEN

    conf = _load_settings()
    sec = _require_section(conf)

    if apipassword is None:
        apipassword = conf.get(sec, "apipassword", fallback=None)

    if not apipassword:
        raise ValueError(f"API設定ファイルに apipassword がありません path={_CONFIG_FILE_PATH} section={sec}")

    url = f"{API_URL}/token"
    headers = {"Content-Type": "application/json"}

    data = json.dumps({"APIPassword": apipassword}).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    with urllib.request.urlopen(req, timeout=5) as res:
        raw = res.read().decode()
        result = json.loads(raw)

    token = result.get("Token")
    if not token:
        raise ValueError("token を取得できませんでした")

    API_TOKEN = token
    _save_token(token)

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
