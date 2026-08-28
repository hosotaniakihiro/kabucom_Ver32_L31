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

import atexit
import os
import queue
import sys
import logging
import datetime as dt
import threading
import time
from pathlib import Path
from typing import Optional, TextIO
from nas_root import nas_path, nas_root


DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


# ============================================================
# 非同期ファイルライタ
# ------------------------------------------------------------
# コンソールログの保存先は NAS
#   \\192.168.0.22\AutoStockBuyAndSell\Logs\console\
# 旧実装は open(..., buffering=1) の上に TeeStream.write() が
# 1 行ごとに flush() をかけていたため、**ログ 1 行 = SMB 往復 1 回**
# だった。実測 (アイドル時の NAS):
#     1 行 flush  mean 7.6〜11.1 ms / p95 27〜38 ms / max 184 ms
#     ローカル                     0.008〜0.013 ms   (約 1000 倍差)
# しかも logging.Handler.handle() は `with self.lock:` の中で
# stream.write() を呼ぶので、**1 スレッドの NAS 書き込みが
# 全スレッドのログをブロックする**。
#
# 2026-08-28 08:55 の cancel SLA breach スタックダンプが
# まさにその状態を捉えていた:
#     indicator_pipeline._log_column_health -> logger.info
#       -> logging/__init__.py handle -> with self.lock:
#
# 実測ログ (9,603 行 / 390 秒) を平均 10 ms/行 で見積もると
# **約 96 秒 = 経過時間の 25%** がログの NAS 書き込みだった。
# 08:51 の 50 秒 tick では core.global_context.context の
# ログだけで 23.5 秒 (窓の 44.3%) を占めていた。
#
# ここではファイル側だけをキューへ逃がす。
#   - 呼び出しスレッドは put_nowait して即座に戻る (NAS を待たない)
#   - 書き込みはライタスレッドがまとめて行い、間隔を空けて flush する
#   - キューが溢れたら **捨てる** (売買スレッドを止めない)。捨てた数は必ず残す
#   - コンソール表示側は同期のまま (速いし、人が見る順序を保つ)
# ============================================================

class _AsyncFileWriter:
    """NAS 上のログファイルへ非同期に書き出す。呼び出し側は待たない。"""

    def __init__(self, stream: TextIO, *, maxsize: int, flush_interval: float) -> None:
        self._stream = stream
        self._q: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=max(1000, maxsize))
        self._flush_interval = max(0.05, flush_interval)
        self._dropped = 0
        self._written = 0
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="console-tee-writer", daemon=True
        )
        self._thread.start()
        atexit.register(self.close)

    # -- 呼び出し側 (売買スレッド) --------------------------------
    def write(self, data: str) -> None:
        if self._stopping.is_set():
            self._write_direct(data)
            return
        try:
            self._q.put_nowait(data)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def flush(self) -> None:
        # 呼び出し側は NAS の flush を待たない。ライタスレッドが定期的に行う。
        return None

    def _write_direct(self, data: str) -> None:
        try:
            self._stream.write(data)
            self._stream.flush()
        except Exception:
            pass

    # -- ライタスレッド ------------------------------------------
    def _run(self) -> None:
        pending: list = []
        last_flush = time.monotonic()
        while True:
            try:
                item = self._q.get(timeout=self._flush_interval)
                if item is None:
                    break
                pending.append(item)
                # 溜まっている分はまとめて取り出す (SMB 往復を減らす)
                while len(pending) < 512:
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        pending.append(None)
                        break
                    pending.append(nxt)
            except queue.Empty:
                pass

            stop_after = False
            if pending and pending[-1] is None:
                pending.pop()
                stop_after = True

            now = time.monotonic()
            if pending and (stop_after or (now - last_flush) >= self._flush_interval):
                self._drain(pending)
                pending = []
                last_flush = now
            if stop_after:
                break

        if pending:
            self._drain(pending)

    def _drain(self, chunk: list) -> None:
        if not chunk:
            return
        try:
            self._stream.write("".join(chunk))
            self._stream.flush()
            with self._lock:
                self._written += len(chunk)
        except Exception:
            # NAS が一時的に落ちてもプロセスは止めない
            pass
        dropped = 0
        with self._lock:
            if self._dropped:
                dropped = self._dropped
                self._dropped = 0
        if dropped:
            try:
                self._stream.write(
                    "%s [WARNING] console_tee: [CONSOLE TEE] queue overflow -> %d 行を破棄した "
                    "(NAS 書き込みが追いついていない。売買スレッドは止めない)\n"
                    % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3], dropped)
                )
                self._stream.flush()
            except Exception:
                pass

    def stats(self) -> dict:
        with self._lock:
            return {
                "queued": self._q.qsize(),
                "written": self._written,
                "dropped": self._dropped,
                "alive": self._thread.is_alive(),
            }

    def close(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        try:
            self._q.put_nowait(None)
        except Exception:
            pass
        try:
            self._thread.join(3.0)
        except Exception:
            pass


class TeeStream:
    """コンソールへは同期、ファイルへは非同期で書く。

    file_writer が渡された場合、その stream への write は
    キュー経由になり、呼び出しスレッドは NAS を待たない。
    """

    def __init__(self, *streams: TextIO, file_writer: Optional[_AsyncFileWriter] = None):
        self.streams = streams
        self.encoding = "utf-8"
        self._file_writer = file_writer

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
        if self._file_writer is not None:
            self._file_writer.write(data)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass
        if self._file_writer is not None:
            self._file_writer.flush()

    def isatty(self):
        return False


_console_file: Optional[TextIO] = None
_console_path: Optional[Path] = None
_async_writer: Optional[_AsyncFileWriter] = None


def writer_stats() -> dict:
    """診断用: 非同期ライタの状態。dropped が増え続けたら NAS 側が遅い。"""
    if _async_writer is None:
        return {"async": False}
    out = _async_writer.stats()
    out["async"] = True
    return out


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
    log_dir: str | Path = nas_path(r"Logs\console"),
    *,
    prefix: str = "console",
    level: int = logging.INFO,
) -> Path:
    """
    stdout / stderr / logging を tee 化する。
    既存の logging.StreamHandler も tee 側へ付け替える。
    """
    global _console_file, _console_path, _async_writer

    if _console_file is not None and _console_path is not None:
        rebind_logging_streams_to_console_tee(force_formatter=True)
        return _console_path

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    pid = os.getpid()
    _console_path = log_dir / f"{prefix}_{ts}_{pid}.log"

    # 行バッファ (buffering=1) をやめる。flush はライタスレッドが行う。
    # 1 行ごとに SMB 往復させないことがこの変更の目的。
    _console_file = open(
        _console_path,
        "a",
        encoding="utf-8",
        errors="replace",
        buffering=1024 * 64,
    )

    use_async = _env_bool("CONSOLE_TEE_ASYNC", True)
    if use_async:
        _async_writer = _AsyncFileWriter(
            _console_file,
            maxsize=_env_int("CONSOLE_TEE_QUEUE_MAX", 200000),
            flush_interval=float(_env_int("CONSOLE_TEE_FLUSH_MS", 200)) / 1000.0,
        )
        sys.stdout = TeeStream(sys.__stdout__, file_writer=_async_writer)
        sys.stderr = TeeStream(sys.__stderr__, file_writer=_async_writer)
    else:
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
