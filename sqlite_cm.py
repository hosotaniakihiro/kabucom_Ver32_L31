# ============================================================
# File   : sqlite_cm.py
# Version: Ver1.0-SQLITE-COMMITTING-CONNECT (R18)
# ------------------------------------------------------------
# 【なぜ必要か】
#   `with sqlite3.connect(...) as conn:` は Python 公式仕様どおり
#   commit / rollback しかせず、**接続を閉じない**。
#   traceback / cursor / logger.exception のフレームが conn を掴むと
#   GC まで生き残り、1 接続 = Win32 ハンドル 3 個を保持する
#   (第6/第11ラウンドの実測: 500 接続で +1502 ハンドル)。
#   DB が NAS(SMB) 上だと SMB のオープン上限に当たり、
#   `sqlite3.OperationalError: disk I/O error` /
#   `unable to open database file` / `OSError: [Errno 24]` が
#   全 NAS DB で同時に発生する。
#
# 【なぜ closing_connect ではないか】
#   database/sqlite_util.closing_connect は close するが commit しない。
#   `with sqlite3.connect(...)` からの素朴な置換に使うと
#   **書き込みが黙って消える**。
#   committing_connect は「従来の with と同じ commit/rollback」+「close」で、
#   読み取り経路にも書き込み経路にもそのまま置換できる。
#
# 【依存】
#   標準ライブラリのみ。database パッケージを import しない
#   (database/__init__.py は engine を生成するため、
#    起動順序を変えないようにここでは触らない)。
# ============================================================

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_STATS_LOCK = threading.Lock()
_OPENED = 0
_CLOSED = 0
_CLOSE_FAILED = 0


@contextlib.contextmanager
def committing_connect(*args: Any, **kwargs: Any) -> Iterator[sqlite3.Connection]:
    """`with sqlite3.connect(...) as conn:` と同じ意味 + 必ず close する。

    - 正常終了なら commit、例外なら rollback (従来どおり)
    - どちらの場合も finally で close する (従来は閉じていなかった)

    引数はそのまま sqlite3.connect へ渡す。
    """
    global _OPENED, _CLOSED, _CLOSE_FAILED

    conn = sqlite3.connect(*args, **kwargs)

    with _STATS_LOCK:
        _OPENED += 1

    try:
        # sqlite3.Connection の __enter__/__exit__ = commit / rollback
        with conn:
            yield conn
    finally:
        try:
            conn.close()
            with _STATS_LOCK:
                _CLOSED += 1
        except Exception:
            with _STATS_LOCK:
                _CLOSE_FAILED += 1
            logger.debug("[SQLITE CM] close failed", exc_info=True)


def stats() -> dict:
    """opened / closed / outstanding を返す (resource_monitor 用)。"""
    with _STATS_LOCK:
        return {
            "opened": _OPENED,
            "closed": _CLOSED,
            "close_failed": _CLOSE_FAILED,
            "outstanding": _OPENED - _CLOSED,
        }


def reset_stats() -> None:
    global _OPENED, _CLOSED, _CLOSE_FAILED
    with _STATS_LOCK:
        _OPENED = 0
        _CLOSED = 0
        _CLOSE_FAILED = 0


__all__ = ["committing_connect", "stats", "reset_stats"]
