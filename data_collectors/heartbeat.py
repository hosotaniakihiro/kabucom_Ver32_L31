# ============================================================
# File   : data_collectors/heartbeat.py
# Version: DATA-COLLECTORS-HEARTBEAT-V1
# ============================================================

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from data_collectors.config import LOG_DIR


def write_heartbeat(name: str, **payload: Any) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{name}.heartbeat.json"

    data = {
        "name": name,
        "pid": os.getpid(),
        "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **payload,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
