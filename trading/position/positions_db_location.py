# ============================================================
# trading/position/positions_db_location.py
# ------------------------------------------------------------
# positions.db を「どこに置くか」を決める唯一の場所。
#
# 2026-09-16: NAS (X:) 上の positions.db が SMB 劣化で開けなくなり
#   (11:20〜11:51 に 630 回 "unable to open database file"、17:26〜 "disk I/O error")、
#   建玉の記録・保護・決済側の DB 参照が全部落ちた。push / ranking DB と同じく
#   **ローカルに書いて NAS へ複製** する方式にする。
#
#   - 場中はローカル (%LOCALAPPDATA%\autostock\positions\positions.db) に書く
#   - 起動時、ローカルに無ければ NAS の positions.db をオンラインバックアップで
#     ローカルへ取り込む (当日の建玉履歴を引き継ぐ)
#   - 定期 (POSITIONS_DB_SYNC_INTERVAL_MIN、既定 30) と停止時に NAS へ複製
#   - POSITIONS_DB_ONEDRIVE_DIR が指定されていれば OneDrive にも複製 (コピーなので
#     OneDrive の同期クライアントとロック競合しない。稼働 DB は置かない)
#
# 環境変数:
#   POSITIONS_DB_LOCAL_STAGING     0 で従来どおり NAS を直接使う (既定 1)
#   POSITIONS_DB_LOCAL_DIR         ローカルの置き場所
#   POSITIONS_DB_SYNC_INTERVAL_MIN 定期複製の間隔 (0 で無効)
#   POSITIONS_DB_ONEDRIVE_DIR      OneDrive 側の複製先ディレクトリ (空なら %OneDrive%\autostock_backup\positions)
#   POSITIONS_DB_PATH              最優先。指定があればそれを使う
# ============================================================
from __future__ import annotations

import atexit
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

VERSION = "V1.0-POSITIONS-DB-LOCAL-STAGING"
_lock = threading.Lock()
_sync_thread_started = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def local_staging_enabled() -> bool:
    return _env_bool("POSITIONS_DB_LOCAL_STAGING", True)


def nas_positions_db_path() -> Path:
    from config.paths import get_path
    from nas_root import to_real_nas
    # 2026-10-24: config.paths はローカルのデータ root を指しうるので NAS 上へ読み替える
    return Path(to_real_nas(get_path("runtime_positions") / "positions.db"))


def local_positions_db_dir() -> Path:
    env = os.getenv("POSITIONS_DB_LOCAL_DIR", "").strip()
    if env:
        return Path(env)
    base = os.getenv("LOCALAPPDATA") or os.getenv("TEMP") or r"C:\Temp"
    return Path(base) / "autostock" / "positions"


def onedrive_positions_db_dir() -> Optional[Path]:
    env = os.getenv("POSITIONS_DB_ONEDRIVE_DIR", "").strip()
    if env:
        return Path(env)
    od = os.getenv("OneDrive") or os.getenv("OneDriveConsumer") or ""
    if od:
        return Path(od) / "autostock_backup" / "positions"
    return None


def positions_db_path() -> Path:
    """稼働中に開く positions.db のパス。"""
    env = os.getenv("POSITIONS_DB_PATH", "").strip()
    if env:
        return Path(env)
    if not local_staging_enabled():
        return nas_positions_db_path()
    d = local_positions_db_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("[POSITIONS DB] local dir mkdir failed %s", d)
    return d / "positions.db"


def _backup(src: Path, dst: Path, *, timeout_sec: float, label: str) -> dict:
    """sqlite3 オンラインバックアップで src -> dst (一時ファイル経由で置き換え)。
    NAS のハングで呼び出し側を固めないよう deadline 付きスレッドで行う。"""
    out = {"ok": False, "src": str(src), "dst": str(dst), "error": "", "bytes": 0}

    def _work():
        s = d = None
        tmp = dst.with_name(dst.name + ".tmp%d" % os.getpid())
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            for suf in ("", "-wal", "-shm"):
                q = Path(str(tmp) + suf)
                if q.exists():
                    q.unlink()
            s = sqlite3.connect(str(src), timeout=30.0)
            d = sqlite3.connect(str(tmp), timeout=30.0)
            s.backup(d, pages=2000)
            d.commit(); d.close(); d = None
            size = tmp.stat().st_size
            if size <= 0:
                raise OSError("backup produced an empty file")
            os.replace(str(tmp), str(dst))
            out["ok"] = True
            out["bytes"] = size
        except Exception as e:
            out["error"] = "%s: %s" % (type(e).__name__, e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
        finally:
            for c in (d, s):
                try:
                    if c is not None:
                        c.close()
                except Exception:
                    pass

    try:
        from utils.nas_guard import run_with_deadline
        guard = run_with_deadline(_work, timeout_sec=timeout_sec, label=label, thread_name=f"positions_db_{label}")
        if guard.get("timed_out"):
            out["error"] = out["error"] or "timed_out"
    except Exception:
        # nas_guard が無い環境では同期実行 (テスト用)
        _work()
    return out


def bootstrap_local_from_nas(*, timeout_sec: float = 60.0) -> dict:
    """ローカルに無ければ NAS からコピーして引き継ぐ。ローカルがあれば何もしない。"""
    local = positions_db_path()
    if not local_staging_enabled() or local.exists():
        return {"ok": True, "skipped": True, "local": str(local)}
    try:
        nas = nas_positions_db_path()
    except Exception as e:
        return {"ok": False, "error": f"nas path: {e}"}
    if not nas.exists():
        return {"ok": True, "skipped": True, "reason": "nas missing", "local": str(local)}
    out = _backup(nas, local, timeout_sec=timeout_sec, label="bootstrap_from_nas")
    if out["ok"]:
        logger.warning("[POSITIONS DB] bootstrapped local from NAS %s -> %s bytes=%s", nas, local, out["bytes"])
    else:
        logger.error("[POSITIONS DB] bootstrap from NAS failed %s (ローカルは新規作成される)", out["error"])
    return out


def sync_to_replicas(*, reason: str = "", timeout_sec: float = 120.0) -> dict:
    """ローカルの positions.db を NAS と OneDrive へ複製する。失敗しても例外を投げない。"""
    results = {"reason": reason, "nas": None, "onedrive": None}
    if not local_staging_enabled():
        return results
    local = positions_db_path()
    if not local.exists():
        return results
    with _lock:
        try:
            results["nas"] = _backup(local, nas_positions_db_path(), timeout_sec=timeout_sec, label="sync_nas")
        except Exception as e:
            results["nas"] = {"ok": False, "error": str(e)}
        od = onedrive_positions_db_dir()
        if od is not None:
            try:
                results["onedrive"] = _backup(local, od / "positions.db", timeout_sec=timeout_sec, label="sync_onedrive")
            except Exception as e:
                results["onedrive"] = {"ok": False, "error": str(e)}
    for k in ("nas", "onedrive"):
        r = results.get(k)
        if isinstance(r, dict):
            if r.get("ok"):
                logger.info("[POSITIONS DB] replicated to %s reason=%s bytes=%s dst=%s", k, reason, r.get("bytes"), r.get("dst"))
            else:
                logger.warning("[POSITIONS DB] replicate to %s failed reason=%s err=%s", k, reason, r.get("error"))
    return results


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv and "main_database.py" not in argv
    except Exception:
        return False


def start_background_sync() -> bool:
    """main.py だけが定期複製と停止時複製を担当する (複製が 2 本走ると NAS 側が壊れる)。"""
    global _sync_thread_started
    if _sync_thread_started or not local_staging_enabled() or not _is_main_py():
        return False
    interval_min = _env_float("POSITIONS_DB_SYNC_INTERVAL_MIN", 30.0)
    _sync_thread_started = True

    def _loop():
        while True:
            time.sleep(max(60.0, interval_min * 60.0))
            try:
                sync_to_replicas(reason="periodic")
            except Exception:
                logger.exception("[POSITIONS DB] periodic sync failed")

    if interval_min > 0:
        threading.Thread(target=_loop, daemon=True, name="positions_db_sync").start()
    atexit.register(lambda: sync_to_replicas(reason="shutdown", timeout_sec=60.0))
    logger.warning("[POSITIONS DB] local staging active path=%s interval_min=%s onedrive=%s version=%s",
                   positions_db_path(), interval_min, onedrive_positions_db_dir(), VERSION)
    return True
