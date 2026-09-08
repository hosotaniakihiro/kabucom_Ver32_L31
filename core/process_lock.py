# ============================================================
# File   : core/process_lock.py
# Version: Ver1.0-AUTOSTOCK-SINGLE-INSTANCE-LOCK
# ------------------------------------------------------------
# 【なぜ必要か】
#   main_database.py / data_collectors_runner.py を二重に起動しても、
#   誰も止めていなかった。
#
#   2026-09-03 12:2x の実測プロセスツリー:
#       tree A 10:47:06  main_database 12608
#                          push_receiver 46696   <- WS 所有権ロック保持 (稼働中)
#                          yahoo 48864 / summary 50464 / ranking 52616
#       tree B 11:08:56  main_database 19972
#                          push_receiver 57420   <- ロックを取れず WS 起動できない
#                          yahoo 56152 / summary 52300 / ranking 49160
#
#   kabu Station の PUSH は 1 セッション 1 接続なので、
#   trading/push/push_stream/ws_owner.py の single-owner lock が
#   2 本目を正しく拒否する。つまり「WebSocket が起動できない」のではなく
#   「起動してはいけない 2 本目を起動しようとしている」のが実態だった。
#
#   ws_owner の lock は WebSocket だけを守る。プロセスツリーごと
#   二重に走ること自体は防げないので、その手前で止める。
#
# 【設計】
#   - OS のファイルロック (msvcrt / fcntl) を単一の真実とする。
#     ロックはプロセス終了時に OS が必ず解放するので、
#     異常終了しても次の起動が確実に取得できる。
#   - meta json は「誰が持っているか」を報告するためだけに使う。
#   - PID は OS が再利用するので、pid 単独では本人確認にならない。
#     pid + create_time + argv(role) + project_root + heartbeat の
#     5 点で照合する。
#   - 無関係な python プロセスには一切触らない。kill もしない。
#     このモジュールは「自分が起動をやめる」判断材料しか作らない。
#
# 【使い方】
#   from core.process_lock import acquire_single_instance
#
#   ok, holder = acquire_single_instance("main_database")
#   if not ok:
#       logger.error("既に起動している: %s", holder)
#       return 0
#
# 【ENV】
#   AUTOSTOCK_SINGLE_INSTANCE_LOCK=0    無効化 (従来どおり多重起動できる)
#   AUTOSTOCK_SINGLE_INSTANCE_WAIT_SEC  先行プロセスの終了待ち秒数 (既定 0)
#   AUTOSTOCK_SINGLE_INSTANCE_DIR       ロック置き場 (既定 %TEMP%)
# ============================================================

from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import tempfile
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

VERSION = "Ver1.0-AUTOSTOCK-SINGLE-INSTANCE-LOCK"

_HEARTBEAT_INTERVAL_SEC = 15.0

_LOCK = threading.RLock()
# role -> 保持中のファイルハンドル
_handles: Dict[str, Any] = {}
_heartbeat_stop = threading.Event()
_heartbeat_thread: Optional[threading.Thread] = None
_heartbeat_meta: Dict[str, Dict[str, Any]] = {}


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _lock_dir() -> str:
    d = os.environ.get("AUTOSTOCK_SINGLE_INSTANCE_DIR", "").strip()
    return d or tempfile.gettempdir()


def lock_path(role: str) -> str:
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(role))
    return os.path.join(_lock_dir(), "autostock_single_instance_%s.lock" % safe)


def _meta_path(role: str) -> str:
    return lock_path(role) + ".owner.json"


def _project_root() -> str:
    """このモジュールから見たプロジェクトルート (core/ の親)。"""
    try:
        return os.path.normcase(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    except Exception:
        return ""


def _proc_create_time(pid: Any) -> Optional[float]:
    try:
        import psutil  # type: ignore

        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


def _proc_cmdline(pid: Any) -> str:
    try:
        import psutil  # type: ignore

        return " ".join(str(x) for x in (psutil.Process(int(pid)).cmdline() or []))
    except Exception:
        return ""


def _pid_alive(pid: Any) -> Optional[bool]:
    try:
        pid = int(pid)
    except Exception:
        return None
    if pid <= 0:
        return None
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    if os.name == "nt":
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _self_info(role: str) -> Dict[str, Any]:
    now = time.time()
    pid = os.getpid()
    return {
        "role": str(role),
        "pid": pid,
        "parent_pid": (os.getppid() if hasattr(os, "getppid") else -1),
        "create_time": _proc_create_time(pid),
        "argv": " ".join(str(a) for a in sys.argv[:3]),
        "project_root": _project_root(),
        "acquired_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "acquired_ts": now,
        "heartbeat_at": now,
        "heartbeat_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": VERSION,
    }


def read_owner_info(role: str) -> Dict[str, Any]:
    try:
        with open(_meta_path(role), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def verify_holder(role: str, holder: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """meta に書かれたプロセスが本当に「その役割で生きている」かを判定する。

    戻り値: (判定, 補足情報)
        "alive"        本人が生きている                -> 起動をやめるべき
        "pid_reused"   pid は生きているが別プロセス     -> meta が古い
        "dead"         pid が居ない                    -> meta が古い
        "unknown"      判定材料が足りない

    pid だけでは PID 再利用と区別できないので create_time で照合する。
    さらに argv / project_root も見て「同じ役割・同じソースツリー」かを確かめる。
    """
    detail: Dict[str, Any] = {}
    pid = holder.get("pid")

    alive = _pid_alive(pid)
    detail["pid_alive"] = alive
    if alive is False:
        return "dead", detail
    if alive is None:
        return "unknown", detail

    meta_ct = holder.get("create_time")
    live_ct = _proc_create_time(pid)
    detail["meta_create_time"] = meta_ct
    detail["live_create_time"] = live_ct
    if meta_ct is not None and live_ct is not None:
        # 同じプロセスなら create_time は完全一致する。
        if abs(float(live_ct) - float(meta_ct)) > 1.0:
            return "pid_reused", detail

    # 本人確認の主軸は create_time (上で照合済み)。
    # 補助として「meta に記録した argv」と実プロセスの cmdline を突き合わせる。
    # role 名そのものを cmdline に求めると、role 名が argv に現れない
    # 呼び出し方 (テスト、-m 実行、ラッパー経由) で生きている本人を
    # pid_reused と誤判定してしまうので使わない。
    cmdline = _proc_cmdline(pid).replace("\\", "/").lower()
    detail["live_cmdline_tail"] = cmdline[-120:]
    meta_argv = str(holder.get("argv") or "").replace("\\", "/").lower()
    if cmdline and meta_argv:
        # meta の argv は sys.argv[:3] を空白連結したもの。
        # 実 cmdline には python.exe が先頭に付くので部分一致で見る。
        head = meta_argv.split(" ")[0]
        if head and head not in cmdline:
            # 同じ pid が全く別のプログラムで走っている = meta が古い
            detail["meta_argv_head"] = head
            return "pid_reused", detail

    hb = holder.get("heartbeat_at")
    if hb is not None:
        try:
            detail["heartbeat_age_sec"] = round(time.time() - float(hb), 1)
        except Exception:
            pass

    return "alive", detail


def _heartbeat_loop() -> None:
    while not _heartbeat_stop.wait(_HEARTBEAT_INTERVAL_SEC):
        with _LOCK:
            roles = list(_handles.keys())
        for role in roles:
            try:
                info = dict(_heartbeat_meta.get(role) or {})
                if not info:
                    continue
                now = time.time()
                info["heartbeat_at"] = now
                info["heartbeat_str"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _heartbeat_meta[role] = info
                with open(_meta_path(role), "w", encoding="utf-8") as fh:
                    json.dump(info, fh, ensure_ascii=False, indent=2)
            except Exception:
                logger.debug("[SINGLE INSTANCE] heartbeat write failed role=%s", role, exc_info=True)


def _ensure_heartbeat() -> None:
    global _heartbeat_thread
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        return
    _heartbeat_stop.clear()
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop, name="autostock-single-instance-heartbeat", daemon=True
    )
    _heartbeat_thread.start()


def _try_os_lock(fh: Any) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    except Exception:
        logger.debug("[SINGLE INSTANCE] os lock attempt failed", exc_info=True)
        return False


def _release_os_lock(fh: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
    except Exception:
        logger.debug("[SINGLE INSTANCE] os unlock failed", exc_info=True)


def _acquire_once(role: str) -> Tuple[bool, Dict[str, Any]]:
    with _LOCK:
        if role in _handles:
            return True, {}

        path = lock_path(role)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            pass

        try:
            fh = open(path, "a+", encoding="utf-8")
        except Exception:
            logger.exception(
                "[SINGLE INSTANCE] lock file open failed role=%s path=%s -> 多重起動チェックなしで続行する",
                role, path,
            )
            return True, {}

        if not _try_os_lock(fh):
            try:
                fh.close()
            except Exception:
                pass
            return False, read_owner_info(role)

        _handles[role] = fh

        info = _self_info(role)
        _heartbeat_meta[role] = info
        try:
            with open(_meta_path(role), "w", encoding="utf-8") as meta:
                json.dump(info, meta, ensure_ascii=False, indent=2)
        except Exception:
            logger.debug("[SINGLE INSTANCE] owner meta write failed role=%s", role, exc_info=True)

        atexit.register(release_single_instance, role)
        _ensure_heartbeat()
        return True, {}


def acquire_single_instance(role: str) -> Tuple[bool, Dict[str, Any]]:
    """role について、このマシンで自分が唯一のインスタンスかを確定する。

    戻り値: (取得できたか, 取得できなかったときのホルダ情報)

    ホルダ情報には verify_holder() の判定を "holder_state" として入れる。
    "alive" 以外 (dead / pid_reused) の場合、meta は古いだけで
    OS ロックは別の誰かが握っている、という矛盾した状態なので
    そのまま報告する。判断は呼び出し側に任せる。
    """
    if not _env_bool("AUTOSTOCK_SINGLE_INSTANCE_LOCK", True):
        logger.warning(
            "[SINGLE INSTANCE] disabled by AUTOSTOCK_SINGLE_INSTANCE_LOCK=0 role=%s pid=%s "
            "(多重起動を検出できない)",
            role, os.getpid(),
        )
        return True, {}

    ok, holder = _acquire_once(role)
    if ok:
        logger.warning(
            "[SINGLE INSTANCE] acquired role=%s pid=%s path=%s version=%s",
            role, os.getpid(), lock_path(role), VERSION,
        )
        return True, {}

    wait_sec = _env_float("AUTOSTOCK_SINGLE_INSTANCE_WAIT_SEC", 0.0)
    if wait_sec > 0:
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            time.sleep(1.0)
            ok, holder = _acquire_once(role)
            if ok:
                logger.warning(
                    "[SINGLE INSTANCE] acquired role=%s pid=%s after waiting %.0fs",
                    role, os.getpid(), wait_sec,
                )
                return True, {}

    state, detail = verify_holder(role, holder)
    holder = dict(holder)
    holder["holder_state"] = state
    holder.update(detail)
    return False, holder


def release_single_instance(role: str) -> None:
    with _LOCK:
        fh = _handles.pop(role, None)
        _heartbeat_meta.pop(role, None)
        if not _handles:
            try:
                _heartbeat_stop.set()
            except Exception:
                pass
    if fh is None:
        return
    try:
        _release_os_lock(fh)
    finally:
        try:
            fh.close()
        except Exception:
            pass
    logger.warning("[SINGLE INSTANCE] released role=%s pid=%s", role, os.getpid())


def holds_single_instance(role: str) -> bool:
    with _LOCK:
        return role in _handles


def describe_holder(holder: Dict[str, Any]) -> str:
    """ログ 1 行用の整形。"""
    keys = (
        "holder_state", "pid", "parent_pid", "acquired_at", "heartbeat_str",
        "heartbeat_age_sec", "pid_alive", "project_root", "argv",
    )
    try:
        return " ".join("%s=%s" % (k, holder.get(k)) for k in keys if k in holder)
    except Exception:
        return repr(holder)


__all__ = [
    "VERSION",
    "acquire_single_instance",
    "release_single_instance",
    "holds_single_instance",
    "verify_holder",
    "read_owner_info",
    "describe_holder",
    "lock_path",
]
