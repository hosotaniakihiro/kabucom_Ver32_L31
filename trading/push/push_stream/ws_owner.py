# ============================================================
# File   : trading/push/push_stream/ws_owner.py
# Version: Ver1.0-PUSH-WS-SINGLE-OWNER-LOCK
# ------------------------------------------------------------
# kabu Station の PUSH WebSocket は 1 セッション 1 接続しか許容しない。
# 2 本張ると後から張った方が生き残り、先に張っていた方が数秒で切られる。
# 実測 (2026-08-31):
#   main.py が 11:48:00 に memory-only fallback WS を張った直後から
#   push_receiver_runner.py 側の WS が 3〜11 秒で切れ続け、
#   connection_generation が 850 まで増えた (fast_death consecutive=316)。
#
# 同一プロセス内の二重起動は state._ws_thread で防げるが、
# プロセスを跨いだ二重起動は防げなかったので、
# ここで OS レベルのファイルロックによる single-instance lock を持つ。
#
# 使い方:
#   ok, holder = acquire_push_ws_owner("push_receiver")
#   if not ok:  -> MULTI_WS_DETECTED。WebSocket も rotation も起動しない。
#
# ロックはプロセス終了時に OS が自動解放するので、
# 異常終了しても次の起動が確実に取得できる。
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

VERSION = "Ver2.0-PUSH-WS-SINGLE-OWNER-LOCK-STALE-TAKEOVER"

_LOCK = threading.RLock()
_lock_handle: Any = None
_owner_role: Optional[str] = None
_owner_acquired_at: Optional[float] = None

# 同一プロセス内で同時に走っている WebSocket run_forever の本数。
_instance_lock = threading.RLock()
_instance_seq = 0
_instance_active = 0


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
        return True
    if s in {"0", "false", "no", "n", "off", "disable", "disabled", ""}:
        return False
    return bool(default)


def lock_path() -> str:
    return os.environ.get("KABU_PUSH_WS_OWNER_LOCK_PATH") or os.path.join(
        tempfile.gettempdir(), "autostock_kabustation_push_ws_owner.lock"
    )


def _meta_path() -> str:
    return lock_path() + ".owner.json"


def _parent_pid() -> int:
    try:
        return int(os.getppid())
    except Exception:
        return -1


def _proc_create_time(pid: Any) -> Optional[float]:
    """pid の起動時刻 (epoch)。取得できなければ None。

    pid は OS が再利用するので、pid だけでは「同じプロセスか」を判定できない。
    起動時刻とセットで見る。
    """
    try:
        import psutil  # type: ignore

        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


def _self_info(role: str) -> Dict[str, Any]:
    now = time.time()
    return {
        "pid": os.getpid(),
        "parent_pid": _parent_pid(),
        "ws_owner_role": role,
        "argv": " ".join(str(a) for a in sys.argv[:3]),
        "acquired_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "acquired_ts": now,
        # heartbeat は保持中スレッドが更新する。止まった = owner が固まっている。
        "heartbeat_at": now,
        "heartbeat_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "create_time": _proc_create_time(os.getpid()),
    }


def read_owner_info() -> Dict[str, Any]:
    """ロックを保持しているプロセスの情報 (取得できなければ空 dict)。"""
    try:
        with open(_meta_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pid_alive(pid: Any) -> Optional[bool]:
    """pid が生きているか。判定できないときは None。"""
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


def push_start_trace(step: str, **fields: Any) -> None:
    """PUSH 起動経路の必須トレース。silent return を禁止するための共通出力。

    どの段階まで到達したかをログだけで追えるようにする
    (2026-09-03: 前日の push_receiver が WS 所有権ロックを握ったまま生存し、
     当日の push_receiver が websocket を一度も起動しないまま
     heartbeat だけ出し続ける状態になった)。
    """
    try:
        parts = ["%s=%s" % (k, v) for k, v in fields.items()]
        logger.warning("[PUSH START TRACE] %s %s", step, " ".join(parts))
    except Exception:
        pass


def _fmt_holder(holder: Dict[str, Any]) -> str:
    try:
        keys = ("pid", "parent_pid", "ws_owner_role", "acquired_at", "heartbeat_str", "pid_alive")
        return " ".join("%s=%s" % (k, holder.get(k)) for k in keys)
    except Exception:
        return repr(holder)


def _holder_heartbeat_age(holder: Dict[str, Any]) -> Optional[float]:
    """ホルダの heartbeat からの経過秒。判定できなければ None。"""
    hb = holder.get("heartbeat_at")
    if hb is None:
        # Ver1.0 が書いたメタには heartbeat が無い。acquired_at を代わりに使う。
        raw = holder.get("acquired_ts")
        if raw is None:
            raw_str = holder.get("acquired_at")
            if not raw_str:
                return None
            try:
                raw = time.mktime(time.strptime(str(raw_str), "%Y-%m-%d %H:%M:%S"))
            except Exception:
                return None
        hb = raw
    try:
        return max(0.0, time.time() - float(hb))
    except Exception:
        return None


def _stale_sec() -> float:
    try:
        return max(30.0, float(os.environ.get("KABU_PUSH_WS_OWNER_STALE_SEC", "120")))
    except Exception:
        return 120.0


def _wait_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("KABU_PUSH_WS_OWNER_WAIT_SEC", "20")))
    except Exception:
        return 20.0


_EVICT_ARGV_MARKERS = ("push_receiver_runner.py", "main_database.py", "data_collectors_runner.py")


def _holder_is_stale(holder: Dict[str, Any]) -> Tuple[bool, str]:
    """ホルダが「生きているが仕事をしていない」= 引き継いでよい状態か。"""
    pid = holder.get("pid")
    alive = _pid_alive(pid)
    if alive is False:
        return True, "holder_pid_dead"

    age = _holder_heartbeat_age(holder)
    if age is None:
        return False, "holder_heartbeat_unknown"

    limit = _stale_sec()
    if age > limit:
        return True, "holder_heartbeat_stale age=%.0fs limit=%.0fs" % (age, limit)
    return False, "holder_alive age=%.0fs limit=%.0fs" % (age, limit)


def _evict_stale_holder(holder: Dict[str, Any], reason: str) -> bool:
    """固まった旧 owner を終了させて所有権を空ける。

    kabu Station の PUSH は 1 セッション 1 接続なので、旧 owner が生きている限り
    新 owner は websocket を張れない。旧 owner が heartbeat を止めている場合だけ、
    厳格な条件を満たすときに限って終了させる。

    安全条件 (すべて満たすときだけ terminate する):
      1. KABU_PUSH_WS_OWNER_EVICT_STALE != 0
      2. pid が自分・自分の親ではない
      3. 実プロセスの cmdline が本プロジェクトの PUSH 系プロセスである
      4. 旧 owner の起動時刻が自プロセスより古い (= 前の世代である)
    """
    if not _env_bool("KABU_PUSH_WS_OWNER_EVICT_STALE", True):
        push_start_trace("EVICT_SKIPPED", reason="disabled_by_env", holder=_fmt_holder(holder))
        return False

    try:
        pid = int(holder.get("pid"))
    except Exception:
        push_start_trace("EVICT_SKIPPED", reason="holder_pid_unparsable", holder=_fmt_holder(holder))
        return False

    me = os.getpid()
    if pid in (me, _parent_pid()) or pid <= 0:
        push_start_trace("EVICT_SKIPPED", reason="holder_is_self_or_parent", holder_pid=pid, self_pid=me)
        return False

    try:
        import psutil  # type: ignore
    except Exception:
        push_start_trace("EVICT_SKIPPED", reason="psutil_unavailable", holder_pid=pid)
        return False

    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(str(x) for x in (proc.cmdline() or [])).replace("\\", "/").lower()
        holder_create = float(proc.create_time())
    except Exception:
        push_start_trace("EVICT_SKIPPED", reason="holder_process_unreadable", holder_pid=pid)
        return False

    if not any(m.lower() in cmdline for m in _EVICT_ARGV_MARKERS):
        push_start_trace(
            "EVICT_SKIPPED", reason="holder_cmdline_not_project_push", holder_pid=pid, cmdline=cmdline[-120:]
        )
        return False

    my_create = _proc_create_time(me)
    if my_create is not None and holder_create >= my_create:
        push_start_trace(
            "EVICT_SKIPPED",
            reason="holder_not_older_generation",
            holder_pid=pid,
            holder_create=holder_create,
            self_create=my_create,
        )
        return False

    push_start_trace(
        "EVICT_START",
        holder_pid=pid,
        stale_reason=reason,
        holder_age_sec="%.0f" % (time.time() - holder_create),
        cmdline=cmdline[-120:],
    )
    try:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
            proc.wait(timeout=10)
    except Exception:
        logger.exception("[push_ws_owner] stale holder terminate failed pid=%s", pid)
        push_start_trace("EVICT_FAILED", holder_pid=pid)
        return False

    push_start_trace("EVICT_DONE", holder_pid=pid, stale_reason=reason)
    logger.warning(
        "[push_ws_owner] stale PUSH WS owner evicted pid=%s reason=%s "
        "(旧世代の push_receiver が所有権を握ったまま受信を止めていた)",
        pid, reason,
    )
    return True


# ------------------------------------------------------------
# 所有中の heartbeat
# ------------------------------------------------------------
_heartbeat_thread: Optional[threading.Thread] = None
_heartbeat_stop = threading.Event()


def _heartbeat_loop(info: Dict[str, Any]) -> None:
    interval = 15.0
    while not _heartbeat_stop.wait(interval):
        with _LOCK:
            if _lock_handle is None:
                return
        try:
            now = time.time()
            info = dict(info)
            info["heartbeat_at"] = now
            info["heartbeat_str"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(_meta_path(), "w", encoding="utf-8") as meta:
                json.dump(info, meta, ensure_ascii=False, indent=2)
        except Exception:
            logger.debug("[push_ws_owner] heartbeat write failed", exc_info=True)


def _start_heartbeat(info: Dict[str, Any]) -> None:
    global _heartbeat_thread
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        return
    _heartbeat_stop.clear()
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop, args=(info,), name="push-ws-owner-heartbeat", daemon=True
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
        logger.debug("[push_ws_owner] os lock attempt failed", exc_info=True)
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
        logger.debug("[push_ws_owner] os unlock failed", exc_info=True)


def _acquire_once(role: str) -> Tuple[bool, Dict[str, Any]]:
    """ロックを 1 回だけ試す。(取得できたか, 取れなかったときのホルダ情報)"""
    global _lock_handle, _owner_role, _owner_acquired_at

    with _LOCK:
        if _lock_handle is not None:
            return True, {}

        path = lock_path()
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        except Exception:
            pass

        fh = None
        try:
            fh = open(path, "a+", encoding="utf-8")
        except Exception:
            logger.exception(
                "[push_ws_owner] lock file open failed path=%s -> 所有権チェックなしで続行する", path
            )
            return True, {}

        if not _try_os_lock(fh):
            try:
                fh.close()
            except Exception:
                pass
            holder = read_owner_info()
            holder["pid_alive"] = _pid_alive(holder.get("pid"))
            return False, holder

        _lock_handle = fh
        _owner_role = str(role)
        _owner_acquired_at = time.time()

        info = _self_info(role)
        try:
            with open(_meta_path(), "w", encoding="utf-8") as meta:
                json.dump(info, meta, ensure_ascii=False, indent=2)
        except Exception:
            logger.debug("[push_ws_owner] owner meta write failed", exc_info=True)

        try:
            fh.seek(1)
            fh.truncate(1)
            fh.write(json.dumps(info, ensure_ascii=False) + "\n")
            fh.flush()
        except Exception:
            logger.debug("[push_ws_owner] lock file annotate failed", exc_info=True)

        logger.warning(
            "[push_ws_owner] PUSH WebSocket owner acquired role=%s pid=%s parent_pid=%s path=%s version=%s",
            role,
            info["pid"],
            info["parent_pid"],
            path,
            VERSION,
        )
        atexit.register(release_push_ws_owner)
        _start_heartbeat(info)
        return True, {}


def acquire_push_ws_owner(role: str) -> Tuple[bool, Dict[str, Any]]:
    """PUSH WebSocket の所有権を取る。

    戻り値: (取得できたか, 取得できなかったときの現ホルダ情報)

    Ver2.0 (2026-09-03):
      Ver1.0 は「1 回試して駄目なら永久に諦める」だった。
      前日の push_receiver_runner.py (pid 34060) が 16 時間生き残ったまま
      受信を止め、ロックだけ握り続けたため、当日の push_receiver は
      websocket を一度も起動しないまま heartbeat だけ出す
      デッド状態になった (PUSH DB の最終時刻が 07:52 で停止)。

      対策:
        1. すぐに取れない場合は KABU_PUSH_WS_OWNER_WAIT_SEC 秒まで待って再試行する
           (正常な世代交代では旧プロセスの終了待ちで足りる)。
        2. それでも取れず、ホルダの heartbeat が
           KABU_PUSH_WS_OWNER_STALE_SEC 秒以上止まっている場合だけ、
           旧世代のプロセスを終了させて引き継ぐ。
        3. どの経路を通ったかを [PUSH START TRACE] に必ず残す。

    KABU_PUSH_WS_OWNER_LOCK=0 で無効化できるが、
    その場合は多重 WebSocket を防げないので運用では 1 のままにする。
    """
    if not _env_bool("KABU_PUSH_WS_OWNER_LOCK", True):
        logger.warning(
            "[push_ws_owner] disabled by KABU_PUSH_WS_OWNER_LOCK=0 role=%s pid=%s "
            "(多重 WebSocket を検出できない)",
            role,
            os.getpid(),
        )
        return True, {}

    ok, holder = _acquire_once(role)
    if ok:
        push_start_trace("OWNER_LOCK_ACQUIRED", role=role, pid=os.getpid(), wait_sec=0)
        return True, {}

    wait_sec = _wait_sec()
    push_start_trace(
        "OWNER_LOCK_BUSY",
        role=role,
        pid=os.getpid(),
        wait_sec="%.0f" % wait_sec,
        holder=_fmt_holder(holder),
    )

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(2.0)
        ok, holder = _acquire_once(role)
        if ok:
            push_start_trace(
                "OWNER_LOCK_ACQUIRED", role=role, pid=os.getpid(), wait_sec="%.0f" % wait_sec, after_wait=1
            )
            return True, {}

    stale, reason = _holder_is_stale(holder)
    push_start_trace(
        "OWNER_LOCK_STALE_CHECK", role=role, pid=os.getpid(), stale=stale, reason=reason,
        holder=_fmt_holder(holder),
    )
    if not stale:
        return False, holder

    if not _evict_stale_holder(holder, reason):
        return False, holder

    # 退去後は OS がロックを解放するまで少し待つ。
    for _ in range(10):
        ok, holder2 = _acquire_once(role)
        if ok:
            push_start_trace("OWNER_LOCK_ACQUIRED", role=role, pid=os.getpid(), after_evict=1)
            return True, {}
        holder = holder2
        time.sleep(1.0)

    push_start_trace("OWNER_LOCK_FAILED_AFTER_EVICT", role=role, pid=os.getpid(), holder=_fmt_holder(holder))
    return False, holder


def release_push_ws_owner() -> None:
    global _lock_handle, _owner_role, _owner_acquired_at

    with _LOCK:
        fh = _lock_handle
        if fh is None:
            return
        _lock_handle = None
        try:
            _heartbeat_stop.set()
        except Exception:
            pass
        role = _owner_role
        _owner_role = None
        _owner_acquired_at = None

    try:
        _release_os_lock(fh)
    finally:
        try:
            fh.close()
        except Exception:
            pass
    logger.warning("[push_ws_owner] PUSH WebSocket owner released role=%s pid=%s", role, os.getpid())


def is_push_ws_owner() -> bool:
    """このプロセスが PUSH WebSocket の所有者か。"""
    if not _env_bool("KABU_PUSH_WS_OWNER_LOCK", True):
        return True
    with _LOCK:
        return _lock_handle is not None


def owner_role() -> Optional[str]:
    with _LOCK:
        return _owner_role


def owned_elsewhere() -> Tuple[bool, Dict[str, Any]]:
    """別プロセスが所有権を持っているか (自分が所有者なら False)。

    誰も所有していない場合も False を返す。
    ロックを取れないときだけ True。
    """
    if not _env_bool("KABU_PUSH_WS_OWNER_LOCK", True):
        return False, {}
    with _LOCK:
        if _lock_handle is not None:
            return False, {}

    path = lock_path()
    fh = None
    try:
        fh = open(path, "a+", encoding="utf-8")
    except Exception:
        return False, {}

    try:
        if _try_os_lock(fh):
            _release_os_lock(fh)
            return False, {}
        holder = read_owner_info()
        holder["pid_alive"] = _pid_alive(holder.get("pid"))
        return True, holder
    finally:
        try:
            fh.close()
        except Exception:
            pass


def registration_allowed(reason: str = "") -> Tuple[bool, Dict[str, Any]]:
    """kabu Station の register/unregister を撃ってよいか。

    register リストはセッション全体の共有資源で、
    PUSH WebSocket を持っているプロセスがその内容に責任を持つ。
    所有していない側が unregister/all を撃つと、
    所有側の購読が消えて PUSH が止まり、再接続ループの引き金になる。

    - 自分が所有者              -> 許可
    - 誰も所有していない        -> 許可 (単体ツール・バックテスト等)
    - 別プロセスが所有している  -> 拒否
    """
    if not _env_bool("KABU_REGISTER_REQUIRE_WS_OWNER", True):
        return True, {}
    if is_push_ws_owner():
        return True, {}
    elsewhere, holder = owned_elsewhere()
    if not elsewhere:
        return True, {}
    return False, holder


# ------------------------------------------------------------
# 同一プロセス内の WebSocket インスタンス数
# ------------------------------------------------------------

def register_ws_instance() -> int:
    """run_forever に入る直前に呼ぶ。instance_id を返す。"""
    global _instance_seq, _instance_active
    with _instance_lock:
        _instance_seq += 1
        _instance_active += 1
        return _instance_seq


def unregister_ws_instance() -> None:
    global _instance_active
    with _instance_lock:
        _instance_active = max(0, _instance_active - 1)


def active_ws_instance_count() -> int:
    with _instance_lock:
        return _instance_active


def ownership_snapshot(role: Optional[str] = None) -> Dict[str, Any]:
    """起動時ログ・監視用のスナップショット。"""
    try:
        from .ws_callbacks import current_connection_generation

        generation = current_connection_generation()
    except Exception:
        generation = -1

    elsewhere, holder = owned_elsewhere()
    return {
        "process_id": os.getpid(),
        "parent_pid": _parent_pid(),
        "ws_owner_role": role or owner_role(),
        "is_ws_owner": is_push_ws_owner(),
        "connection_generation": generation,
        "active_ws_instance_count": active_ws_instance_count(),
        "owned_elsewhere": bool(elsewhere),
        "other_owner": holder,
        "lock_path": lock_path(),
    }


__all__ = [
    "VERSION",
    "acquire_push_ws_owner",
    "registration_allowed",
    "release_push_ws_owner",
    "is_push_ws_owner",
    "owner_role",
    "owned_elsewhere",
    "read_owner_info",
    "register_ws_instance",
    "unregister_ws_instance",
    "active_ws_instance_count",
    "ownership_snapshot",
    "lock_path",
]
