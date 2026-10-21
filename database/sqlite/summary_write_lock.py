# ============================================================
# File   : database/sqlite/summary_write_lock.py
# ------------------------------------------------------------
# summaryYYYYMMDD.db (NAS) への書き込みをプロセスをまたいで 1 本にする。
#
# 2026-10-19:
#   summary DB が引け後に "database disk image is malformed" で壊れる
#   (9/10, 9/17, 9/24, 10/05, 10/07, 10/16)。同じ DB に
#   yahoo_complement / summary_database / push_receiver が書き、
#   main_database が毎分 checkpoint する。壊れたのは毎回、引け後に
#   yahoo の一括 upsert と他プロセスの書き込みが重なった直後だった
#   (9/24 は 17:20 の summary 書き込み → 17:28、9/10 は 18:00 → 18:01)。
#   直前に NAS の I/O エラーは出ていない。
#
#   全プロセスは同じ PC で動くので、Windows の名前付き mutex で
#   「summary DB に書くのは同時に 1 プロセス」にする。
#   プロセスが落ちると mutex は OS が解放する (WAIT_ABANDONED)。
#
#   ザラ場は書き込み頻度が高く、待ちが発注判断を遅らせかねないので、
#   既定では引け後 (15:30〜翌 08:00) だけ有効にする。
#
# env:
#   SUMMARY_WRITE_LOCK_MODE = after_close (既定) / always / off
#   SUMMARY_WRITE_LOCK_TIMEOUT_SEC = 120  取れなければ警告して書く (止めない)
# ============================================================

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import sys
import time
from typing import Iterator

logger = logging.getLogger(__name__)

_MUTEX_NAME = "Local\\autostock_summary_db_write"
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED = 0x00000080
_WAIT_TIMEOUT = 0x00000102

_handle = None


def _mode() -> str:
    return str(os.environ.get("SUMMARY_WRITE_LOCK_MODE", "after_close")).strip().lower()


def _timeout_sec() -> float:
    try:
        return max(1.0, float(os.environ.get("SUMMARY_WRITE_LOCK_TIMEOUT_SEC", "120")))
    except Exception:
        return 120.0


def is_active(now: dt.datetime | None = None) -> bool:
    mode = _mode()
    if mode in {"off", "0", "false", "no"} or sys.platform != "win32":
        return False
    if mode in {"always", "1", "on"}:
        return True
    t = (now or dt.datetime.now()).time()
    return t >= dt.time(15, 30) or t < dt.time(8, 0)


def is_summary_target(name: object) -> bool:
    """テーブル名 stock_summary_* か、パスが summaryYYYYMMDD.db なら True。"""
    s = str(name or "").replace("/", "\\").lower()
    if s.startswith("stock_summary_"):
        return True
    base = s.rsplit("\\", 1)[-1]
    return base.startswith("summary2") and base.endswith(".db")


def _get_handle():
    global _handle
    if _handle is None:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        h = k32.CreateMutexW(None, False, _MUTEX_NAME)
        if not h:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        _handle = h
    return _handle


@contextlib.contextmanager
def summary_write_lock(label: str = "") -> Iterator[None]:
    """summary DB への 1 回の書き込み (1 トランザクション) を囲む。

    mutex はスレッド所有で再入可。取得したスレッドで解放する。
    取れない・使えないときは書き込みを止めずにそのまま進む。
    """
    if not is_active():
        yield
        return

    import ctypes

    try:
        h = _get_handle()
    except Exception:
        logger.warning("[SUMMARY WRITE LOCK] mutex unavailable; write without lock label=%s", label, exc_info=True)
        yield
        return

    k32 = ctypes.windll.kernel32
    t0 = time.time()
    rc = k32.WaitForSingleObject(h, int(_timeout_sec() * 1000))
    waited = time.time() - t0
    acquired = rc in (_WAIT_OBJECT_0, _WAIT_ABANDONED)
    if rc == _WAIT_ABANDONED:
        logger.warning("[SUMMARY WRITE LOCK] previous owner died while holding the lock label=%s", label)
    if not acquired:
        logger.warning(
            "[SUMMARY WRITE LOCK] timeout %.1fs; write without lock label=%s rc=%s", waited, label, rc
        )
    elif waited >= 1.0:
        logger.info("[SUMMARY WRITE LOCK] acquired after wait=%.1fs label=%s", waited, label)
    try:
        yield
    finally:
        if acquired:
            k32.ReleaseMutex(h)
