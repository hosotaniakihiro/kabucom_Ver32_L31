# ============================================================
# File   : core/diagnostics/resource_monitor.py
# Version: V1.0-RESOURCE-MONITOR
# ------------------------------------------------------------
# P0-6: disk I/O error の原因を推測で決めないための計測。
#
# 2026-08-27 の障害では
#   15:21:44  positions.db PRAGMA synchronous -> disk I/O error
#   15:21:47  push DB / positions.db 同時に "unable to open database file"
#   15:30:37  ローカルパスで OSError [Errno 24] Too many open files
# が起きた。つまり positions.db 固有の問題ではなく、
# プロセス全体のファイル/ハンドル枯渇だった。
# これを次から即座に特定できるよう、定期スナップショットと
# I/O エラー直前 5 秒のリングバッファを残す。
#
# 計測項目:
#   - Windows handle count (GetProcessHandleCount / psutil.num_handles)
#   - open files 数と内訳 (psutil があるときのみ)
#   - net connections 数
#   - thread 数と thread 名の内訳
#   - SQLAlchemy pool の checkedin / checkedout / overflow
#   - positions DB の read 回数 / エラー回数
#   - runtime_state_store の接続 opened/closed/outstanding
#
# psutil は必須にしない。無ければ Windows API (ctypes) と
# threading だけで測れるところまで測る。
# ============================================================

from __future__ import annotations

import ctypes
import datetime as dt
import logging
import os
import threading
import time
from collections import Counter, deque
from typing import Any, Deque, Dict, Optional

logger = logging.getLogger(__name__)

try:  # psutil はあれば使う。無くても動く。
    import psutil  # type: ignore
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore


# ============================================================
# LOW LEVEL
# ============================================================

_k32 = None
try:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetProcessHandleCount.restype = wintypes.BOOL
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
except Exception:  # noqa: BLE001
    _k32 = None


def win_handle_count() -> int:
    """このプロセスが保持している Win32 ハンドル数。取れなければ -1。

    SQLite 接続 1 本 = 約 3 ハンドル (実測)。NAS(SMB) 上の DB では
    これがそのままサーバ側のオープン数になるので、増え続けたらリーク。
    """
    if _k32 is None:
        return -1
    try:
        from ctypes import wintypes

        n = wintypes.DWORD(0)
        if _k32.GetProcessHandleCount(_k32.GetCurrentProcess(), ctypes.byref(n)):
            return int(n.value)
    except Exception:
        logger.debug("[RESOURCE] GetProcessHandleCount failed", exc_info=True)
    return -1


def _proc():
    if psutil is None:
        return None
    try:
        return psutil.Process()
    except Exception:
        return None


def _thread_breakdown() -> Dict[str, int]:
    """スレッド名の接頭辞ごとの本数。どの系統が増えているかを見る。"""
    c: Counter = Counter()
    try:
        for t in threading.enumerate():
            name = str(getattr(t, "name", "") or "unknown")
            # summary-main-tick-local_0 -> summary-main-tick-local
            for sep in ("_", "-"):
                head = name.rsplit(sep, 1)[0]
                if head and head != name and head[-1:].isalpha():
                    name = head
                    break
            c[name] += 1
    except Exception:
        logger.debug("[RESOURCE] thread breakdown failed", exc_info=True)
    return dict(c.most_common(15))


def _open_files_breakdown(p) -> Dict[str, Any]:
    if p is None:
        return {"count": None, "top": {}}
    try:
        files = p.open_files()
    except Exception:
        return {"count": None, "top": {}}
    c: Counter = Counter()
    for f in files:
        try:
            path = str(getattr(f, "path", "") or "")
        except Exception:
            continue
        # ディレクトリ単位に丸める。どの領域を掴んでいるかが分かれば十分。
        c[os.path.dirname(path) or path] += 1
    return {"count": len(files), "top": dict(c.most_common(8))}


def _sqlalchemy_pool_metrics() -> Dict[str, Any]:
    """engine ごとの pool 状態。NullPool は checkedout を持たないので None。"""
    out: Dict[str, Any] = {}
    try:
        import database.session as ds
    except Exception:
        return out

    for name in ("push", "summary", "position", "ranking", "tosama"):
        try:
            engine = getattr(ds, f"_{name}_engine", None)
            if engine is None:
                continue
            pool = getattr(engine, "pool", None)
            if pool is None:
                continue
            info: Dict[str, Any] = {"pool": type(pool).__name__}
            for attr in ("checkedin", "checkedout", "overflow", "size"):
                fn = getattr(pool, attr, None)
                if callable(fn):
                    try:
                        info[attr] = fn()
                    except Exception:
                        info[attr] = None
            out[name] = info
        except Exception:
            logger.debug("[RESOURCE] pool metrics failed name=%s", name, exc_info=True)
    return out


def _positions_state_stats() -> Dict[str, Any]:
    try:
        from trading.position.position_state_service import stats as _stats

        return _stats()
    except Exception:
        return {}


def _runtime_state_conn_stats() -> Dict[str, Any]:
    try:
        from trading.runtime_persistence.runtime_state_store import connection_stats

        return connection_stats()
    except Exception:
        return {}


def _sqlite_cm_stats() -> Dict[str, Any]:
    """R18: committing_connect の opened/closed/outstanding。

    `with sqlite3.connect(...)` は close しないため 99 箇所を
    committing_connect へ置換した。outstanding が増え続けていたら
    どこかで CM を経由しない接続が残っている。
    """
    try:
        from sqlite_cm import stats as _stats

        return _stats()
    except Exception:
        return {}


def _engine_conn_stats() -> Dict[str, Any]:
    try:
        import database.session as ds

        fn = getattr(ds, "connection_stats", None)
        return fn() if callable(fn) else {}
    except Exception:
        return {}


# ============================================================
# SNAPSHOT
# ============================================================

def snapshot() -> Dict[str, Any]:
    p = _proc()
    of = _open_files_breakdown(p)

    snap: Dict[str, Any] = {
        "at": dt.datetime.now(),
        "monotonic": time.monotonic(),
        "handles": win_handle_count(),
        "num_threads": threading.active_count(),
        "threads": _thread_breakdown(),
        "open_files": of["count"],
        "open_files_top": of["top"],
        "net_connections": None,
        "rss_mb": None,
        "pools": _sqlalchemy_pool_metrics(),
        "engine_conns": _engine_conn_stats(),
        "positions_state": _positions_state_stats(),
        "runtime_state_conns": _runtime_state_conn_stats(),
        "sqlite_cm": _sqlite_cm_stats(),
    }

    if p is not None:
        try:
            snap["num_threads"] = p.num_threads()
        except Exception:
            logger.debug("[RESOURCE] num_threads failed", exc_info=True)
        try:
            snap["handles"] = p.num_handles()
        except Exception:
            logger.debug("[RESOURCE] num_handles failed", exc_info=True)
        try:
            snap["net_connections"] = len(p.net_connections(kind="inet"))
        except Exception:
            logger.debug("[RESOURCE] net_connections failed", exc_info=True)
        try:
            snap["rss_mb"] = round(p.memory_info().rss / (1024 * 1024), 1)
        except Exception:
            logger.debug("[RESOURCE] memory_info failed", exc_info=True)

    return snap


def _fmt(snap: Dict[str, Any]) -> str:
    return (
        "handles=%s open_files=%s net_conn=%s threads=%s rss_mb=%s "
        "pools=%s engine_conns=%s runtime_state_conns=%s sqlite_cm=%s positions=%s open_files_top=%s threads_top=%s"
        % (
            snap.get("handles"), snap.get("open_files"), snap.get("net_connections"),
            snap.get("num_threads"), snap.get("rss_mb"),
            snap.get("pools"), snap.get("engine_conns"),
            snap.get("runtime_state_conns"),
            snap.get("sqlite_cm"),
            {k: v for k, v in (snap.get("positions_state") or {}).items()
             if k in ("db_reads", "db_errors", "db_error_episodes", "cache_hits", "broker_errors")},
            snap.get("open_files_top"), snap.get("threads"),
        )
    )


# ============================================================
# RING BUFFER (I/O エラー直前の状況)
# ============================================================

_RING: Deque[Dict[str, Any]] = deque(maxlen=64)
_RING_LOCK = threading.Lock()


def _push_ring(snap: Dict[str, Any]) -> None:
    with _RING_LOCK:
        _RING.append(snap)


def recent(seconds: float = 5.0) -> list:
    """直近 N 秒のスナップショット。I/O エラー時の chronology 用。"""
    now = time.monotonic()
    with _RING_LOCK:
        return [s for s in _RING if (now - float(s.get("monotonic") or 0.0)) <= seconds]


def log_io_error_chronology(context: str, error: Any) -> None:
    """I/O エラー発生時に直前の資源状況を吐く。

    「何が原因か」を後から推測しないで済むようにする。
    """
    try:
        rows = recent(seconds=_env_float("RESOURCE_MONITOR_CHRONOLOGY_SEC", 5.0))
        now_snap = snapshot()
        logger.error(
            "[IO ERROR CHRONOLOGY] context=%s err=%s now: %s",
            context, repr(error)[:200], _fmt(now_snap),
        )
        for s in rows[-4:]:
            logger.error(
                "[IO ERROR CHRONOLOGY]   t-%.1fs %s",
                now_snap["monotonic"] - float(s.get("monotonic") or 0.0), _fmt(s),
            )
    except Exception:
        logger.exception("[IO ERROR CHRONOLOGY] failed context=%s", context)


# ============================================================
# LOOP
# ============================================================

def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


_STARTED = False
_START_LOCK = threading.Lock()
_PEAK_HANDLES = 0
_HANDLE_WARN_AT = 0


def start_resource_monitor(interval_sec: Optional[float] = None) -> bool:
    """30 秒ごとに資源スナップショットを記録する常駐スレッドを起動する。"""
    global _STARTED

    if not _env_bool("RESOURCE_MONITOR_ENABLED", True):
        logger.warning("[RESOURCE] monitor disabled by RESOURCE_MONITOR_ENABLED")
        return False

    with _START_LOCK:
        if _STARTED:
            return False
        _STARTED = True

    interval = float(interval_sec if interval_sec is not None
                     else _env_float("RESOURCE_MONITOR_INTERVAL_SEC", 30.0))
    interval = max(5.0, interval)
    warn_handles = int(_env_float("RESOURCE_MONITOR_HANDLE_WARN", 4000.0))

    def _loop() -> None:
        global _PEAK_HANDLES, _HANDLE_WARN_AT
        logger.warning(
            "[RESOURCE] monitor started interval=%.1fs psutil=%s handle_warn=%d",
            interval, psutil is not None, warn_handles,
        )
        prev: Optional[Dict[str, Any]] = None
        while True:
            try:
                snap = snapshot()
                _push_ring(snap)

                h = int(snap.get("handles") or -1)
                if h > _PEAK_HANDLES:
                    _PEAK_HANDLES = h

                delta = ""
                if prev is not None:
                    ph = int(prev.get("handles") or -1)
                    if h >= 0 and ph >= 0:
                        delta = " handle_delta=%+d" % (h - ph)

                logger.warning("[RESOURCE] %s peak_handles=%d%s", _fmt(snap), _PEAK_HANDLES, delta)

                # ハンドルが警告水位を超えたら、リーク元の候補を名指しする。
                if h >= warn_handles and h > _HANDLE_WARN_AT:
                    _HANDLE_WARN_AT = h
                    rs = snap.get("runtime_state_conns") or {}
                    logger.error(
                        "[RESOURCE] ハンドルが警告水位を超えた handles=%d warn=%d "
                        "runtime_state_outstanding=%s sqlite_cm=%s engine_conns=%s open_files_top=%s "
                        "(NAS の SMB オープン上限に当たると disk I/O error / "
                        "unable to open database file / Errno 24 が同時多発する)",
                        h, warn_handles, rs.get("outstanding"),
                        snap.get("sqlite_cm"),
                        snap.get("engine_conns"), snap.get("open_files_top"),
                    )

                prev = snap
            except Exception:
                logger.exception("[RESOURCE] snapshot loop error")
            time.sleep(interval)

    threading.Thread(target=_loop, name="resource-monitor", daemon=True).start()
    return True


__all__ = [
    "start_resource_monitor",
    "snapshot",
    "recent",
    "log_io_error_chronology",
    "win_handle_count",
]
