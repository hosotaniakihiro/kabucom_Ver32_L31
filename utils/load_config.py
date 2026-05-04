# utils/config.py

from configparser import ConfigParser
from pathlib import Path

_CONFIG = None

def load_config():
    global _CONFIG
    if _CONFIG is None:
        cfg = ConfigParser()
        cfg.read("setting.ini", encoding="utf-8")
        _CONFIG = cfg
    return _CONFIG

def get_path(key: str) -> Path:
    cfg = load_config()
    return Path(cfg.get("paths", key)).resolve()
