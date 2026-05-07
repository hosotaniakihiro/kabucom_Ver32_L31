# ============================================================
# File   : data_collectors/logging_setup.py
# Version: DATA-COLLECTORS-LOGGING-V1
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from pathlib import Path

from data_collectors.config import LOG_DIR


def setup_logging(name: str, *, also_file: bool = True) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # 二重追加防止
    if not logger.handlers:
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

        if also_file:
            ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            pid = os.getpid()
            log_path = LOG_DIR / f"{name}_{ts}_{pid}.log"
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
            logging.getLogger(__name__).info("[DATA COLLECTORS LOG] save to: %s", log_path)

    return logging.getLogger(name)
