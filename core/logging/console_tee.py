# ============================================================
# File   : core/logging/console_tee.py
# Purpose:
#   - print / stdout / stderr / logging console 出力を
#     コンソール表示しながら同時にファイル保存する
# ============================================================

from __future__ import annotations

import os
import sys
import logging
import datetime as dt
from pathlib import Path
from typing import Optional, TextIO


class TeeStream:
    def __init__(self, *streams: TextIO):
        self.streams = streams
        self.encoding = "utf-8"

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return False


_console_file: Optional[TextIO] = None
_console_path: Optional[Path] = None


def setup_console_tee(
    log_dir: str | Path = r"\\192.168.0.22\AutoStockBuyAndSell\Logs\console",
    *,
    prefix: str = "console",
    level: int = logging.INFO,
) -> Path:
    """
    stdout / stderr / logging を tee 化する。
    既存の logging.StreamHandler も tee 側へ付け替える。
    """
    global _console_file, _console_path

    if _console_file is not None and _console_path is not None:
        rebind_logging_streams_to_console_tee()
        return _console_path

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    pid = os.getpid()
    _console_path = log_dir / f"{prefix}_{ts}_{pid}.log"

    _console_file = open(
        _console_path,
        "a",
        encoding="utf-8",
        errors="replace",
        buffering=1,
    )

    sys.stdout = TeeStream(sys.__stdout__, _console_file)
    sys.stderr = TeeStream(sys.__stderr__, _console_file)

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    stream_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
    ]

    if stream_handlers:
        for h in stream_handlers:
            try:
                h.setStream(sys.stderr)
                h.setLevel(level)
                if h.formatter is None:
                    h.setFormatter(formatter)
            except Exception:
                pass
    else:
        h = logging.StreamHandler(sys.stderr)
        h.setLevel(level)
        h.setFormatter(formatter)
        root.addHandler(h)

    def _excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger(__name__).critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _excepthook

    print(f"[CONSOLE LOG] save to: {_console_path}")

    return _console_path


def rebind_logging_streams_to_console_tee() -> None:
    """
    setup_logging() / system_startup() 等で後から handler が作り直された場合に、
    logging.StreamHandler を現在の sys.stderr へ再接続する。
    """
    root = logging.getLogger()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    stream_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
    ]

    if not stream_handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setLevel(logging.INFO)
        h.setFormatter(formatter)
        root.addHandler(h)
        return

    for h in stream_handlers:
        try:
            h.setStream(sys.stderr)
            if h.formatter is None:
                h.setFormatter(formatter)
        except Exception:
            pass


__all__ = [
    "setup_console_tee",
    "rebind_logging_streams_to_console_tee",
    "TeeStream",
]