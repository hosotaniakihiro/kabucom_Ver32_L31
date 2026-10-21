# ============================================================
# File   : utils/nas_sqlite_io_guard.py
# Version: V1.0-NAS-SQLITE-IO-GUARD
# ------------------------------------------------------------
# 旧 core/startup/nas_sqlite_io_guard_patch.py を本文化した際の共有部分。
#
# 目的:
#   NAS/SMB 上の sqlite DB が一時的に open 不能 / disk I/O error になると、
#   ranking selector / symbol_flags filter が同じDBを短時間に何度も開き、
#   PUSH購読更新やschedulerを重くする。
#
# 対策:
#   - sqlite OperationalError / disk I/O error を短時間ネガティブキャッシュ
#   - キャッシュ中は同じDBを再openせず空DF/None扱いで即fallback
#   - symbol_flags はI/O失敗時に pass-through し、購読更新自体は止めない
#
# ネガティブキャッシュは複数のホストモジュール
# (trading/push/subscription_manager/*, ats/ats_ranking/db_path.py) で共有する
# 必要があるため、旧パッチと同じく単一モジュールに置いている。
#
# 適用スコープ: 旧パッチは main.py からのみ install されていたため、
# guard_enabled() が main.py プロセスかどうかを判定する。
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1.0-NAS-SQLITE-IO-GUARD"

_BAD_UNTIL: dict[str, float] = {}
_SCOPE: bool | None = None

_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def guard_enabled() -> bool:
    """旧パッチが install されていたプロセス (main.py) かどうか。"""
    global _SCOPE
    if _SCOPE is not None:
        return _SCOPE
    enabled = False
    try:
        argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        if not any(n in argv_text for n in _DB_PROCESS_NAMES):
            enabled = "main.py" in argv_text
    except Exception:
        enabled = False
    _SCOPE = enabled
    return enabled


def _norm_path(path: Any) -> str:
    try:
        return str(path or "").replace("/", "\\").lower()
    except Exception:
        return str(path or "")


def cooldown_sec() -> float:
    return max(5.0, _env_float("NAS_SQLITE_IO_GUARD_COOLDOWN_SEC", 20.0))


def is_bad(path: Any) -> bool:
    """このパスが直近のI/O失敗によるクールダウン中か。"""
    if not guard_enabled():
        return False
    key = _norm_path(path)
    until = float(_BAD_UNTIL.get(key, 0.0) or 0.0)
    return until > time.time()


def mark_bad(path: Any, err: BaseException | None = None) -> None:
    key = _norm_path(path)
    until = time.time() + cooldown_sec()
    _BAD_UNTIL[key] = until
    logger.warning("[NAS SQLITE IO GUARD] mark bad path=%s cooldown=%.1fs err=%s", path, cooldown_sec(), err)


def looks_io_error(err: BaseException) -> bool:
    s = str(err).lower()
    return (
        isinstance(err, sqlite3.OperationalError)
        and (
            "unable to open database file" in s
            or "disk i/o error" in s
            or "database is locked" in s
            or "interrupted" in s
        )
    ) or "disk i/o error" in s or "unable to open database file" in s


def handle_error(path: Any, err: BaseException) -> bool:
    """I/O 系エラーならネガティブキャッシュへ登録して True を返す。

    True の場合、呼び出し側は例外を握りつぶして空の結果を返す。
    False の場合は再送出する (旧パッチと同じ)。
    """
    if not guard_enabled():
        return False
    if looks_io_error(err):
        mark_bad(path, err)
        return True
    return False


__all__ = [
    "VERSION",
    "guard_enabled",
    "cooldown_sec",
    "is_bad",
    "mark_bad",
    "looks_io_error",
    "handle_error",
]
