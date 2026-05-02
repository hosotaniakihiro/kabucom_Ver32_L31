# ============================================================
# File   : ats/ats_api.py
# Version: Ver1.0-ATS-API
# ------------------------------------------------------------
# kabu Station ATS API 呼び出し helper
# ============================================================

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

from global_state import global_data

logger = logging.getLogger(__name__)

API_BASE = "http://localhost:18080/kabusapi"
COOLDOWN_SEC_ON_429 = 30


def _mark_429_now() -> None:
    global_data.ats_last_429_at = time.time()


def is_in_429_cooldown(cooldown_sec: int = COOLDOWN_SEC_ON_429) -> bool:
    ts = getattr(global_data, "ats_last_429_at", 0) or 0
    if not ts:
        return False
    return (time.time() - float(ts)) < cooldown_sec


def api_headers() -> Optional[dict]:
    token = getattr(global_data, "token_value", None)
    if not token:
        return None

    return {
        "Content-Type": "application/json",
        "X-API-KEY": token,
    }


def request_api_put(path: str, payload_obj: dict | None, timeout: int = 8):
    headers = api_headers()
    if not headers:
        logger.warning("[ATS API] token missing path=%s", path)
        return False, None, "token missing"

    if is_in_429_cooldown():
        logger.warning("[ATS API] skipped in 429 cooldown path=%s", path)
        return False, 429, "cooldown"

    data = None
    if payload_obj is not None:
        data = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=data,
        method="PUT",
        headers=headers,
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8", errors="ignore")
            return True, getattr(res, "status", 200), body

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        if e.code == 429:
            _mark_429_now()
        return False, e.code, body

    except Exception as e:
        return False, None, str(e)