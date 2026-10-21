# -*- coding: utf-8 -*-
"""Summary worker のステージ別 elapsed / deadline / 協調キャンセル。

P0-2 対応。

背景 (2026-08-26 のログ):
    generation=18 owner=tick:20260826145200
    14:56 時点で age=241 秒、done=False running=True cancelled=False thread_alive=True

    ワーカーが「どこで」止まったのかログから分からなかった。
    scheduler 側の inflight メタデータだけを clear しても、
    ワーカー本体は走り続けるので意味がない。

この module がやること:
    1. ワーカー内の各ステージの開始/終了/elapsed を generation 付きで出す。
    2. ワーカー全体に予算 (既定 25 秒) を持たせる。
    3. 各ステージの入口で予算超過を確認し、超えていたらワーカー自身が
       協調的に return する (外から Thread を殺さない)。

使い方:
    with stage_tracker.worker("push:1", generation=token, owner=owner):
        with stage_tracker.stage("runner"):
            df = runner(...)
        if stage_tracker.should_stop():
            return df                     # 自分から降りる
        with stage_tracker.stage("normalize"):
            ...

env:
    SUMMARY_STAGE_TRACKER_ENABLED=0   -> 無効化 (従来動作)
    SUMMARY_STAGE_BUDGET_SEC=25       -> ワーカー全体の予算 (秒)
    SUMMARY_STAGE_SLOW_SEC=3          -> これを超えたステージは WARNING で出す
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TLS = threading.local()


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return default
        return float(str(v).strip())
    except Exception:
        return default


def enabled() -> bool:
    return _env_bool("SUMMARY_STAGE_TRACKER_ENABLED", True)


def budget_sec() -> float:
    """ワーカー全体の予算。完成条件は 1分 Summary worker < 10 秒 (目標 < 5 秒)。

    ここは「これを超えたら自分から降りる」ハードな上限なので 25 秒。
    timeout を伸ばして誤魔化す用途では使わない。
    """
    return _env_float("SUMMARY_STAGE_BUDGET_SEC", 25.0)


def slow_sec() -> float:
    return _env_float("SUMMARY_STAGE_SLOW_SEC", 3.0)


def _unaccounted_warn_sec() -> float:
    """どの stage にも属さない時間がこれを超えたら WARNING。"""
    return _env_float("SUMMARY_STAGE_UNACCOUNTED_WARN_SEC", 2.0)


def _heartbeat_sec() -> float:
    """worker heartbeat の間隔。0 以下で無効。"""
    return _env_float("SUMMARY_WORKER_HEARTBEAT_SEC", 5.0)


# ============================================================
# P0-7: cancel から実停止までの上限
# ============================================================

class SummaryWorkerCancelled(BaseException):
    """協調キャンセル。stage 境界で投げて worker を降ろす。

    BaseException を継承しているのは、途中の `except Exception:` に
    握り潰されて走り続けるのを防ぐため。
    worker() の finally が必ず受け止めるので外へは漏れない。
    """


def cancel_sla_sec() -> float:
    """cancel 要求から実停止までの許容秒数。超えたら SLA 違反として記録する。"""
    return max(1.0, _env_float("SUMMARY_WORKER_CANCEL_SLA_SEC", 5.0))


def _hard_cancel_enabled() -> bool:
    """stage 境界で例外を投げて降りるか。既定 True。"""
    return _env_bool("SUMMARY_WORKER_HARD_CANCEL", True)


def _start_cancel_watchdog(ctx: "WorkerCtx", stop: threading.Event) -> Optional[threading.Thread]:
    """cancel 後 SLA 秒以内に降りられなかったら、どこで固まっているか出す。

    2026-08-27 の push:1 は cancel 要求 15:25:00 -> 実停止 15:30:40 で
    330 秒かかった。原因は light_push_runner の中に降り場所が無かったこと。
    どの stage で SLA を超えたかを名指しできるようにする。
    """
    sla = cancel_sla_sec()

    def _watch() -> None:
        # cancel されるまで待つ
        while not stop.is_set():
            if ctx.cancel.wait(0.25):
                break
        if stop.is_set():
            return
        cancelled_at = time.perf_counter()
        breached = False
        while not stop.wait(0.5):
            waited = time.perf_counter() - cancelled_at
            if waited < sla:
                continue
            if not breached:
                breached = True
                ctx.cancel_sla_breached = True
            logger.error(
                "[SUMMARY STAGE] cancel SLA breach kind=%s generation=%s owner=%s "
                "waited=%.1fs sla=%.1fs current_stage=%s stage_elapsed=%.1fs "
                "total=%.1fs thread=%s tid=%s "
                "(cancel 後 %.1f 秒以内に降りられていない。"
                "この stage の中に should_stop() 相当の降り場所が無い)",
                ctx.kind, ctx.generation, ctx.owner, waited, sla,
                ctx.current_stage or "<between stages>",
                time.perf_counter() - (ctx.current_stage_at or ctx.started_at),
                ctx.elapsed, ctx.thread_name, ctx.thread_id, sla,
            )
            _dump_worker_stack(ctx)
            # 一度出したら 30 秒ごとに再掲する (ログを埋めない)
            if stop.wait(30.0):
                return

    t = threading.Thread(target=_watch, name="summary-cancel-watchdog", daemon=True)
    t.start()
    return t


def _dump_worker_stack(ctx: "WorkerCtx") -> None:
    """止まらないワーカーのスタックを出す。推測せず場所を特定するため。"""
    try:
        import traceback

        frames = sys._current_frames()
        frame = frames.get(int(ctx.thread_id or -1))
        if frame is None:
            logger.error("[SUMMARY STAGE] cancel SLA breach: thread=%s のフレームが取れない",
                         ctx.thread_name)
            return
        stack = "".join(traceback.format_stack(frame, limit=25))
        logger.error(
            "[SUMMARY STAGE] cancel SLA breach stack kind=%s generation=%s thread=%s:\n%s",
            ctx.kind, ctx.generation, ctx.thread_name, stack,
        )
    except Exception:
        logger.exception("[SUMMARY STAGE] stack dump failed")


def _start_heartbeat(ctx: "WorkerCtx", stop: threading.Event) -> Optional[threading.Thread]:
    """worker が今どこに居るかを定期的に出す。

    stage 計測の外で時間を使っている場合でも、
    current_stage=<between stages> として位置が分かる。
    """
    interval = _heartbeat_sec()
    if interval <= 0:
        return None

    def _beat() -> None:
        n = 0
        while not stop.wait(interval):
            n += 1
            try:
                cur = ctx.current_stage or "<between stages>"
                since = time.perf_counter() - (ctx.current_stage_at or ctx.started_at)
                logger.warning(
                    "[SUMMARY WORKER HEARTBEAT] generation=%s kind=%s owner=%s beat=%d "
                    "current_stage=%s stage_elapsed=%.1fs total_elapsed=%.1fs "
                    "stage_sum=%.1fs unaccounted=%.1fs cancelled=%s "
                    "deadline_remaining=%.1fs thread=%s tid=%s",
                    ctx.generation, ctx.kind, ctx.owner, n, cur, since, ctx.elapsed,
                    ctx.top_elapsed, ctx.elapsed - ctx.top_elapsed,
                    ctx.cancel.is_set(), ctx.remaining, ctx.thread_name, ctx.thread_id,
                )
            except Exception:
                logger.debug("[SUMMARY WORKER HEARTBEAT] failed", exc_info=True)

    t = threading.Thread(target=_beat, name="summary-worker-heartbeat", daemon=True)
    t.start()
    return t


class WorkerCtx:
    __slots__ = ("kind", "generation", "owner", "started_at", "deadline_at",
                 "cancel", "stages", "stopped_reason", "stop_logged",
                 "thread_name", "thread_id", "depth", "top_elapsed",
                 "current_stage", "current_stage_at",
                 "cancelled_at", "cancel_sla_breached")

    def __init__(self, kind: str, generation: Any, owner: str, budget: float) -> None:
        self.kind = str(kind)
        self.generation = generation
        self.owner = str(owner or "")
        self.started_at = time.perf_counter()
        self.deadline_at = self.started_at + float(budget)
        self.cancel = threading.Event()
        self.stages: list[tuple[str, float]] = []
        self.stopped_reason: Optional[str] = None
        self.stop_logged = False
        # stage の入れ子深さ。トップレベルだけを合計して二重計上を避ける。
        self.depth = 0
        self.top_elapsed = 0.0
        # 今どの stage に居るか。stage の外なら None。
        self.current_stage: Optional[str] = None
        self.current_stage_at = self.started_at
        # P0-7: cancel 要求時刻と SLA 違反フラグ。cancel latency の実測用。
        self.cancelled_at: Optional[float] = None
        self.cancel_sla_breached = False
        th = threading.current_thread()
        self.thread_name = th.name
        self.thread_id = th.ident

    @property
    def cancel_latency(self) -> Optional[float]:
        """cancel 要求から現在までの秒数。cancel されていなければ None。"""
        if self.cancelled_at is None:
            return None
        return time.perf_counter() - self.cancelled_at

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started_at

    @property
    def remaining(self) -> float:
        return self.deadline_at - time.perf_counter()


def current() -> Optional[WorkerCtx]:
    return getattr(_TLS, "ctx", None)


def _fmt_stages(ctx: WorkerCtx) -> str:
    return ",".join("%s=%.3f" % (n, e) for n, e in ctx.stages) or "-"


@contextlib.contextmanager
def worker(kind: str, *, generation: Any = None, owner: str = "", budget: Optional[float] = None):
    """1 本のワーカーの生存期間。ネストした場合は外側を優先する。"""
    if not enabled():
        yield None
        return
    prev = current()
    if prev is not None:
        # 既に親ワーカーの中。予算は親のものを使い続ける。
        yield prev
        return
    ctx = WorkerCtx(kind, generation, owner, budget if budget is not None else budget_sec())
    _TLS.ctx = ctx
    logger.info(
        "[SUMMARY STAGE] worker begin kind=%s generation=%s owner=%s thread=%s tid=%s budget=%.1fs",
        ctx.kind, ctx.generation, ctx.owner, ctx.thread_name, ctx.thread_id,
        ctx.deadline_at - ctx.started_at,
    )
    err = None
    cancelled_clean = False
    _hb_stop = threading.Event()
    _hb = _start_heartbeat(ctx, _hb_stop)
    _wd = _start_cancel_watchdog(ctx, _hb_stop)
    try:
        yield ctx
    except SummaryWorkerCancelled as e:
        # 協調キャンセルは異常終了ではない。ここで受け止めて外へ出さない。
        cancelled_clean = True
        logger.warning(
            "[SUMMARY STAGE] worker stopped by cooperative cancel kind=%s generation=%s "
            "owner=%s cancel_latency=%.2fs detail=%s",
            ctx.kind, ctx.generation, ctx.owner,
            ctx.cancel_latency if ctx.cancel_latency is not None else -1.0, e,
        )
    except BaseException as e:   # noqa: BLE001  - ログに残してから再送出する
        err = e
        raise
    finally:
        _hb_stop.set()
        for _t in (_hb, _wd):
            if _t is not None:
                try:
                    _t.join(1.0)
                except Exception:
                    logger.debug("[SUMMARY WORKER HEARTBEAT] join failed", exc_info=True)
        _TLS.ctx = None
        total = ctx.elapsed
        over = total > (ctx.deadline_at - ctx.started_at)
        # P0-9: どの stage にも計上されていない時間。
        # これが大きいなら「計測していない場所」で時間を使っている。
        unaccounted = total - ctx.top_elapsed
        latency = ctx.cancel_latency
        log = logger.warning if (over or ctx.stopped_reason or err) else logger.info
        log(
            "[SUMMARY STAGE] worker end kind=%s generation=%s owner=%s thread=%s "
            "total=%.3fs stage_sum=%.3fs unaccounted=%.3fs over_budget=%s "
            "stopped_reason=%s cancel_latency=%s cancel_sla=%.1fs sla_breached=%s "
            "cancelled_clean=%s err=%s stages=[%s]",
            ctx.kind, ctx.generation, ctx.owner, ctx.thread_name,
            total, ctx.top_elapsed, unaccounted, over,
            ctx.stopped_reason,
            "None" if latency is None else "%.2fs" % latency,
            cancel_sla_sec(), ctx.cancel_sla_breached, cancelled_clean,
            repr(err) if err else None, _fmt_stages(ctx),
        )
        if latency is not None and latency > cancel_sla_sec():
            logger.error(
                "[SUMMARY STAGE] cancel latency SLA 違反 kind=%s generation=%s "
                "latency=%.2fs sla=%.1fs stages=[%s] "
                "(cancel から実停止まで %.1f 秒以内にすること)",
                ctx.kind, ctx.generation, latency, cancel_sla_sec(),
                _fmt_stages(ctx), cancel_sla_sec(),
            )
        if unaccounted > _unaccounted_warn_sec():
            logger.warning(
                "[SUMMARY STAGE] unaccounted elapsed kind=%s generation=%s "
                "unaccounted=%.3fs total=%.3fs stage_sum=%.3fs "
                "(stage 計測の外で時間を使っている。計測点を足す必要がある)",
                ctx.kind, ctx.generation, unaccounted, total, ctx.top_elapsed,
            )


@contextlib.contextmanager
def stage(name: str):
    """1 ステージ。開始/終了/elapsed を generation 付きで出す。

    P0-7: cancel 済みなら stage に入らず SummaryWorkerCancelled を投げる。
    旧実装は stage の入口で cancel を見ていなかったため、
    2026-08-27 15:25:00 に cancel した push:1 が 15:30:40 まで
    (330 秒) 走り続けていた。stage 境界で必ず降りる。
    """
    ctx = current()
    if ctx is None:
        yield None
        return
    if _hard_cancel_enabled() and ctx.cancel.is_set():
        should_stop("stage_entry:%s" % name)
        raise SummaryWorkerCancelled(
            "worker cancelled before stage=%s kind=%s generation=%s reason=%s"
            % (name, ctx.kind, ctx.generation, ctx.stopped_reason)
        )
    t0 = time.perf_counter()
    is_top = (ctx.depth == 0)
    ctx.depth += 1
    _prev_stage, _prev_at = ctx.current_stage, ctx.current_stage_at
    ctx.current_stage = str(name)
    ctx.current_stage_at = t0
    logger.debug(
        "[SUMMARY STAGE] stage_start name=%s kind=%s generation=%s depth=%d elapsed=%.3fs remaining=%.3fs",
        name, ctx.kind, ctx.generation, ctx.depth, ctx.elapsed, ctx.remaining,
    )
    try:
        yield ctx
    finally:
        el = time.perf_counter() - t0
        ctx.depth -= 1
        ctx.current_stage, ctx.current_stage_at = _prev_stage, _prev_at
        if is_top:
            # トップレベルの stage だけ合計する (入れ子は外側に含まれている)
            ctx.top_elapsed += el
        ctx.stages.append((str(name), el))
        log = logger.warning if el >= slow_sec() else logger.info
        log(
            "[SUMMARY STAGE] stage_end name=%s kind=%s generation=%s elapsed=%.3fs "
            "total=%.3fs remaining=%.3fs",
            name, ctx.kind, ctx.generation, el, ctx.elapsed, ctx.remaining,
        )


def cancel(ctx: Optional[WorkerCtx] = None, reason: str = "cancelled") -> None:
    """外から協調キャンセルを要求する (Thread は殺さない)。"""
    c = ctx or current()
    if c is None:
        return
    already = c.cancel.is_set()
    if not already:
        # cancel latency (要求 -> 実停止) を測るための基準時刻。
        c.cancelled_at = time.perf_counter()
    c.cancel.set()
    if c.stopped_reason is None:
        c.stopped_reason = reason
    if not already:
        logger.warning(
            "[SUMMARY STAGE] cancel requested kind=%s generation=%s owner=%s "
            "reason=%s elapsed=%.3fs thread=%s sla=%.1fs",
            c.kind, c.generation, c.owner, reason, c.elapsed, c.thread_name,
            cancel_sla_sec(),
        )


def checkpoint(label: str = "") -> None:
    """重い処理の途中から明示的に降りるための降り場所。

    should_stop() と違い、True/False を返さず即座に例外で降りる。
    ループの内側 (銘柄ごと / チャンクごと) に置くこと。
    """
    ctx = current()
    if ctx is None:
        return
    if not ctx.cancel.is_set() and ctx.remaining > 0.0:
        return
    should_stop(label or "checkpoint")
    if _hard_cancel_enabled():
        raise SummaryWorkerCancelled(
            "worker cancelled at checkpoint=%s kind=%s generation=%s reason=%s"
            % (label or "-", ctx.kind, ctx.generation, ctx.stopped_reason)
        )


def should_stop(label: str = "") -> bool:
    """ワーカー自身が「もう降りるべきか」を判定する。

    True を返したら呼び出し側は速やかに return すること。
    """
    ctx = current()
    if ctx is None:
        return False
    reason = None
    if ctx.cancel.is_set():
        reason = "cancelled"
    elif ctx.remaining <= 0.0:
        reason = "budget_exceeded"
    if reason is None:
        return False
    if ctx.stopped_reason is None:
        ctx.stopped_reason = reason
    # 外から cancel() 済みでも「ワーカーがどこで降りたか」は必ず 1 回出す。
    if not ctx.stop_logged:
        ctx.stop_logged = True
        logger.warning(
            "[SUMMARY STAGE] cooperative stop kind=%s generation=%s owner=%s label=%s "
            "reason=%s requested_reason=%s elapsed=%.3fs budget=%.1fs thread=%s stages=[%s]",
            ctx.kind, ctx.generation, ctx.owner, label or "-", reason,
            ctx.stopped_reason, ctx.elapsed, ctx.deadline_at - ctx.started_at,
            ctx.thread_name, _fmt_stages(ctx),
        )
    return True


def snapshot() -> dict[str, Any]:
    ctx = current()
    if ctx is None:
        return {}
    return {
        "kind": ctx.kind,
        "generation": ctx.generation,
        "owner": ctx.owner,
        "thread": ctx.thread_name,
        "tid": ctx.thread_id,
        "elapsed": round(ctx.elapsed, 3),
        "remaining": round(ctx.remaining, 3),
        "cancelled": ctx.cancel.is_set(),
        "stopped_reason": ctx.stopped_reason,
        "current_stage": ctx.current_stage or "<between stages>",
        "stage_sum": round(ctx.top_elapsed, 3),
        "unaccounted": round(ctx.elapsed - ctx.top_elapsed, 3),
        "stages": list(ctx.stages),
    }
