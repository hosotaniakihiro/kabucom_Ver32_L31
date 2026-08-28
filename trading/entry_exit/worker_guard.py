# -*- coding: utf-8 -*-
# ============================================================
# File   : trading/entry_exit/worker_guard.py
# Version: V1-ENTRY-WORKER-GENERATION-DEADLINE
# ------------------------------------------------------------
# Entry 系 worker (TONOSAMA / RANKING / SUMMARY tick) の
#   世代 (generation) / スレッド / deadline / 終了理由
# を一元管理する。
#
# 背景 (2026-08-26 実測, console_20260826_132625_75076.log):
#   [RANKING ENTRY HARD TIMEOUT] timeout -> return to scheduler
#       timeout_sec=28.000 thread_alive=True                       x10
#   [RANKING ENTRY SCHEDULE] hard timeout ... worker continues in daemon thread
#   14:25:30 [RANKING ENTRY LOOP] hard stop scoring elapsed=317.370s candidates=0
#   14:25:30 [RANKING ENTRY LOOP] hard stop scoring elapsed=197.484s candidates=0
#   14:25:30 [RANKING ENTRY LOOP] hard stop scoring elapsed= 77.919s candidates=0
#
#   scheduler は timeout で戻るが worker (daemon thread) は生き続け、
#   3 本が同時に走って NAS と GIL を奪い合っていた。
#   budget_sec=30 なのに 317 秒動いているのは、
#   ループ本体が deadline を見ていない区間で長時間ブロックするため。
#
# 設計:
#   - begin(kind, timeout_sec) は「前の世代のスレッドが生きている間は
#     新しい世代を作らない」。metadata だけ clear して無理に走らせない。
#   - 各 worker は自分の Slot を thread-local に持ち、
#     重いループ / DB read / normalize の前後で should_stop() を見る。
#   - deadline 超過や世代交代で should_stop() が True になったら
#     worker 自身が return する (協調キャンセル)。
#   - publish 前に is_current() を確認する。古い世代は publish しない。
#   - snapshot() で現在動いている worker 一覧をログへ出せる。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

VERSION = "V1-ENTRY-WORKER-GENERATION-DEADLINE"

_LOCK = threading.RLock()
_LOCAL = threading.local()


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return float(str(v).strip())
    except Exception:
        pass
    return float(default)


def enabled() -> bool:
    return _env_bool("ENTRY_WORKER_GUARD_ENABLED", True)


@dataclass
class Slot:
    kind: str
    generation: int
    started_at: float
    deadline_at: float
    thread_name: str
    thread_id: int
    cancel: threading.Event = field(default_factory=threading.Event)
    finished: bool = False
    finish_reason: str = ""

    def elapsed(self) -> float:
        return time.perf_counter() - self.started_at

    def remaining(self) -> float:
        return self.deadline_at - time.perf_counter()


# kind -> {"slot": Slot, "thread": threading.Thread}
_ACTIVE: dict[str, dict[str, Any]] = {}
_SEQ: dict[str, int] = {}


def current_generation(kind: str) -> int:
    with _LOCK:
        return int(_SEQ.get(str(kind), 0))


def active_slot(kind: str) -> Optional[Slot]:
    with _LOCK:
        rec = _ACTIVE.get(str(kind))
        return rec.get("slot") if rec else None


def begin(kind: str, *, timeout_sec: float, thread: Optional[threading.Thread] = None) -> Optional[Slot]:
    """新しい worker 世代を開始する。前世代が生きていれば None を返す。

    None が返ったら呼び出し側は「今回は動かさない」。
    metadata だけ clear して無理に走らせてはいけない。
    """
    kind = str(kind)
    if not enabled():
        gen = current_generation(kind) + 1
        with _LOCK:
            _SEQ[kind] = gen
        return Slot(
            kind=kind, generation=gen, started_at=time.perf_counter(),
            deadline_at=time.perf_counter() + max(1.0, float(timeout_sec)),
            thread_name=threading.current_thread().name,
            thread_id=threading.get_ident(),
        )

    now = time.perf_counter()
    with _LOCK:
        rec = _ACTIVE.get(kind)
        if rec is not None:
            old: Slot = rec["slot"]
            th: Optional[threading.Thread] = rec.get("thread")
            alive = bool(th is not None and th.is_alive())
            if not old.finished and alive:
                # 前世代へ「もう止まれ」と伝えたうえで、今回は起動しない。
                old.cancel.set()
                logger.warning(
                    "[ENTRY WORKER] begin refused kind=%s reason=previous_generation_alive "
                    "prev_generation=%s prev_thread=%s prev_elapsed=%.1fs prev_over_deadline=%.1fs "
                    "(metadata だけ clear して二重起動しない)",
                    kind, old.generation, old.thread_name, old.elapsed(),
                    max(0.0, -old.remaining()),
                )
                return None
            if not old.finished and not alive:
                logger.warning(
                    "[ENTRY WORKER] previous slot thread already gone kind=%s generation=%s "
                    "elapsed=%.1fs -> 世代を閉じる",
                    kind, old.generation, old.elapsed(),
                )
                old.finished = True
                old.finish_reason = "thread_gone"

        gen = int(_SEQ.get(kind, 0)) + 1
        _SEQ[kind] = gen
        slot = Slot(
            kind=kind, generation=gen, started_at=now,
            deadline_at=now + max(1.0, float(timeout_sec)),
            thread_name=threading.current_thread().name,
            thread_id=threading.get_ident(),
        )
        _ACTIVE[kind] = {"slot": slot, "thread": thread or threading.current_thread()}
    logger.warning(
        "[ENTRY WORKER] begin kind=%s generation=%s thread=%s tid=%s timeout=%.1fs version=%s",
        kind, slot.generation, slot.thread_name, slot.thread_id, float(timeout_sec), VERSION,
    )
    return slot


def attach_thread(slot: Optional[Slot], thread: threading.Thread) -> None:
    """実際に処理を行う daemon thread を世代へ結び付ける。"""
    if slot is None or thread is None:
        return
    with _LOCK:
        rec = _ACTIVE.get(slot.kind)
        if rec is not None and rec.get("slot") is slot:
            rec["thread"] = thread
            slot.thread_name = thread.name
    logger.info("[ENTRY WORKER] attach kind=%s generation=%s thread=%s", slot.kind, slot.generation, thread.name)


def bind_current(slot: Optional[Slot]) -> None:
    """このスレッドで走っている worker slot を記録する (深い階層から参照する)。"""
    try:
        _LOCAL.slot = slot
        if slot is not None:
            slot.thread_name = threading.current_thread().name
            slot.thread_id = threading.get_ident()
    except Exception:
        logger.exception("[ENTRY WORKER] bind_current failed")


def current() -> Optional[Slot]:
    return getattr(_LOCAL, "slot", None)


def is_current(slot: Optional[Slot]) -> bool:
    """この slot がまだ最新世代か (古い世代は publish 禁止)。"""
    if slot is None:
        return True
    return current_generation(slot.kind) == slot.generation


def should_stop(slot: Optional[Slot] = None, *, label: str = "") -> bool:
    """deadline 超過 / キャンセル / 世代交代 のいずれかなら True。"""
    s = slot if slot is not None else current()
    if s is None:
        return False
    if s.cancel.is_set():
        return True
    if time.perf_counter() >= s.deadline_at:
        return True
    if not is_current(s):
        return True
    return False


def stop_reason(slot: Optional[Slot] = None) -> str:
    s = slot if slot is not None else current()
    if s is None:
        return ""
    if s.cancel.is_set():
        return "cancelled"
    if time.perf_counter() >= s.deadline_at:
        return "deadline"
    if not is_current(s):
        return "generation_superseded"
    return ""


def check(slot: Optional[Slot] = None, *, label: str = "") -> bool:
    """should_stop に加えて、止まる理由をログへ残す。"""
    s = slot if slot is not None else current()
    if s is None:
        return False
    if not should_stop(s):
        return False
    logger.warning(
        "[ENTRY WORKER] stop kind=%s generation=%s thread=%s label=%s reason=%s "
        "elapsed=%.1fs over_deadline=%.1fs",
        s.kind, s.generation, s.thread_name, label or "-", stop_reason(s),
        s.elapsed(), max(0.0, -s.remaining()),
    )
    return True


def finish(slot: Optional[Slot], reason: str = "done") -> None:
    if slot is None:
        return
    slot.finished = True
    slot.finish_reason = str(reason)
    with _LOCK:
        rec = _ACTIVE.get(slot.kind)
        if rec is not None and rec.get("slot") is slot:
            _ACTIVE.pop(slot.kind, None)
    logger.warning(
        "[ENTRY WORKER] finish kind=%s generation=%s thread=%s reason=%s elapsed=%.2fs "
        "timed_out=%s cancelled=%s",
        slot.kind, slot.generation, slot.thread_name, slot.finish_reason, slot.elapsed(),
        bool(time.perf_counter() >= slot.deadline_at), bool(slot.cancel.is_set()),
    )
    try:
        _LOCAL.slot = None
    except Exception:
        pass


def cancel(kind: str, reason: str = "external") -> bool:
    with _LOCK:
        rec = _ACTIVE.get(str(kind))
        if not rec:
            return False
        slot: Slot = rec["slot"]
    slot.cancel.set()
    logger.warning("[ENTRY WORKER] cancel requested kind=%s generation=%s reason=%s elapsed=%.1fs",
                   slot.kind, slot.generation, reason, slot.elapsed())
    return True


def snapshot() -> list[dict[str, Any]]:
    """現在動いている worker 一覧 (ログ用)。"""
    out: list[dict[str, Any]] = []
    with _LOCK:
        items = list(_ACTIVE.items())
    for kind, rec in items:
        slot: Slot = rec["slot"]
        th: Optional[threading.Thread] = rec.get("thread")
        out.append({
            "kind": kind,
            "generation": slot.generation,
            "thread": slot.thread_name,
            "tid": slot.thread_id,
            "alive": bool(th is not None and th.is_alive()),
            "elapsed": round(slot.elapsed(), 2),
            "remaining": round(slot.remaining(), 2),
            "cancelled": bool(slot.cancel.is_set()),
            "timed_out": bool(time.perf_counter() >= slot.deadline_at),
            "finished": bool(slot.finished),
        })
    return out


def log_snapshot(context: str = "") -> None:
    try:
        snap = snapshot()
        if snap:
            logger.warning("[ENTRY WORKER] active=%s context=%s", snap, context or "-")
    except Exception:
        logger.exception("[ENTRY WORKER] snapshot log failed")


__all__ = [
    "VERSION", "Slot", "enabled", "begin", "attach_thread", "bind_current",
    "current", "current_generation", "active_slot", "is_current",
    "should_stop", "stop_reason", "check", "finish", "cancel",
    "snapshot", "log_snapshot",
]
