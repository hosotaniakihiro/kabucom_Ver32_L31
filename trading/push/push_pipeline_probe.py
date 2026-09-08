# ============================================================
# File   : trading/push/push_pipeline_probe.py
# Version: V1-PUSH-E2E-PIPELINE-ONELINE
# ------------------------------------------------------------
# Purpose:
#   kabu Station PUSH -> WebSocket -> callback -> queue -> DB writer
#   -> push{YYYYMMDD}.db stream_data_raw -> received_at / datetime
#   -> main.py latest_price_db_feeder -> memory_latest_1m -> Summary
#   の各段を **1 行** で確認できるようにする。
#
#   2026-09-02 の障害では
#     - SUB MANAGER の "token resolved" は正常
#     - latest_price_db_feeder の rowid_delta は増加継続
#   なのに Summary は push_db_stale age=748s だった。
#   「どの段で止まっているか」を 1 行で見えるようにしておかないと、
#   部分的に正常なログだけを見て正常と誤判定する。
#
#   出力する項目 (段ごと):
#     ws_connected / ws_last_msg_age / registered
#     queue / recv_total / recv_per_min
#     writer_alive / writer_last_ok_age / flushed_total / flushed_per_min
#     db_max_rowid / db_rowid_per_min / db_latest_dt / db_latest_recv / db_age
#     feeder_rowid / feeder_age
#     mem1m_ring_rows / mem1m_last_rowid
#     workers (force-direct worker の世代/滞留)
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

VERSION = "V1-PUSH-E2E-PIPELINE-ONELINE"

_THREAD: Optional[threading.Thread] = None
_LOCK = threading.Lock()
_PREV: dict = {}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _age(v: Any) -> Optional[float]:
    """datetime / ISO 文字列 -> 現在との差 (秒)。"""
    if v is None:
        return None
    try:
        if isinstance(v, dt.datetime):
            ts = v
        else:
            s = str(v).strip()
            if not s:
                return None
            ts = dt.datetime.fromisoformat(s)
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        return round((dt.datetime.now() - ts).total_seconds(), 1)
    except Exception:
        return None


def _rate_per_min(key: str, value: Optional[int], now_m: float) -> Optional[float]:
    """前回観測との差から 1 分あたりの増分を出す。"""
    if value is None:
        return None
    prev = _PREV.get(key)
    _PREV[key] = (now_m, int(value))
    if not prev:
        return None
    t0, v0 = prev
    dt_s = now_m - t0
    if dt_s <= 0.5:
        return None
    return round((int(value) - int(v0)) * 60.0 / dt_s, 1)


def _mod(name: str):
    """既に読み込まれているモジュールだけを返す。

    probe スレッドの中で import 文を実行すると、他スレッドが同じ
    パッケージを import している最中に import ロックで固まる。
    (2026-09-02 16:00 実測: probe が 3 分間 1 行も出さなかった)
    このプロセスに無い段は None のままで良い。
    """
    return sys.modules.get(name)


def _ws_stage() -> dict:
    out: dict = {}
    st = _mod("trading.push.push_stream.state")
    try:
        if st is None:
            raise RuntimeError("push_stream.state not loaded")
        ws = getattr(st, "_ws_app", None)
        out["ws_connected"] = bool(getattr(st, "_connected_event", None) and st._connected_event.is_set())
        out["ws_alive"] = bool(ws is not None)
        out["ws_last_msg_age"] = _age(getattr(st, "_last_message_at", None))
        out["ws_last_connect_age"] = _age(getattr(st, "_last_connect_at", None))
        out["ws_last_disconnect_age"] = _age(getattr(st, "_last_disconnect_at", None))
        out["recv_total"] = int(getattr(st, "_total_received", 0) or 0)
        out["flushed_total"] = int(getattr(st, "_total_flushed", 0) or 0)
        out["dropped_total"] = int(getattr(st, "_total_dropped", 0) or 0)
        out["err_total"] = int(getattr(st, "_total_errors", 0) or 0)
        try:
            out["queue"] = int(st._push_queue.qsize())
        except Exception:
            out["queue"] = None
        try:
            out["ring_rows"] = len(getattr(st, "_push_rows", ()) or ())
        except Exception:
            out["ring_rows"] = None
        fth = getattr(st, "_flush_thread", None)
        out["writer_alive"] = bool(fth is not None and fth.is_alive())
        out["writer_last_ok_age"] = _age(getattr(st, "_last_flush_at", None))
        gth = getattr(st, "_gsync_thread", None)
        out["gsync_alive"] = bool(gth is not None and gth.is_alive())
        out["gsync_last_elapsed"] = round(float(getattr(st, "_gsync_last_elapsed", 0.0) or 0.0), 3)
    except Exception:
        logger.debug("[PUSH E2E] ws stage unavailable", exc_info=True)

    # 所有権。これが無いと ws_connected=False が「故障」か「待機」か分からない。
    # 2026-09-03 に実際に誤読が起きた (二重起動の待機側を見て PUSH 停止と判断)。
    try:
        _wo = _mod("trading.push.push_stream.ws_owner")
        if _wo is None:
            raise RuntimeError("ws_owner not loaded")
        out["ws_owner"] = bool(_wo.is_push_ws_owner())
        if not out["ws_owner"]:
            holder_pid = None
            try:
                elsewhere, holder = _wo.owned_elsewhere()
                if isinstance(holder, dict):
                    holder_pid = holder.get("pid")
            except Exception:
                holder_pid = None
            if holder_pid is None:
                # owned_elsewhere() が「生きている別プロセス」を返せなくても、
                # ロックのメタファイルには直前の所有者 pid が残っている。
                try:
                    info = _wo.read_owner_info()
                    if isinstance(info, dict):
                        holder_pid = info.get("pid")
                except Exception:
                    holder_pid = None
            out["ws_owner_holder_pid"] = holder_pid
            out["ws_state_note"] = "standby_not_owner"
        else:
            out["ws_state_note"] = "owner"
    except Exception:
        out["ws_owner"] = None
        out["ws_state_note"] = None

    try:
        _rc = _mod("trading.push.push_stream.rotation_core")
        if _rc is None:
            raise RuntimeError("rotation_core not loaded")
        regs = getattr(_rc, "_LAST_REGISTERED", None) or getattr(_rc, "_last_registered", None)
        out["registered"] = len(regs) if regs is not None else None
    except Exception:
        out["registered"] = None
    return out


def _db_stage() -> dict:
    out: dict = {}
    _m = _mod("scheduler_jobs.summary.memory_latest_1m")
    try:
        if _m is None:
            raise RuntimeError("memory_latest_1m not loaded")
        push_db_probe = _m.push_db_probe
        push_db_read_stats = _m.push_db_read_stats
        pr = push_db_probe(max_age_sec=2.0)
        out["db_max_rowid"] = pr.get("max_rowid")
        out["db_latest_dt"] = pr.get("latest_datetime")
        out["db_latest_recv"] = pr.get("latest_received_at")
        out["db_age"] = _age(pr.get("latest_datetime"))
        out["db_probe_sec"] = pr.get("elapsed")
        out["db_probe_err"] = pr.get("error")

        rs = push_db_read_stats() or {}
        out["mem1m_last_rowid"] = rs.get("last_rowid")
        out["mem1m_ring_rows"] = rs.get("ring_rows")
        out["mem1m_cache_age"] = rs.get("age_sec")
    except Exception:
        logger.debug("[PUSH E2E] db stage unavailable", exc_info=True)
    return out


def _feeder_stage() -> dict:
    out: dict = {}
    try:
        f = _mod("trading.push.latest_price_db_feeder")
        if f is None:
            raise RuntimeError("latest_price_db_feeder not loaded")
        st = f.feeder_conn_stats() if hasattr(f, "feeder_conn_stats") else {}
        if isinstance(st, dict):
            for k in ("last_rowid", "table", "conn_age", "reuse"):
                if k in st:
                    out["feeder_" + k] = st[k]
    except Exception:
        logger.debug("[PUSH E2E] feeder stage unavailable", exc_info=True)
    return out


def _worker_stage() -> dict:
    out: dict = {}
    try:
        _d = _mod("scheduler_jobs.summary.direct_push_1m")
        if _d is None:
            raise RuntimeError("direct_push_1m not loaded")
        snaps = _d.force_direct_worker_stats() or []
        out["workers"] = ",".join(
            "%s:g%s%s" % (s.get("kind"), s.get("generation"),
                          ("/alive %.0fs" % s.get("age", 0.0)) if s.get("alive") else "")
            for s in snaps
        ) or None
        out["worker_alive_n"] = sum(1 for s in snaps if s.get("alive"))
    except Exception:
        logger.debug("[PUSH E2E] worker stage unavailable", exc_info=True)
    try:
        names = [t.name for t in threading.enumerate() if t.name.startswith("summary-force-direct")]
        out["force_direct_threads"] = len(names)
        out["threads_total"] = threading.active_count()
    except Exception:
        out["force_direct_threads"] = None
    try:
        _r = _mod("scheduler_jobs.summary.ranking_summary_jobs")
        if _r is None:
            raise RuntimeError("ranking_summary_jobs not loaded")
        inf = _r.ranking_long_tf_inflight() or {}
        out["ranking_longtf"] = ",".join("%s:%s" % (k, v) for k, v in sorted(inf.items())) or None
    except Exception:
        out["ranking_longtf"] = None
    try:
        _g = _mod("global_state")
        if _g is None:
            raise RuntimeError("global_state not loaded")
        gdf = getattr(_g.global_data, "push_df", None)
        out["global_push_df_rows"] = len(gdf) if gdf is not None and hasattr(gdf, "__len__") else None
    except Exception:
        out["global_push_df_rows"] = None
    try:
        psutil = _mod("psutil")
        if psutil is None:
            raise RuntimeError("psutil not loaded")
        out["rss_mb"] = round(psutil.Process().memory_info().rss / 1024 / 1024, 1)
    except Exception:
        out["rss_mb"] = None
    return out


def pipeline_status() -> dict:
    now_m = time.monotonic()
    d: dict = {"pid": os.getpid()}
    d.update(_ws_stage())
    d.update(_db_stage())
    d.update(_feeder_stage())
    d.update(_worker_stage())
    d["recv_per_min"] = _rate_per_min("recv", d.get("recv_total"), now_m)
    d["flushed_per_min"] = _rate_per_min("flushed", d.get("flushed_total"), now_m)
    d["db_rowid_per_min"] = _rate_per_min("db_rowid", d.get("db_max_rowid"), now_m)
    return d


_ORDER = (
    "pid",
    "ws_owner", "ws_state_note", "ws_owner_holder_pid",
    "ws_connected", "ws_alive", "ws_last_msg_age", "ws_last_connect_age",
    "ws_last_disconnect_age", "registered",
    "queue", "ring_rows", "recv_total", "recv_per_min", "dropped_total", "err_total",
    "writer_alive", "writer_last_ok_age", "flushed_total", "flushed_per_min",
    "gsync_alive", "gsync_last_elapsed",
    "db_max_rowid", "db_rowid_per_min", "db_latest_dt", "db_latest_recv", "db_age",
    "db_probe_sec", "db_probe_err",
    "feeder_last_rowid", "feeder_table", "feeder_conn_age",
    "mem1m_last_rowid", "mem1m_ring_rows", "mem1m_cache_age",
    "force_direct_threads", "threads_total", "worker_alive_n", "workers",
    "ranking_longtf", "global_push_df_rows", "rss_mb",
)


def pipeline_status_line() -> str:
    d = pipeline_status()
    parts = []
    for k in _ORDER:
        if k in d:
            parts.append("%s=%s" % (k, d[k]))
    for k in sorted(d):
        if k not in _ORDER:
            parts.append("%s=%s" % (k, d[k]))
    return "[PUSH E2E] " + " ".join(parts)


def _loop(interval: float) -> None:
    logger.warning("[PUSH E2E] pipeline probe started interval=%.1fs version=%s", interval, VERSION)
    # 起動直後に 1 本出す。プロセスが短命でも「どの段まで来ていたか」を残す。
    first_delay = max(1.0, _env_float("PUSH_E2E_PROBE_FIRST_DELAY_SEC", 5.0))
    wait = first_delay
    while True:
        try:
            time.sleep(wait)
            wait = interval
            t0 = time.perf_counter()
            line = pipeline_status_line()
            el = time.perf_counter() - t0
            # 「どこで止まっているか」を見落とさないよう常に WARNING で出す。
            logger.warning("%s probe_build_sec=%.3f", line, el)
        except Exception:
            logger.warning("[PUSH E2E] probe loop failed", exc_info=True)
            time.sleep(max(5.0, interval))


def start_pipeline_probe(interval: float | None = None) -> bool:
    global _THREAD
    if not _env_bool("PUSH_E2E_PROBE", True):
        return False
    iv = float(interval) if interval else _env_float("PUSH_E2E_PROBE_INTERVAL_SEC", 30.0)
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return True
        _THREAD = threading.Thread(target=_loop, args=(iv,), name="push-e2e-probe", daemon=True)
        _THREAD.start()
    return True


__all__ = [
    "VERSION",
    "pipeline_status",
    "pipeline_status_line",
    "start_pipeline_probe",
]
