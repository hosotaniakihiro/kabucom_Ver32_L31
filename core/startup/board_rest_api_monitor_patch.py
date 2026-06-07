# ============================================================
# File   : core/startup/board_rest_api_monitor_patch.py
# Version: V1-REST-BOARD-API-MONITOR
# ------------------------------------------------------------
# RESTフル板導入後のAPI呼び出し状況を監視する。
# urllib.request.urlopen を軽くwrapし、kabusapi /board /positions の
# 呼び出し回数・失敗回数・平均時間を1分ごとにログ出力する。
#
# 注意:
#   - 挙動は変えない。監視だけ。
#   - URLがkabusapi以外なら集計しない。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.request
from collections import defaultdict, deque
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_URLOPEN = None
_LOCK = threading.RLock()
_COUNTERS: dict[str, dict[str, float]] = defaultdict(lambda: {"ok": 0.0, "ng": 0.0, "total_ms": 0.0, "max_ms": 0.0})
_RECENT: deque[tuple[float, str, bool, float]] = deque(maxlen=2000)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _url_of(req: Any) -> str:
    try:
        if isinstance(req, str):
            return req
        if hasattr(req, "full_url"):
            return str(req.full_url)
        if hasattr(req, "get_full_url"):
            return str(req.get_full_url())
    except Exception:
        pass
    return ""


def _kind(url: str) -> str | None:
    u = str(url or "").lower()
    if "kabusapi" not in u:
        return None
    if "/board/" in u or u.endswith("/board"):
        return "board"
    if "/positions" in u:
        return "positions"
    if "/orders" in u:
        return "orders"
    return None


def _record(kind: str, ok: bool, elapsed_ms: float) -> None:
    now = time.time()
    with _LOCK:
        d = _COUNTERS[kind]
        if ok:
            d["ok"] += 1
        else:
            d["ng"] += 1
        d["total_ms"] += float(elapsed_ms)
        d["max_ms"] = max(float(d.get("max_ms", 0.0)), float(elapsed_ms))
        _RECENT.append((now, kind, ok, float(elapsed_ms)))


def _snapshot_window(sec: float) -> dict[str, dict[str, float]]:
    cutoff = time.time() - sec
    out: dict[str, dict[str, float]] = defaultdict(lambda: {"ok": 0.0, "ng": 0.0, "total_ms": 0.0, "max_ms": 0.0})
    with _LOCK:
        for ts, kind, ok, ms in list(_RECENT):
            if ts < cutoff:
                continue
            d = out[kind]
            if ok:
                d["ok"] += 1
            else:
                d["ng"] += 1
            d["total_ms"] += ms
            d["max_ms"] = max(d["max_ms"], ms)
    return dict(out)


def _format_stats(stats: dict[str, dict[str, float]]) -> str:
    parts = []
    for kind in ("board", "positions", "orders"):
        d = stats.get(kind, {})
        ok = int(d.get("ok", 0))
        ng = int(d.get("ng", 0))
        total = ok + ng
        avg = (d.get("total_ms", 0.0) / total) if total else 0.0
        mx = d.get("max_ms", 0.0)
        parts.append(f"{kind}:total={total} ok={ok} ng={ng} avg_ms={avg:.1f} max_ms={mx:.1f}")
    return " | ".join(parts)


def _report_loop() -> None:
    interval = max(10.0, _env_float("BOARD_REST_API_MONITOR_INTERVAL_SEC", 60.0))
    warn_board_per_min = max(1.0, _env_float("BOARD_REST_API_MONITOR_WARN_BOARD_PER_MIN", 120.0))
    while True:
        try:
            stats = _snapshot_window(interval)
            line = _format_stats(stats)
            board_total = int(stats.get("board", {}).get("ok", 0) + stats.get("board", {}).get("ng", 0))
            if board_total >= warn_board_per_min:
                logger.warning("[BOARD REST API MONITOR] HIGH_USAGE window=%.0fs %s warn_board_per_min=%.0f", interval, line, warn_board_per_min)
            else:
                logger.info("[BOARD REST API MONITOR] window=%.0fs %s", interval, line)
        except Exception:
            logger.exception("[BOARD REST API MONITOR] report loop error")
        time.sleep(interval)


def install() -> bool:
    global _INSTALLED, _ORIG_URLOPEN
    if _INSTALLED:
        return True
    if not _env_bool("BOARD_REST_API_MONITOR_ENABLED", True):
        logger.warning("[BOARD REST API MONITOR] disabled")
        return False
    old = urllib.request.urlopen
    if getattr(old, "_board_rest_api_monitor_wrapped", False):
        _INSTALLED = True
        return True
    _ORIG_URLOPEN = old

    def wrapped_urlopen(req, *args, **kwargs):
        url = _url_of(req)
        k = _kind(url)
        if not k:
            return old(req, *args, **kwargs)
        start = time.perf_counter()
        try:
            ret = old(req, *args, **kwargs)
            _record(k, True, (time.perf_counter() - start) * 1000.0)
            return ret
        except Exception:
            _record(k, False, (time.perf_counter() - start) * 1000.0)
            raise

    wrapped_urlopen._board_rest_api_monitor_wrapped = True  # type: ignore[attr-defined]
    wrapped_urlopen._original = old  # type: ignore[attr-defined]
    urllib.request.urlopen = wrapped_urlopen
    threading.Thread(target=_report_loop, daemon=True, name="board_rest_api_monitor_loop").start()
    _INSTALLED = True
    logger.warning(
        "[BOARD REST API MONITOR] installed interval=%.0fs warn_board_per_min=%.0f",
        _env_float("BOARD_REST_API_MONITOR_INTERVAL_SEC", 60.0),
        _env_float("BOARD_REST_API_MONITOR_WARN_BOARD_PER_MIN", 120.0),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[BOARD REST API MONITOR] auto install failed")


__all__ = ["install"]
