# ============================================================
# File   : trading/push/push_stream/ownership.py
# Version: V1.0-PUSH-WS-OWNERSHIP
# ------------------------------------------------------------
# PUSH WebSocket の所有権を 1 プロセスに固定するための唯一の判定点。
#
# 背景 (2026-08-31 実測):
#   kabu Station の PUSH は 1 セッションしか張れない。2 本目が繋がると
#   サーバが古い方を切るため、両者が数秒おきに互いを蹴り合う。
#
#     10:42 push_receiver_runner(PID 52480) 起動 -> WS は 230 秒維持
#     10:46 main.py(PID 51468) 起動 = 2 本目の WS を張る
#     10:47 以降 632 回連続で uptime 2〜4 秒の切断 (40 分以上回復せず)
#     netstat: 51468 と 52480 が同時に :18080 へ Established
#              main.py 側は一時 5 本同時、TimeWait 約 85 本
#     main.py 側は total_received=0 のまま (配信は勝った方にしか来ない)
#
#   原因は core/startup/bootstrap/background._start_push_stream_runner() が
#   所有権チェック無しで WS と rotation を起動していたこと。
#
# 不変条件:
#   PUSH WebSocket を張ってよいのは role=push_receiver のプロセスだけ。
#   それ以外は WS も subscription rotation も起動しない。
#
#   main.py                        -> role=main             owner=False
#   main_database.py               -> role=main_database    owner=False
#   scripts/data_collectors_runner -> role=data_collectors  owner=False
#       (このプロセスは push_receiver を子として起動する側であって、
#        自分では WS を持たない)
#   scripts/push_receiver_runner   -> role=push_receiver    owner=True
#   scripts/summary_database_runner-> role=summary_database owner=False
#   その他                          -> role=unknown          owner=False
#
# 逃げ道:
#   PUSH_STREAM_FORCE_OWNER=1 で強制的に所有者にする (調査用)。
#   PUSH_STREAM_OWNER_ROLE=<role> で role を明示上書きできる。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

VERSION = "V1.0-PUSH-WS-OWNERSHIP"

ROLE_PUSH_RECEIVER = "push_receiver"
ROLE_MAIN = "main"
ROLE_MAIN_DATABASE = "main_database"
ROLE_DATA_COLLECTORS = "data_collectors"
ROLE_SUMMARY_DATABASE = "summary_database"
ROLE_RANKING_COLLECTOR = "ranking_collector"
ROLE_YAHOO_COMPLEMENT = "yahoo_complement"
ROLE_DB_PREPARE = "db_prepare"
ROLE_UNKNOWN = "unknown"

# argv に含まれていたら その role とみなす。判定順に意味がある
# (push_receiver_runner.py は data_collectors 系の名前も含まないので先頭で良い)。
_ROLE_MARKERS: tuple[tuple[str, str], ...] = (
    ("push_receiver_runner.py", ROLE_PUSH_RECEIVER),
    ("data_collectors_runner.py", ROLE_DATA_COLLECTORS),
    ("summary_database_runner.py", ROLE_SUMMARY_DATABASE),
    ("ranking_collector_runner.py", ROLE_RANKING_COLLECTOR),
    ("yahoo_complement_runner.py", ROLE_YAHOO_COMPLEMENT),
    ("db_prepare_runner.py", ROLE_DB_PREPARE),
    ("main_database.py", ROLE_MAIN_DATABASE),
    ("main.py", ROLE_MAIN),
)

# WS を張ってよい role。ここを増やすと多重接続に戻るので安易に増やさないこと。
_WS_OWNER_ROLES = frozenset({ROLE_PUSH_RECEIVER})

_banner_lock = threading.Lock()
_banner_logged = False


def _env_flag(name: str) -> bool:
    try:
        return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return False


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in (sys.argv or []))
    except Exception:
        return ""


def resolve_role() -> str:
    """このプロセスの role。argv 由来。env で明示上書きできる。"""
    override = str(os.getenv("PUSH_STREAM_OWNER_ROLE") or "").strip().lower()
    if override:
        return override

    argv = _argv_text()
    for marker, role in _ROLE_MARKERS:
        if marker in argv:
            return role
    return ROLE_UNKNOWN


def is_push_owner() -> tuple[bool, str, str]:
    """(owner, role, reason) を返す。"""
    role = resolve_role()
    if _env_flag("PUSH_STREAM_FORCE_OWNER"):
        return True, role, "forced_by_env:PUSH_STREAM_FORCE_OWNER"
    if role in _WS_OWNER_ROLES:
        return True, role, "designated_push_receiver"
    if role == ROLE_UNKNOWN:
        return False, role, "unknown_role_not_push_receiver"
    return False, role, "not_push_receiver_role:%s" % role


def ws_allowed() -> bool:
    owner, _role, _reason = is_push_owner()
    return owner


def _ppid() -> int:
    try:
        return int(os.getppid())
    except Exception:
        return -1


def owner_banner_fields() -> dict:
    owner, role, reason = is_push_owner()
    return {
        "role": role,
        "pid": os.getpid(),
        "ppid": _ppid(),
        "owner": owner,
        "reason": reason,
        "ws_allowed": owner,
        "argv": " ".join(str(x) for x in (sys.argv or [])[:3]),
    }


def log_owner_banner(force: bool = False) -> dict:
    """起動時に 1 回だけ所有権を 1 行で出す。"""
    global _banner_logged
    fields = owner_banner_fields()
    with _banner_lock:
        if _banner_logged and not force:
            return fields
        _banner_logged = True
    logger.warning(
        "[PUSH WS OWNER] role=%s pid=%s ppid=%s owner=%s reason=%s ws_allowed=%s "
        "connection_generation=%s ws_instance_id=%s argv=%s version=%s",
        fields["role"], fields["pid"], fields["ppid"], fields["owner"],
        fields["reason"], fields["ws_allowed"],
        current_connection_generation(), ws_instance_id(),
        fields["argv"], VERSION,
    )
    return fields


def warn_non_owner_call(entrypoint: str) -> None:
    """所有していないプロセスが WS 経路を呼んだら黙って落とさず必ず警告する。"""
    fields = owner_banner_fields()
    logger.warning(
        "[PUSH WS OWNER] BLOCKED entrypoint=%s role=%s pid=%s ppid=%s owner=False reason=%s "
        "-> WebSocket / rotation register は起動しない。"
        "kabu Station の PUSH は 1 セッションのみで、2 本張ると互いを切り合う。"
        "(調査目的で許可するなら PUSH_STREAM_FORCE_OWNER=1)",
        entrypoint, fields["role"], fields["pid"], fields["ppid"], fields["reason"],
    )


# ------------------------------------------------------------
# 観測用 (monitor / health から使う)
# ------------------------------------------------------------

def ws_instance_id() -> str:
    """現行 WebSocketApp と その sock の同一性。差し替わりを追える。"""
    try:
        from . import state

        with state._ws_state_lock:
            ws_app = getattr(state, "_ws_app", None)
        if ws_app is None:
            return "none"
        sock = getattr(ws_app, "sock", None)
        return "%x/sock=%s" % (id(ws_app), "none" if sock is None else ("%x" % id(sock)))
    except Exception:
        return "unknown"


def current_connection_generation():
    try:
        from .ws_callbacks import current_connection_generation as _gen

        return _gen()
    except Exception:
        return None


def active_ws_instances() -> int:
    """このプロセスが今持っている WebSocketApp の数 (0 or 1 が正常)。"""
    try:
        from . import state

        with state._ws_state_lock:
            return 1 if getattr(state, "_ws_app", None) is not None else 0
    except Exception:
        return -1


__all__ = [
    "VERSION",
    "ROLE_PUSH_RECEIVER",
    "resolve_role",
    "is_push_owner",
    "ws_allowed",
    "log_owner_banner",
    "owner_banner_fields",
    "warn_non_owner_call",
    "ws_instance_id",
    "current_connection_generation",
    "active_ws_instances",
]
