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
#   常連 = NAS 日足の直近 60 営業日で 値幅の平均が大きい 50 銘柄 (売買代金 10 億以上・300〜7,000 円、週 1 回)。
#   日足が読めなければ push DB 10 日分で代用。
#   中継がつながっていて常連が計算済みなら: 常連 50 を中継 (その日 1 日固定)、
#   本番の口座は 常連以外の 保有中・発注中 → 入れ替わる候補 で 50。
#   常連の計算前は 先頭 50 を自分、51〜100 番目を中継。中継がなければ従来どおり 50 銘柄だけ。
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
# ------------------------------------------------------------
# 常連 50 銘柄 (2026-10-03 ユーザー判断: 常連は中継側、本番の口座は保有・発注中と入れ替わる候補に使う)
#   直近 10 営業日の push DB で、その日に 200 ティック以上受信した日数が多い順 (同数ならティック数)。
#   起動時に裏で計算し runtime/relay_regulars_YYYYMMDD.json に保存する。計算が済むまでは従来の割り振り。
# ------------------------------------------------------------
_regulars: List[str] = []
_regulars_ready = False


def _compute_regulars(n: int = 50, days: int = 10, min_ticks: int = 200,
                      min_days: int = 3, min_range_pct: Optional[float] = None) -> List[str]:
    import datetime as dt
    import glob
    import re
    import sqlite3
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "autostock"
    if min_range_pct is None:
        min_range_pct = float(os.getenv("PUSH_RELAY_REGULARS_MIN_RANGE_PCT", "2.5") or 2.5)
    cache = base / "runtime" / f"relay_regulars_v2_{dt.date.today():%Y%m%d}.json"
    try:
        if cache.exists():
            return list(json.loads(cache.read_text(encoding="utf-8")).get("symbols") or [])[:n]
    except Exception:
        pass
    today = f"{dt.date.today():%Y%m%d}"
    files = sorted(f for f in glob.glob(str(base / "push" / "push2*.db"))
                   if re.search(r"push(\d{8})\.db$", f) and today not in f)[-days:]
    score: dict = {}
    for f in files:
        try:
            con = sqlite3.connect(f"file:{f}?mode=ro", uri=True, timeout=2.0)
            try:
                rows = con.execute("SELECT symbol, COUNT(*), MIN(price), MAX(price) FROM stream_data "
                                   "WHERE price > 0 GROUP BY symbol").fetchall()
            finally:
                con.close()
        except Exception:
            continue
        for sym, cnt, mn, mx in rows:
            if not sym or int(cnt or 0) < min_ticks or not mn or float(mn) <= 0:
                continue
            ranges = score.setdefault(str(sym), [])
            ranges.append((float(mx) / float(mn) - 1) * 100)
    # 2026-10-03: 値動きの小さい銘柄 (大型株で 1 日の値幅が 1% 前後) は常連にしない (ユーザー判断)。
    #   直近 10 日の常連候補 50 のうち 15 が値幅中央値 2% 未満 (5401 0.88% / 7203 0.95% / 6501 1.20% ...)。
    stat = {}
    for sym, rs in score.items():
        if len(rs) < min_days:
            continue
        med = sorted(rs)[len(rs) // 2]
        if med >= min_range_pct:
            stat[sym] = (len(rs), round(med, 2))
    out = [s for s, _ in sorted(stat.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))][:n]
    score = stat
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"symbols": out, "files": [os.path.basename(x) for x in files],
                                     "score": {s: score[s] for s in out}}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return out


def _compute_regulars_daily(n: int = 50) -> List[str]:
    """2026-10-03 (ユーザー判断: もっと長い期間で選ぶ): NAS 日足 DB の直近 60 営業日で選ぶ。週 1 回。
    売買代金の平均 >= 10 億円、株価 300〜7,000 円 (予算 50 万円・100 株で買える範囲)、40 日以上の足、
    TR/終値 >= 40% の壊れた足は除き、1 日の値幅 (TR/終値) の平均が大きい順に n 銘柄。
    10/3 試算: 候補 566、上位 50 の値幅平均 6〜15% (半導体・電線・新興株・9984 など)。"""
    import datetime as dt
    import sqlite3
    days = int(float(os.getenv("PUSH_RELAY_REGULARS_DAYS", "60") or 60))
    min_tv = float(os.getenv("PUSH_RELAY_REGULARS_MIN_TV", "1e9") or 1e9)
    pmin = float(os.getenv("PUSH_RELAY_REGULARS_MIN_PRICE", "300") or 300)
    pmax = float(os.getenv("PUSH_RELAY_REGULARS_MAX_PRICE", "7000") or 7000)
    base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "autostock"
    y, w, _ = dt.date.today().isocalendar()
    cache = base / "runtime" / f"relay_regulars_daily_{y}W{w:02d}.json"
    try:
        if cache.exists():
            return list(json.loads(cache.read_text(encoding="utf-8")).get("symbols") or [])[:n]
    except Exception:
        pass
    from trading.daily.daily_signal_loader import _resolve_daily_db_path
    con = sqlite3.connect(f"file:{_resolve_daily_db_path()}?mode=ro", uri=True, timeout=10)
    try:
        dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM stock_analysis_history "
                                           "ORDER BY date DESC LIMIT ?", (days,))]
        if len(dates) < 20:
            return []
        rows = con.execute("SELECT stock_code, date, high, low, close, volume FROM stock_analysis_history "
                           "WHERE date >= ? ORDER BY stock_code, date", (dates[-1],)).fetchall()
    finally:
        con.close()
    stat: dict = {}
    prev_sym, prev_close = None, None
    for sym, d, h, l, c, v in rows:
        if sym != prev_sym:
            prev_sym, prev_close = sym, None
        if not (c and h and l and c > 0 and h >= l > 0):
            continue
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close)) if prev_close else (h - l)
        prev_close = c
        trp = tr / c * 100
        if trp >= 40:
            continue
        st = stat.setdefault(str(sym), {"n": 0, "tr": 0.0, "tv": 0.0, "price": 0.0})
        st["n"] += 1
        st["tr"] += trp
        st["tv"] += c * float(v or 0)
        st["price"] = c
    cand = []
    for sym, st in stat.items():
        if st["n"] < 40:
            continue
        tv = st["tv"] / st["n"]
        if tv < min_tv or not (pmin <= st["price"] <= pmax):
            continue
        cand.append((sym, st["tr"] / st["n"], tv, st["price"]))
    cand.sort(key=lambda x: -x[1])
    out = [c[0] for c in cand[:n]]
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"symbols": out, "period": [dates[-1], dates[0]], "candidates": len(cand),
                                     "detail": {c[0]: {"atr_pct": round(c[1], 2), "tv_oku": round(c[2] / 1e8), "price": c[3]}
                                                for c in cand[:n]}}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return out


def _load_regulars_bg() -> None:
    global _regulars, _regulars_ready
    try:
        n = int(os.getenv("PUSH_RELAY_REGULARS_COUNT", str(REMOTE_LIMIT)) or REMOTE_LIMIT)
        try:
            _regulars = _compute_regulars_daily(n=n)
        except Exception:
            logger.warning("[PUSH RELAY] daily regulars failed; fallback to push DB 10 days", exc_info=True)
            _regulars = []
        if not _regulars:
            _regulars = _compute_regulars(n=n)
        _regulars_ready = bool(_regulars)
        logger.warning("[PUSH RELAY] regulars ready count=%d head=%s", len(_regulars), _regulars[:10])
    except Exception:
        logger.exception("[PUSH RELAY] regulars compute failed")


def _must_local_symbols() -> List[str]:
    """本番の口座で必ず受ける銘柄 = 保有中 (P1) と 発注中・候補ファイル (P2)。"""
    out: List[str] = []
    try:
        from trading.push.push_stream.protected_symbols import _priority_buckets
        for label, syms in _priority_buckets():
            if label.startswith(("P1_", "P2_")):
                for s in syms or []:
                    s = str(s)
                    if s and s not in out:
                        out.append(s)
    except Exception:
        logger.debug("[PUSH RELAY] protected buckets unavailable", exc_info=True)
    return out


# ------------------------------------------------------------
# 本番の口座 50 枠の入れ替え (2026-10-03 ユーザー判断: 入れ替え型)
#   時分割ローテーション (A/B) は 9/18 実測で各銘柄の観測率 13% (実効は固定 50 の 1/4) だったので使わない。
#   常に 50 銘柄を同時に受け、優先順は
#     1) 保有中・発注中   2) 入れてから PUSH_LOCAL_MIN_STAY_SEC (既定 600 秒) 未満 (最大 40 枠)
#     3) 候補の上位順 (残っている銘柄・新しく出てきた銘柄)。新顔のために PUSH_LOCAL_NEW_SLOTS (既定 10) 枠は空けておく
#   入れて 10 分を過ぎ、上位から外れた銘柄は外す。
# ------------------------------------------------------------
_local_added_at: dict = {}


def _sticky_local(must: List[str], targets: List[str], exclude: List[str]) -> List[str]:
    now = time.time()
    min_stay = float(os.getenv("PUSH_LOCAL_MIN_STAY_SEC", "600") or 600)
    ex = set(exclude or [])
    cand = [s for s in targets if s not in ex]
    cand_set = set(cand)
    current = [s for s in _local_added_at if s not in ex]
    out: List[str] = []

    def add(s):
        if s and s not in out and s not in ex and len(out) < LOCAL_LIMIT:
            out.append(s)

    # 新しく上位に出てきた銘柄のための枠 (急騰・出来高急増を 10 分待たせない)
    new_slots = int(float(os.getenv("PUSH_LOCAL_NEW_SLOTS", "10") or 10))
    sticky_cap = max(0, LOCAL_LIMIT - new_slots)
    for s in must:
        add(s)
    for s in sorted(current, key=lambda x: _local_added_at[x]):      # 2) 最低滞在時間内 (新顔の枠は残す)
        if len(out) >= sticky_cap:
            break
        if now - _local_added_at[s] < min_stay:
            add(s)
    for s in cand:                                                    # 3) 候補順: 残っている銘柄も新顔も
        add(s)
    for s in list(_local_added_at):
        if s not in out:
            _local_added_at.pop(s, None)
    for s in out:
        _local_added_at.setdefault(s, now)
    removed_new = [s for s in out if s not in cand_set and s not in must]
    if removed_new and len(removed_new) > 0:
        logger.debug("[PUSH LOCAL STICKY] kept_by_min_stay=%s", removed_new[:10])
    return out


def split_targets_for_relay(targets: List[str]) -> List[str]:
    """登録対象を 自分の口座 (戻り値、最大 50) と 中継 (最大 50) に分ける。
    常連が計算済みで中継がつながっていれば、常連 (保有中・発注中を除く) を中継へ、
    本番の口座は 保有中・発注中 → 残りの候補 (常連以外) の順で埋める。"""
    targets = [str(s) for s in (targets or []) if s]
    if _started and is_connected() and _regulars_ready:
        # 2026-10-03 (ユーザー判断): 中継側の常連はその日 1 日変えない (保有中・発注中になっても中継側で受ける)。
        #   本番の口座は常連以外だけを扱う (保有中・発注中 → 入れ替わる候補)。
        #   中継が止まると常連の PUSH は途切れる。保有中の価格は exit_price_source の broker フォールバックで取る。
        remote = list(_regulars[:REMOTE_LIMIT])
        must = [s for s in _must_local_symbols() if s not in remote]
        local = _sticky_local(must, targets, exclude=remote)
        set_remote_symbols(remote)
        return local
    local = _sticky_local(_must_local_symbols(), targets, exclude=[])
    remote = [s for s in targets if s not in local][:REMOTE_LIMIT]
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
    threading.Thread(target=_load_regulars_bg, daemon=True, name="push-relay-regulars").start()
    threading.Thread(target=_server_loop, args=(c["bind"], c["port"], c["secret"]), daemon=True,
                     name="push-relay-server").start()
    return True


__all__ = ["start_relay_server_safe", "split_targets_for_relay", "is_connected", "stats", "set_remote_symbols"]
