# =========================================================
# kabu_api/utils.py（Ver24-FINAL-NO-REFRESH）
# ---------------------------------------------------------
# ✔ Tokenを固定利用（リフレッシュしない）
# ✔ settings.ini の Token を常に返すだけ
# ✔ /token API は一切使わない
# =========================================================

import os
import json
import logging
import requests
from configparser import ConfigParser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# settings.ini 読み込み
# ---------------------------------------------------------
conf = ConfigParser()
conf.read("settings.ini", encoding="utf-8")

API_URL = "http://localhost:18080/kabusapi"

# settings.ini の Token をそのまま使う（固定）
FIXED_TOKEN = conf.get("aukabu", "token", fallback=None)

if not FIXED_TOKEN:
    FIXED_TOKEN = os.getenv("KABU_API_TOKEN", "")

if not FIXED_TOKEN:
    print("⚠️ [警告] FIXED_TOKEN が未設定です。settings.ini の [aukabu] token=xxxxx を設定してください。")

# API用パスワード
Password = conf.get("aukabu", "password", fallback=None)
if not Password:
    Password = os.getenv("KABU_API_PASSWORD", "")


# =========================================================
# 🔑 get_valid_token（固定版）
# =========================================================
def get_valid_token():
    """
    ★ トークン固定版 ★
    settings.ini に記載した Token を常に返す。
    refresh_token() は絶対に呼ばない。
    """
    return FIXED_TOKEN


# =========================================================
# 共通 HTTP ラッパ
# =========================================================
def send_request(method: str, endpoint: str, headers: dict = None,
                 json_data: dict = None, timeout: int = 5):
    """
    - method: GET / POST / PUT / DELETE
    - endpoint: "/orders" 等
    """
    url = f"{API_URL}{endpoint}"

    try:
        if method.upper() == "GET":
            res = requests.get(url, headers=headers, timeout=timeout)

        elif method.upper() == "POST":
            res = requests.post(url, headers=headers, json=json_data, timeout=timeout)

        elif method.upper() == "PUT":
            res = requests.put(url, headers=headers, json=json_data, timeout=timeout)

        elif method.upper() == "DELETE":
            res = requests.delete(url, headers=headers, timeout=timeout)

        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        if res.status_code >= 400:
            logger.error(f"❌ HTTP {res.status_code}: {res.text}")
            return None

        try:
            return res.json()
        except json.JSONDecodeError:
            logger.error("❌ JSON decode error")
            return None

    except Exception as e:
        logger.error(f"❌ send_request 通信エラー: {e}")
        return None
