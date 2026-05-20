# ============================================================
# File   : core/logging/console_tee.py
# Purpose:
#   - print / stdout / stderr / logging console 出力を
#     コンソール表示しながら同時にファイル保存する
# Version: Ver02-FORCE-TIMESTAMP-FORMATTER
# ------------------------------------------------------------
# 修正:
#   - optional系などが %(message)s だけの formatter を設定した場合でも、
#     root StreamHandler を必ず時刻付き formatter に戻す。
#   - print文は仕様上そのまま。logging経由の出力は時刻付きになる。
# ============================================================

from __future__ import annotations

import os
import sys
import logging
import datetime as dt
from pathlib import Path
from typing import Optional, TextIO


DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


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


def _timestamp_formatter() -> logging.Formatter:
    return logging.Formatter(DEFAULT_LOG_FORMAT)


def _force_timestamp_formatter(handler: logging.Handler, *, level: int | None = None) -> None:
    """既存formatterがmessage-onlyでも、必ず時刻付きへ上書きする。"""
    try:
        if level is not None:
            handler.setLevel(level)
        handler.setFormatter(_timestamp_formatter())
    except Exception:
        pass


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
        rebind_logging_streams_to_console_tee(force_formatter=True)
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

    stream_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
    ]

    if stream_handlers:
        for h in stream_handlers:
            try:
                h.setStream(sys.stderr)
                _force_timestamp_formatter(h, level=level)
            except Exception:
                pass
    else:
        h = logging.StreamHandler(sys.stderr)
        _force_timestamp_formatter(h, level=level)
        root.addHandler(h)

    def _excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger(__name__).critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _excepthook

    print(f"[CONSOLE LOG] save to: {_console_path}")

    return _console_path


def rebind_logging_streams_to_console_tee(*, force_formatter: bool = True) -> None:
    """
    setup_logging() / system_startup() 等で後から handler が作り直された場合に、
    logging.StreamHandler を現在の sys.stderr へ再接続する。

    force_formatter=True:
      既存formatterが %(message)s のみでも必ず時刻付きformatterへ戻す。
    """
    root = logging.getLogger()

    stream_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
    ]

    if not stream_handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setLevel(logging.INFO)
        h.setFormatter(_timestamp_formatter())
        root.addHandler(h)
        return

    for h in stream_handlers:
        try:
            h.setStream(sys.stderr)
            if force_formatter or h.formatter is None:
                _force_timestamp_formatter(h)
        except Exception:
            pass


__all__ = [
    "setup_console_tee",
    "rebind_logging_streams_to_console_tee",
    "TeeStream",
]
