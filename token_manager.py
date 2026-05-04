# ============================================================
# token_manager.py（Ver24-SAFE-LTS）
# ------------------------------------------------------------
# ・main.py / startup.py から安全に import RefreshToken が可能
# ・循環importなし
# ・settings.ini の token を維持
# ============================================================

import os
import json
import urllib.request
from configparser import ConfigParser

API_URL = "http://localhost:18080/kabusapi"
CONFIG_PATH = "settings.ini"

API_TOKEN = None


# ============================================================
# 設定ファイル読込
# ============================================================
def _load_settings():
    conf = ConfigParser()
    if os.path.exists(CONFIG_PATH):
        conf.read(CONFIG_PATH, encoding="utf-8")
    return conf


def _get_section(conf):
    if conf.has_section("aukabu"):
        return "aukabu"
    if conf.has_section("kabusapi"):
        return "kabusapi"
    raise ValueError("settings.ini に [aukabu] or [kabusapi] がありません")


# ============================================================
# Token 保存
# ============================================================
def _save_token(token):
    conf = _load_settings()
    sec = _get_section(conf)

    conf.set(sec, "token", token)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        conf.write(f)


# ============================================================
# Token 再取得
# ============================================================
def refresh_token(apipassword=None):
    global API_TOKEN

    conf = _load_settings()
    sec = _get_section(conf)

    if apipassword is None:
        apipassword = conf.get(sec, "apipassword", fallback=None)

    if not apipassword:
        raise ValueError("settings.ini に apipassword がありません")

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
    sec = _get_section(conf)

    token = conf.get(sec, "token", fallback=None)
    API_TOKEN = token
    return API_TOKEN
