# ============================================================
# File   : trading/push/push_relay_server.py
# 2026-10-03: 2 つ目の kabu 口座 (別 PC) の PUSH を受け取る中継の受信口
# ------------------------------------------------------------
# kabu の PUSH 登録は 1 口座 50 銘柄まで (2027/3 まで)。別 PC で 2 つ目の口座の
# kabu Station を動かし、ops/push_relay/relay_agent.py がその PUSH を LAN 越しに転送する。
# この受信口は push_receiver プロセスの中で動き、届いた kabu PUSH の JSON を
# 自分の websocket から届いたものと同じ on_message に流す (DB 保存・5 秒足・最新値すべて共通)。
#
# 銘柄の割り振り (rotation_core から split_targets_for_relay を呼ぶ):
#   登録対象を最大 100 銘柄まで解決し、先頭 50 (保有中などの保護銘柄が先) を自分の口座、
#   51〜100 番目を中継に回す。中継がつながっていなければ従来どおり 50 銘柄だけ。
#
# 中継とのやりとり (改行区切りの JSON、TCP):
#   中継 -> ここ : {"type":"hello","secret":"...","name":"..."} / {"type":"push","data":{kabu PUSH}} / {"type":"ping"}
#   ここ -> 中継 : {"type":"ok"} / {"type":"register","symbols":[...]}
#
# 設定 (settings.ini [push_relay]):  enabled = 1 / port = 18090 / secret = <長い合言葉>
#   secret が空なら起動しない。発注は一切しない (データの受け取りだけ)。
# ============================================================
from __future__ import annotations

import configparser
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_client: Optional[socket.socket] = None
_client_name = ""
_remote_symbols: List[str] = []
_sent_symbols: List[str] = []
_stats = {"relay_msgs": 0, "relay_bad": 0, "connected_at": 0.0, "last_msg_at": 0.0}
_started = False
LOCAL_LIMIT = 50
REMOTE_LIMIT = 50


def _cfg() -> dict:
    out = {"enabled": False, "port": 18090, "secret": "", "bind": "0.0.0.0"}
    try:
        p = Path(__file__).resolve().parents[2] / "settings.ini"
        cp = configparser.ConfigParser()
        cp.read(p, encoding="utf-8")
        if cp.has_section("push_relay"):
            sec = cp["push_relay"]
            out["enabled"] = str(sec.get("enabled", "0")).strip().lower() in ("1", "true", "yes", "on")
            out["port"] = int(sec.get("port", "18090") or 18090)
            out["secret"] = str(sec.get("secret", "") or "").strip()
            out["bind"] = str(sec.get("bind", "0.0.0.0") or "0.0.0.0").strip()
    except Exception:
        logger.warning("[PUSH RELAY] settings read failed", exc_info=True)
    if os.getenv("PUSH_RELAY_ENABLED") is not None:
        out["enabled"] = str(os.getenv("PUSH_RELAY_ENABLED")).strip().lower() in ("1", "true", "yes", "on")
    return out


def is_connected() -> bool:
    with _lock:
        return _client is not None


def stats() -> dict:
    with _lock:
        return {**_stats, "connected": _client is not None, "name": _client_name, "remote_symbols": len(_remote_symbols)}


# ------------------------------------------------------------
# 銘柄の割り振り
# ------------------------------------------------------------
def split_targets_for_relay(targets: List[str]) -> List[str]:
    """登録対象を 自分の口座 (戻り値、最大 50) と 中継 (最大 50) に分ける。"""
    targets = [str(s) for s in (targets or []) if s]
    local = targets[:LOCAL_LIMIT]
    remote = [s for s in targets[LOCAL_LIMIT:LOCAL_LIMIT + REMOTE_LIMIT] if s not in local]
    if _started:
        set_remote_symbols(remote if is_connected() else [])
    return local


def set_remote_symbols(symbols: List[str]) -> None:
    global _remote_symbols
    with _lock:
        _remote_symbols = list(symbols)
        _send_register_locked()


def _send_register_locked() -> None:
    global _sent_symbols
    if _client is None or _remote_symbols == _sent_symbols:
        return
    try:
        _client.sendall((json.dumps({"type": "register", "symbols": _remote_symbols}) + "\n").encode("utf-8"))
        _sent_symbols = list(_remote_symbols)
        logger.warning("[PUSH RELAY] register sent count=%d head=%s", len(_remote_symbols), _remote_symbols[:8])
    except Exception:
        logger.warning("[PUSH RELAY] register send failed", exc_info=True)


# ------------------------------------------------------------
# 受け取った PUSH を on_message へ
# ------------------------------------------------------------
def _ingest(payload: Any) -> bool:
    if not isinstance(payload, dict) or not str(payload.get("Symbol") or "").strip():
        _stats["relay_bad"] += 1
        return False
    from trading.push.push_stream import state as ps_state
    from trading.push.push_stream.ws_callbacks import on_message
    # 自分の websocket の停止検知 (_last_message_at など) を中継の受信で上書きしない
    last_at = getattr(ps_state, "_last_message_at", None)
    total = getattr(ps_state, "_total_received", 0)
    try:
        on_message(None, payload)
    finally:
        try:
            ps_state._last_message_at = last_at
            ps_state._total_received = total
        except Exception:
            pass
    _stats["relay_msgs"] += 1
    _stats["last_msg_at"] = time.time()
    return True


def _serve_client(conn: socket.socket, addr, secret: str) -> None:
    global _client, _client_name, _sent_symbols
    conn.settimeout(60.0)
    f = conn.makefile("r", encoding="utf-8", newline="\n")
    try:
        hello = json.loads(f.readline() or "{}")
        if hello.get("type") != "hello" or str(hello.get("secret") or "") != secret:
            logger.warning("[PUSH RELAY] auth failed from %s", addr)
            return
        with _lock:
            if _client is not None:
                try:
                    _client.close()
                except Exception:
                    pass
            _client = conn
            _client_name = str(hello.get("name") or addr[0])
            _sent_symbols = []
            _stats["connected_at"] = time.time()
            conn.sendall(b'{"type":"ok"}\n')
            _send_register_locked()
        logger.warning("[PUSH RELAY] connected name=%s addr=%s", _client_name, addr)
        last_log = time.time()
        for line in f:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except Exception:
                _stats["relay_bad"] += 1
                continue
            t = msg.get("type")
            if t == "push":
                _ingest(msg.get("data"))
            elif t == "status":
                logger.info("[PUSH RELAY] agent status %s", {k: v for k, v in msg.items() if k != "type"})
            if time.time() - last_log >= 60:
                last_log = time.time()
                logger.warning("[PUSH RELAY] stats %s", stats())
    except Exception as e:
        logger.warning("[PUSH RELAY] client error addr=%s err=%s", addr, e)
    finally:
        with _lock:
            if _client is conn:
                _client = None
                _sent_symbols = []
        try:
            conn.close()
        except Exception:
            pass
        logger.warning("[PUSH RELAY] disconnected addr=%s stats=%s", addr, stats())


def _server_loop(bind: str, port: int, secret: str) -> None:
    while True:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((bind, port))
            srv.listen(2)
            logger.warning("[PUSH RELAY] listening %s:%d", bind, port)
            while True:
                conn, addr = srv.accept()
                threading.Thread(target=_serve_client, args=(conn, addr, secret), daemon=True,
                                 name="push-relay-client").start()
        except Exception:
            logger.exception("[PUSH RELAY] server error; retry in 10s")
            time.sleep(10)


def start_relay_server_safe() -> bool:
    """push_receiver プロセスから呼ぶ。設定が無効・secret 未設定なら何もしない。"""
    global _started
    if _started:
        return True
    c = _cfg()
    if not c["enabled"]:
        logger.info("[PUSH RELAY] disabled")
        return False
    if len(c["secret"]) < 16:
        logger.warning("[PUSH RELAY] secret が未設定か短すぎる (16 文字以上) ので起動しない")
        return False
    # 登録対象を 100 銘柄まで解決させる (先頭 50 が自分、残りが中継)
    os.environ["PUSH_ROTATION_TARGET_MAX_KEEP"] = "100"
    _started = True
    threading.Thread(target=_server_loop, args=(c["bind"], c["port"], c["secret"]), daemon=True,
                     name="push-relay-server").start()
    return True


__all__ = ["start_relay_server_safe", "split_targets_for_relay", "is_connected", "stats", "set_remote_symbols"]
