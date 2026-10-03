# ============================================================
# File   : ops/push_relay/relay_agent.py
# 2026-10-03: 2 つ目の kabu 口座の PUSH を、本番 PC の push_receiver へ転送する中継
# ------------------------------------------------------------
# もう 1 台の PC (2 つ目の口座で kabu Station を起動しておく) で動かす。
#   1) 本番 PC (trading/push/push_relay_server.py) に TCP で接続し、合言葉で認証する
#   2) 本番 PC から届いた銘柄リスト (最大 50) を、この PC の kabu Station に登録する
#   3) この PC の kabu Station の PUSH を受け取り、そのまま本番 PC へ転送する
# 発注は一切しない。必要なのは Python 3 と `pip install websocket-client requests` だけ。
# 設定は同じフォルダの relay_agent.ini (relay_agent.ini.example をコピーして編集)。
# ============================================================
from __future__ import annotations

import configparser
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import requests
import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("relay_agent")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
for h in (logging.StreamHandler(sys.stdout),
          RotatingFileHandler(os.path.join(HERE, "relay_agent.log"), maxBytes=20_000_000, backupCount=3, encoding="utf-8")):
    h.setFormatter(_fmt)
    log.addHandler(h)

cp = configparser.ConfigParser()
cp.read(os.path.join(HERE, "relay_agent.ini"), encoding="utf-8")
KABU_PORT = int(cp.get("kabu", "port", fallback="18080"))
KABU_PASSWORD = cp.get("kabu", "api_password", fallback="").strip()
MAIN_HOST = cp.get("main", "host", fallback="").strip()
MAIN_PORT = int(cp.get("main", "port", fallback="18090"))
SECRET = cp.get("main", "secret", fallback="").strip()
NAME = cp.get("main", "name", fallback=socket.gethostname()).strip()
API = f"http://localhost:{KABU_PORT}/kabusapi"

_token = ""
_out: "queue.Queue[str]" = queue.Queue(maxsize=20000)
_main_ok = threading.Event()
_stats = {"kabu_msgs": 0, "sent": 0, "dropped": 0, "registered": 0}
_want: list = []
_registered: list = []
_reg_lock = threading.Lock()


# ---------------- kabu Station (この PC) ----------------
def get_token() -> str:
    global _token
    while True:
        try:
            r = requests.post(f"{API}/token", json={"APIPassword": KABU_PASSWORD}, timeout=10)
            if r.status_code == 200 and r.json().get("Token"):
                _token = r.json()["Token"]
                log.info("kabu token OK")
                return _token
            log.warning("kabu token failed status=%s body=%s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("kabu token error %s (kabu Station は起動している? API は有効?)", e)
        time.sleep(15)


def _kabu_put(path: str, body: dict):
    for _ in range(2):
        r = requests.put(f"{API}{path}", json=body, headers={"X-API-KEY": _token}, timeout=10)
        if r.status_code == 401:
            get_token()
            continue
        return r
    return r


def apply_register(symbols: list) -> None:
    """登録をやり直す (全解除 → 最大 50 件を登録)。"""
    global _registered
    symbols = [str(s) for s in symbols if s][:50]
    with _reg_lock:
        if symbols == _registered:
            return
        try:
            r = _kabu_put("/unregister/all", {})
            log.info("unregister/all status=%s", getattr(r, "status_code", None))
            if symbols:
                r = _kabu_put("/register", {"Symbols": [{"Symbol": s, "Exchange": 1} for s in symbols]})
                log.info("register count=%d status=%s body=%s", len(symbols), r.status_code, r.text[:200])
                if r.status_code != 200:
                    return
            _registered = symbols
            _stats["registered"] = len(symbols)
        except Exception:
            log.exception("register failed")


def kabu_ws_loop() -> None:
    def on_message(ws, message):
        _stats["kabu_msgs"] += 1
        if not _main_ok.is_set():
            return
        try:
            _out.put_nowait(message if isinstance(message, str) else message.decode("utf-8", "ignore"))
        except queue.Full:
            _stats["dropped"] += 1

    while True:
        try:
            ws = websocket.WebSocketApp(f"ws://localhost:{KABU_PORT}/kabusapi/websocket",
                                        on_message=on_message,
                                        on_open=lambda ws: log.info("kabu websocket connected"),
                                        on_error=lambda ws, e: log.warning("kabu websocket error %s", e),
                                        on_close=lambda ws, *a: log.warning("kabu websocket closed %s", a))
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception:
            log.exception("kabu websocket loop error")
        time.sleep(5)


# ---------------- 本番 PC ----------------
def main_loop() -> None:
    backoff = 3
    while True:
        sock = None
        try:
            sock = socket.create_connection((MAIN_HOST, MAIN_PORT), timeout=10)
            sock.settimeout(None)
            sock.sendall((json.dumps({"type": "hello", "secret": SECRET, "name": NAME}) + "\n").encode("utf-8"))
            rf = sock.makefile("r", encoding="utf-8", newline="\n")
            ok = json.loads(rf.readline() or "{}")
            if ok.get("type") != "ok":
                log.warning("main rejected (secret 不一致?) resp=%s", ok)
                time.sleep(30)
                continue
            log.info("main connected %s:%s", MAIN_HOST, MAIN_PORT)
            backoff = 3
            stop = threading.Event()

            def reader():
                try:
                    for line in rf:
                        msg = json.loads(line) if line.strip() else {}
                        if msg.get("type") == "register":
                            apply_register(msg.get("symbols") or [])
                except Exception as e:
                    log.warning("main reader stopped %s", e)
                finally:
                    stop.set()

            threading.Thread(target=reader, daemon=True).start()
            _main_ok.set()
            last_status = time.time()
            while not stop.is_set():
                try:
                    raw = _out.get(timeout=5)
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    sock.sendall((json.dumps({"type": "push", "data": data}, ensure_ascii=False) + "\n").encode("utf-8"))
                    _stats["sent"] += 1
                except queue.Empty:
                    sock.sendall(b'{"type":"ping"}\n')
                if time.time() - last_status >= 30:
                    last_status = time.time()
                    sock.sendall((json.dumps({"type": "status", **_stats}) + "\n").encode("utf-8"))
                    log.info("stats %s", _stats)
        except Exception as e:
            log.warning("main connection error %s (本番 PC のファイアウォール / IP / ポートを確認)", e)
        finally:
            _main_ok.clear()
            while not _out.empty():
                try:
                    _out.get_nowait()
                except Exception:
                    break
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


def main() -> int:
    if not (KABU_PASSWORD and MAIN_HOST and len(SECRET) >= 16):
        log.error("relay_agent.ini の api_password / host / secret (16 文字以上) を設定してください")
        return 1
    get_token()
    threading.Thread(target=kabu_ws_loop, daemon=True, name="kabu-ws").start()
    main_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
