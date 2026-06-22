# ============================================================
# File   : data_collectors/logging_setup.py
# Version: DATA-COLLECTORS-LOGGING-V2-FORCE-CONSOLE-FILE
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from pathlib import Path

from data_collectors.config import LOG_DIR


_INSTALLED_FILE_KEYS: set[str] = set()


def setup_logging(name: str, *, also_file: bool = True) -> logging.Logger:
    """Setup timestamped console logging and save to X:\\logs\\console by default.

    sitecustomize/usercustomize can create root handlers before this function runs.
    Therefore this function does not rely on `if not logger.handlers`; it also
    updates existing handler formatters and adds a per-name FileHandler once.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not root.handlers:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    else:
        for h in root.handlers:
            try:
                h.setFormatter(fmt)
            except Exception:
                pass

    if also_file and name not in _INSTALLED_FILE_KEYS:
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        pid = os.getpid()
        log_path = LOG_DIR / f"{name}_{ts}_{pid}.log"
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
        _INSTALLED_FILE_KEYS.add(name)
        logging.getLogger(__name__).warning("[DATA COLLECTORS LOG] save to: %s", log_path)

    return logging.getLogger(name)
