# ============================================================
# File   : data_collectors/config.py
# Version: DATA-COLLECTORS-CONFIG-V1
# ============================================================

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path


def find_project_root() -> Path:
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = find_project_root()

DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"
BASE_DIR = Path(os.environ.get("KABU_BASE_DIR", DEFAULT_BASE_DIR))

RAW_DATA_DIR = BASE_DIR / "raw_data" / "kabu_station"
PUSH_DIR = RAW_DATA_DIR / "push"
RANKING_DIR = RAW_DATA_DIR / "ranking"
SUMMARY_DIR = RAW_DATA_DIR / "summary"
SUBSCRIPTION_DIR = RAW_DATA_DIR / "push_subscription"
LOG_DIR = BASE_DIR / "Logs" / "data_collectors"


def trade_date_yyyymmdd() -> str:
    return os.environ.get("KABU_TRADE_DATE", dt.datetime.now().strftime("%Y%m%d"))


def push_db_path(trade_date: str | None = None) -> Path:
    d = trade_date or trade_date_yyyymmdd()
    return PUSH_DIR / f"push{d}.db"


def ranking_db_path(trade_date: str | None = None) -> Path:
    d = trade_date or trade_date_yyyymmdd()
    return RANKING_DIR / f"ranking{d}.db"


def summary_db_path(trade_date: str | None = None) -> Path:
    d = trade_date or trade_date_yyyymmdd()
    return SUMMARY_DIR / f"summary{d}.db"


def subscription_db_path(trade_date: str | None = None) -> Path:
    d = trade_date or trade_date_yyyymmdd()
    return SUBSCRIPTION_DIR / f"subscription_history{d}.db"


SQLITE_TIMEOUT_SEC = float(os.environ.get("KABU_SQLITE_TIMEOUT_SEC", "30"))
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("KABU_SQLITE_BUSY_TIMEOUT_MS", "30000"))

HEARTBEAT_INTERVAL_SEC = float(os.environ.get("DATA_COLLECTORS_HEARTBEAT_SEC", "30"))
RESTART_DELAY_SEC = float(os.environ.get("DATA_COLLECTORS_RESTART_DELAY_SEC", "5"))

PUSH_REGISTER_SEC = float(os.environ.get("PUSH_REGISTER_SEC", "4.8"))
PUSH_SWITCH_GAP_SEC = float(os.environ.get("PUSH_SWITCH_GAP_SEC", "0.2"))
PUSH_BATCH_SIZE = int(os.environ.get("PUSH_BATCH_SIZE", "50"))
PUSH_TARGET_TOTAL = int(os.environ.get("PUSH_TARGET_TOTAL", "100"))
