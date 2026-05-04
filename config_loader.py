# ============================================================
# config_loader.py
# ------------------------------------------------------------
# settings.ini 読み込み専用
# ============================================================

import configparser
import os
from pathlib import Path

conf = configparser.ConfigParser()
CONFIG_PATH = Path(__file__).resolve().parent / "settings.ini"
conf.read(CONFIG_PATH, encoding="utf-8")


def get_path(key: str) -> Path:
    """
    settings.ini [paths] から Path を返す
    """
    value = conf.get("paths", key, fallback=None)
    if not value:
        raise KeyError(f"[paths] {key} not found in settings.ini")
    return Path(value)


# --- paths ---
STOCK_DATA_DIR = get_path("stock_data_dir")
BASE_PATH      = get_path("base_path")
EXCEL_PATH     = get_path("excel_path")
