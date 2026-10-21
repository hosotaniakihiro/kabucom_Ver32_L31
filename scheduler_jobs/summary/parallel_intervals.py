# ============================================================
# File   : core/startup/summary_parallel_intervals_runtime_patch.py
# Version: Ver12-MAIN-WAIT-PUSH-1M-ONLY
# ------------------------------------------------------------
# 1分・3分・5分サマリーを並列実行する runtime patch。
#
# Ver10 Fix:
#   ✔ CPU高止まり対策として、既定で 1m/3m/5m 全足強制をしない。
#   ✔ main.py は親tickを詰まらせないが、長足BGも既定では増やさない。
#   ✔ workers/bg_workers 既定を 1 にしてスレッド増殖を抑える。
# Ver11 Fix:
#   ✔ main.py / entry-only process では 90秒 timeout を引き継がず、
#     25秒で親tickを返す。summary_parent_tick 80秒化を防ぐ。
# Ver12 Fix:
#   ✔ main.py / entry-only process では PUSH同期待ち対象を既定で1分足だけにする。
#   ✔ 3分/5分は raw fallback / cache / DB fallback から読む前提にして、
#     5分足待ちによる親tick timeoutを防ぐ。
# ============================================================

# ------------------------------------------------------------
# 旧 core/startup/summary_parallel_intervals_runtime_patch.py から移設。
#
# 旧 install() は scheduler_jobs.summary の time_locked_runner / runners /
# scheduler の 3 モジュールへ run_time_locked_summary_jobs を差し替えていたが、
# runners.py:41 と scheduler.py:76 は time_locked_runner から from-import して
# いるので、本体 (time_locked_runner.run_time_locked_summary_jobs) が
# ここの run_parallel_interval_tick() を呼ぶ形にすれば 3 つとも追従する。
#
# env 既定値 9 件は apply_env_defaults() が担う。
# ------------------------------------------------------------
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_VERSION_TAG = "v12"
_INSTALLED = False
_ORIG_TIME_LOCKED = None
_RUNNING_LOCK = threading.RLock()
_RUNNING_KEYS: set[str] = set()
_BG_RUNNING_KEYS: set[str] = set()

# 実行中のジョブ (source, interval) -> 投入時刻。
# 親 tick は timeout=25s で待つのをやめて executor を reset するが、
# 実行中の future はキャンセルできないのでワーカーは走り続ける。
# ジョブが 60 秒 (tick 間隔) を超えると毎分1本ずつ積み上がり、
# 2026-08-17 は大引け後に 1 本 7,000 秒超・timeout 322/324 tick まで悪化した。
# 滞留したワーカーが summary DB を掴み続けるため
# WAL チェックポイントが PASSIVE で skip busy を繰り返して WAL が 373MB まで伸び、
# SMB 越しの読み取りが "file is not a database" を 142 件出す連鎖になっていた。
# 同じ (source, interval) のジョブが走っている間は次を投げない。
class _InflightEntry:
    """1 つの (source, interval) ジョブの実体を追跡する。

    token は acquire ごとに一意。古いワーカーの finally が
    新しい世代の登録を消さないようにするために使う。
    """

    __slots__ = ("token", "started_ts", "started_at", "owner", "future", "thread",
                 "ctx", "cancel_requested")

    def __init__(self, token: int, started_ts: float, started_at: Any, owner: str) -> None:
        self.token = token
        self.started_ts = started_ts
        self.started_at = started_at
        self.owner = owner
        self.future: Any = None
        self.thread: Any = None
        # P0-1: stage_tracker の WorkerCtx。これを通じて worker へ cancel を届ける。
        self.ctx: Any = None
        self.cancel_requested = False


_INFLIGHT: dict[tuple[str, int], "_InflightEntry"] = {}
_INFLIGHT_SEQ = 0


def _inflight_try_acquire(source: str, interval: int, now_ts: float, *, owner: str = "") -> int | None:
    """取得できたら token を返す。すでに実行中なら None。"""
    global _INFLIGHT_SEQ
    key = (str(source), int(interval))
    # _now_naive() は内部で import するのでロック内では呼ばない。
    # (import ロックと _RUNNING_LOCK の獲得順が逆転しうる)
    started_at = _now_naive()
    with _RUNNING_LOCK:
        if key in _INFLIGHT:
            return None
        _INFLIGHT_SEQ += 1
        token = _INFLIGHT_SEQ
        _INFLIGHT[key] = _InflightEntry(token, now_ts, started_at, owner)
        return token


def _inflight_attach_future(source: str, interval: int, token: int, future: Any) -> None:
    key = (str(source), int(interval))
    with _RUNNING_LOCK:
        e = _INFLIGHT.get(key)
        if e is not None and e.token == token:
            e.future = future


def _inflight_mark_thread(source: str, interval: int, token: int) -> None:
    """ワーカーが実際に走り始めたことを記録する。"""
    key = (str(source), int(interval))
    with _RUNNING_LOCK:
        e = _INFLIGHT.get(key)
        if e is not None and e.token == token:
            e.thread = threading.current_thread()


def _inflight_attach_ctx(source: str, interval: int, token: int | None, ctx: Any) -> None:
    """ワーカーの stage_tracker ctx を登録する (P0-1)。

    これがあると parent 側から「降りろ」を届けられる。
    """
    if token is None or ctx is None:
        return
    key = (str(source), int(interval))
    with _RUNNING_LOCK:
        e = _INFLIGHT.get(key)
        if e is not None and e.token == token:
            e.ctx = ctx


def _inflight_request_cancel(source: str, interval: int, *, reason: str,
                             generation: Any = None) -> bool:
    """走っているワーカーへ協調キャンセルを要求する (P0-1)。

    Thread は殺さない。ワーカーが次のチェックポイントで自分から return する。
    scheduler の登録だけ消してワーカーを放置するのは禁止なので、
    「回収」ではなく「本人に降りてもらう」形にする。
    """
    key = (str(source), int(interval))
    with _RUNNING_LOCK:
        e = _INFLIGHT.get(key)
        if e is None:
            return False
        if generation is not None and e.token != generation:
            return False
        ctx = e.ctx
        already = e.cancel_requested
        e.cancel_requested = True
        token = e.token
        owner = e.owner
        age = time.time() - e.started_ts

    if ctx is None:
        logger.warning(
            "[SUMMARY PARALLEL CANCEL] no ctx to cancel key=%s:%s generation=%s owner=%s "
            "age=%.1fs reason=%s (worker は stage_tracker を持っていない)",
            source, interval, token, owner, age, reason,
        )
        return False

    _st = _stage_tracker()
    if _st is None:
        return False
    try:
        _st.cancel(ctx, reason=reason)
    except Exception:
        logger.debug("[SUMMARY PARALLEL CANCEL] cancel failed key=%s:%s", source, interval, exc_info=True)
        return False
    logger.warning(
        "[SUMMARY PARALLEL CANCEL] requested key=%s:%s generation=%s owner=%s age=%.1fs "
        "reason=%s already=%s (協調キャンセル; Thread は殺さない)",
        source, interval, token, owner, age, reason, already,
    )
    return True


def _inflight_release(source: str, interval: int, token: int | None = None) -> None:
    """token 一致時のみ解放する。

    token=None は acquire していない呼び出し元なので何もしない。
    これがないと、古いワーカーの finally が新しい世代の登録を消してしまう。
    """
    if token is None:
        return
    key = (str(source), int(interval))
    with _RUNNING_LOCK:
        e = _INFLIGHT.get(key)
        if e is not None and e.token == token:
            _INFLIGHT.pop(key, None)


def _inflight_age(source: str, interval: int, now_ts: float) -> float:
    with _RUNNING_LOCK:
        e = _INFLIGHT.get((str(source), int(interval)))
    return (now_ts - e.started_ts) if e is not None else 0.0


def inflight_snapshot() -> dict[str, float]:
    """診断用: 実行中ジョブと経過秒。"""
    import time as _t

    now = _t.perf_counter()
    with _RUNNING_LOCK:
        return {"%s:%s" % (s, i): round(now - e.started_ts, 1) for (s, i), e in _INFLIGHT.items()}


def _inflight_diagnose(source: str, interval: int, now_ts: float) -> dict[str, Any]:
    """実体 (future / thread) の状態を返す。

    「worker がまだ生きている」「future だけ残っている」
    「登録だけ残っている」を区別するための情報を全部載せる。
    """
    key = (str(source), int(interval))
    with _RUNNING_LOCK:
        e = _INFLIGHT.get(key)
        if e is None:
            return {"key": "%s:%s" % key, "state": "absent"}
        fut = e.future
        th = e.thread
        info: dict[str, Any] = {
            "key": "%s:%s" % key,
            "generation": e.token,
            "owner": e.owner,
            "started_at": str(e.started_at),
            "age": round(now_ts - e.started_ts, 1),
            "cancel_requested": bool(e.cancel_requested),
        }
        ctx = e.ctx

    # future / thread の問い合わせはロック外で行う
    if fut is None:
        info.update({"future": "not_submitted", "done": None, "running": None, "cancelled": None})
    else:
        try:
            info.update({
                "future": "attached",
                "done": bool(fut.done()),
                "running": bool(fut.running()),
                "cancelled": bool(fut.cancelled()),
            })
        except Exception:
            info.update({"future": "query_failed", "done": None, "running": None, "cancelled": None})

    if th is None:
        info.update({"thread": None, "thread_alive": None})
    else:
        try:
            info.update({"thread": th.name, "thread_alive": bool(th.is_alive())})
        except Exception:
            info.update({"thread": "query_failed", "thread_alive": None})

    # P0-1: どのステージに居るのかを診断に出す。
    if ctx is not None:
        try:
            info.update({
                "stages": ",".join("%s=%.2f" % (n, s) for n, s in getattr(ctx, "stages", [])) or "-",
                "ctx_cancelled": bool(ctx.cancel.is_set()),
                "stopped_reason": getattr(ctx, "stopped_reason", None),
                "elapsed": round(getattr(ctx, "elapsed", 0.0), 1),
            })
        except Exception:
            logger.debug("[SUMMARY PARALLEL] ctx snapshot failed", exc_info=True)

    return info


def _inflight_stale_reclaim_sec() -> float:
    """この秒数を超えた登録は実体を確認して回収を検討する。"""
    base = _env_float("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", 25.0)
    return max(30.0, _env_float("SUMMARY_PARALLEL_INFLIGHT_STALE_SEC", base * 2.0))


def _inflight_try_reclaim(source: str, interval: int, info: dict[str, Any]) -> str | None:
    """実体が死んでいる登録だけ回収する。戻り値は回収理由 (しなければ None)。

    ワーカーが本当に走っている間は絶対に回収しない。
    同じジョブを二重起動しないための一線。
    """
    if info.get("state") == "absent":
        return None

    age = float(info.get("age") or 0.0)
    stale_sec = _inflight_stale_reclaim_sec()
    reason = None

    if info.get("cancelled"):
        reason = "future_cancelled"
    elif info.get("done"):
        reason = "future_done_but_not_released"
    elif (info.get("future") == "not_submitted" and age >= stale_sec
          and not info.get("thread_alive")):
        # thread_alive の確認を必ず入れる。future の attach に失敗しただけで
        # ワーカー本体は走っている、という状態で回収すると二重起動になる。
        reason = "never_submitted"
    elif (info.get("running") is False and info.get("done") is False and age >= stale_sec
          and not info.get("thread_alive")):
        # queue に残ったまま executor が差し替えられた等。走っていないので回収してよい。
        reason = "queued_but_executor_gone"
    elif info.get("thread_alive") is False and age >= stale_sec:
        reason = "worker_thread_dead"

    if reason is None:
        return None

    key = (str(source), int(interval))
    with _RUNNING_LOCK:
        e = _INFLIGHT.get(key)
        if e is None or e.token != info.get("generation"):
            # 別世代に入れ替わっていたら触らない
            return None
        _INFLIGHT.pop(key, None)
    return reason


def _skip_when_busy_enabled() -> bool:
    return _env_bool("SUMMARY_PARALLEL_SKIP_WHEN_BUSY", True)
_EXECUTOR: ThreadPoolExecutor | None = None
_BG_EXECUTOR: ThreadPoolExecutor | None = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return max(1, int(float(v)))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return max(1.0, float(v))
    except Exception:
        return float(default)


def _setdefault_env(name: str, value: str) -> None:
    try:
        cur = os.getenv(name)
        if cur is None or str(cur).strip() == "":
            os.environ[name] = str(value)
            logger.warning("[SUMMARY PARALLEL] env default set %s=%s", name, value)
    except Exception:
        pass


def _force_env(name: str, value: str, *, reason: str) -> None:
    try:
        old = os.getenv(name)
        if str(old or "").strip() != str(value):
            os.environ[name] = str(value)
            logger.warning("[SUMMARY PARALLEL] env forced %s=%s old=%s reason=%s", name, value, old, reason)
    except Exception:
        pass


def _is_main_entry_only_process() -> bool:
    try:
        if _env_bool("AUTOSTOCK_MAIN_DATABASE_PROCESS", False):
            return False
        if _env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False):
            return False
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False) or role in {"entry_only", "main_entry_only", "read_only", "no_save"}
    except Exception:
        return False


def _main_tick_timeout_cap_enabled() -> bool:
    # main.py は発注・PUSH監視を止めないことを優先する。長い集計は main_database.py 側。
    return _is_main_entry_only_process() and not _env_bool("SUMMARY_MAIN_ALLOW_LONG_TICK", False)


def _ensure_timeout_min() -> None:
    try:
        if _main_tick_timeout_cap_enabled():
            cap = _env_float("SUMMARY_MAIN_TICK_TIMEOUT_CAP_SEC", 25.0)
            cur_raw = os.getenv("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC")
            cur = float(cur_raw) if cur_raw is not None and str(cur_raw).strip() != "" else cap
            if cur > cap:
                os.environ["SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"] = str(int(cap) if float(cap).is_integer() else cap)
                logger.warning(
                    "[SUMMARY PARALLEL] timeout capped old=%s new=%s reason=main_entry_tick_latency",
                    cur_raw,
                    os.environ.get("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"),
                )
            min_raw = os.getenv("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC")
            min_sec = float(min_raw) if min_raw is not None and str(min_raw).strip() != "" else cap
            if min_sec > cap:
                os.environ["SUMMARY_PARALLEL_TIMEOUT_MIN_SEC"] = str(int(cap) if float(cap).is_integer() else cap)
                logger.warning(
                    "[SUMMARY PARALLEL] timeout min capped old=%s new=%s reason=main_entry_tick_latency",
                    min_raw,
                    os.environ.get("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC"),
                )
            return

        min_sec = _env_float("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC", 30.0)
        cur_raw = os.getenv("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC")
        cur = float(cur_raw) if cur_raw is not None and str(cur_raw).strip() != "" else min_sec
        if cur < min_sec:
            os.environ["SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"] = str(int(min_sec) if float(min_sec).is_integer() else min_sec)
            logger.warning("[SUMMARY PARALLEL] timeout raised old=%s new=%s reason=min_timeout_for_summary", cur_raw, os.environ.get("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"))
    except Exception:
        os.environ["SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"] = "25" if _main_tick_timeout_cap_enabled() else "30"
        logger.warning("[SUMMARY PARALLEL] timeout fallback set %s", os.environ.get("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"))


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    workers = _env_int("SUMMARY_PARALLEL_INTERVAL_WORKERS", 1)
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="summary-parallel-interval")
    return _EXECUTOR


def _bg_executor() -> ThreadPoolExecutor:
    global _BG_EXECUTOR
    workers = _env_int("SUMMARY_PUSH_BG_INTERVAL_WORKERS", 1)
    if _BG_EXECUTOR is None:
        _BG_EXECUTOR = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="summary-push-bg-interval")
    return _BG_EXECUTOR


def _now_naive() -> dt.datetime:
    try:
        from scheduler_jobs.summary.time_utils import now_naive
        return now_naive()
    except Exception:
        return dt.datetime.now()


def _latest_dt_str(df: Any) -> str | None:
    try:
        from scheduler_jobs.summary.display_prepare import latest_dt_str
        return latest_dt_str(df)
    except Exception:
        return None


def _is_market_session(now: dt.datetime) -> bool:
    try:
        from scheduler_jobs.summary.time_utils import is_market_session
        return bool(is_market_session(now))
    except Exception:
        return True


def _base_time_locked_targets(now: dt.datetime, in_session: bool) -> list[int]:
    try:
        from scheduler_jobs.summary.time_utils import resolve_target_intervals
        targets = list(resolve_target_intervals(now) or [])
    except Exception:
        targets = []

    if not targets and not in_session:
        try:
            from scheduler_jobs.summary.time_locked_runner import closed_market_display_targets
            targets = list(closed_market_display_targets(now) or [])
        except Exception:
            targets = [1]
            try:
                if int(now.minute) % 3 == 0:
                    targets.append(3)
                if int(now.minute) % 5 == 0:
                    targets.append(5)
            except Exception:
                pass

    clean = sorted({int(x) for x in targets if int(x) in {1, 3, 5}})
    return clean or [1]


def _force_all_targets_enabled() -> bool:
    return _env_bool("SUMMARY_PARALLEL_FORCE_1_3_5", False)


def _push_all_intervals_enabled() -> bool:
    return _env_bool("SUMMARY_PUSH_DISPLAY_ALL_INTERVALS", False)


def _push_bg_all_intervals_enabled() -> bool:
    return _env_bool("SUMMARY_PUSH_BG_ALL_INTERVALS", False)


def _push_bg_long_intervals_enabled() -> bool:
    return _env_bool("SUMMARY_PUSH_BG_LONG_INTERVALS", False)


def _main_wait_push_1m_only_enabled() -> bool:
    # main.py は発注判断優先。5分足は使うが、同期完了は待たない。
    return _is_main_entry_only_process() and _env_bool("SUMMARY_MAIN_WAIT_PUSH_1M_ONLY", True)


def _resolve_targets(now: dt.datetime, in_session: bool) -> tuple[list[int], list[int]]:
    base = _base_time_locked_targets(now, in_session)
    force_all = _force_all_targets_enabled()

    ranking_targets = list(base)
    if in_session and force_all:
        ranking_targets = [1, 3, 5]

    push_targets = [1, 3, 5] if _push_all_intervals_enabled() else list(base)
    push_targets = sorted({int(x) for x in push_targets if int(x) in {1, 3, 5}}) or [1]
    ranking_targets = sorted({int(x) for x in ranking_targets if int(x) in {1, 3, 5}}) or [1]
    return push_targets, ranking_targets


def _split_push_wait_and_bg_core(push_targets: list[int], *, in_session: bool) -> tuple[list[int], list[int]]:
    targets = sorted({int(x) for x in push_targets if int(x) in {1, 3, 5}})

    # Ver12: main.py / entry-only では 5分足を同期で待たない。
    # 3分/5分は summary_mtf_push_raw_fallback / cache / DB fallback 側で読む。
    # 明示的にBGを有効化していない限り、長足計算ジョブ自体もmain.pyからは投げない。
    if _main_wait_push_1m_only_enabled():
        wait = [1]
        long_targets = [x for x in targets if int(x) in (3, 5)]
        if _env_bool("SUMMARY_MAIN_BG_LONG_PUSH_ENABLED", False):
            if (not bool(in_session)) and _env_bool("SUMMARY_PUSH_SKIP_BG_WHEN_OUT_OF_SESSION", True):
                return wait, []
            return wait, long_targets
        if long_targets:
            logger.warning(
                "[SUMMARY PARALLEL] main wait limited to 1m; long push targets not waited targets=%s wait=%s skipped_long=%s reason=main_entry_latency",
                targets,
                wait,
                long_targets,
            )
        return wait, []

    if _is_main_entry_only_process() and _env_bool("SUMMARY_MAIN_BG_PUSH_ENABLED", False):
        if (not bool(in_session)) and _env_bool("SUMMARY_PUSH_SKIP_BG_WHEN_OUT_OF_SESSION", True):
            return [], []
        return [], targets

    if _push_bg_all_intervals_enabled():
        return [], targets

    if _push_bg_long_intervals_enabled():
        wait = [x for x in targets if int(x) == 1]
        bg = [x for x in targets if int(x) in (3, 5)]
        return (wait or [1]), bg

    return targets, []


def _publish_skipped_long_targets(push_targets, wait_push_targets, bg_push_targets, *, now) -> None:
    """wait にも bg にも入らなかった長足 (3/5) を summary DB から publish する。

    main.py は長足を自分で計算しない方針なので、計算済みのものを
    データ側 (main_database が書いた summaryYYYYMMDD.db) から読む。
    黙って捨てると merged tf=3 / tf=5 が rows=0 のままになる。
    """
    try:
        handled = {int(x) for x in (list(wait_push_targets or []) + list(bg_push_targets or []))}
        dropped = sorted({int(x) for x in (push_targets or []) if int(x) in (3, 5)} - handled)
        if not dropped:
            return
        from scheduler_jobs.summary.long_tf_db_publish import start_long_tf_db_publish_async

        started = start_long_tf_db_publish_async(tuple(dropped), reason="tick_skipped_long:%s" % (now,))
        logger.warning(
            "[SUMMARY PARALLEL] long targets not computed in main.py -> summary DB から publish "
            "dropped=%s started=%s (main.py は長足を計算しない方針)",
            dropped, started,
        )
    except Exception:
        logger.exception("[SUMMARY PARALLEL] long tf db publish dispatch failed push_targets=%s", push_targets)


def _stage_tracker():
    """P0-2: ステージ別 elapsed / deadline / 協調キャンセル。取得できなければ None。"""
    try:
        from scheduler_jobs.summary import stage_tracker as _st
        return _st
    except Exception:
        logger.debug("[SUMMARY PARALLEL] stage_tracker import failed", exc_info=True)
        return None


def _log_freshness_timeline(reason: str) -> None:
    """P0-6: now / websocket / memory push / push db / raw / 1m OHLC /
    history / merged の最新時刻を 1 行にまとめて出す。

    どの段で鮮度が落ちたのかをログから直接読めるようにするための診断のみ。
    鮮度判定 (STALE DROP 等) の挙動は変更しない。
    """
    try:
        from scheduler_jobs.summary import freshness_timeline as _ft
        _ft.log_timeline(reason)
    except Exception:
        logger.debug("[SUMMARY PARALLEL] freshness timeline failed reason=%s", reason, exc_info=True)


def _job_one_source(*, source: str, interval: int, now: dt.datetime, display: bool, run_entry: bool, inflight_token: int | None = None) -> tuple[str, int, pd.DataFrame]:
    t0 = time.perf_counter()
    if inflight_token is not None:
        # 実際に走り始めたワーカーを記録する (診断で thread_alive を見るため)
        _inflight_mark_thread(source, interval, inflight_token)
    # P0-2: ワーカー本体に予算を持たせ、どのステージで止まったかを必ず出す。
    # scheduler 側の inflight メタデータを clear するだけでは
    # ワーカーが走り続けてしまうため、ワーカー自身が降りられるようにする。
    _st = _stage_tracker()
    # now は None で呼ばれることがある (テスト・一部の内部経路)。
    # 診断用の owner 文字列のために本処理を落とさない。
    try:
        owner = "tick:%s" % now.strftime("%Y%m%d%H%M%S")
    except Exception:
        owner = "tick:unknown"
    # P0-7: worker() は SummaryWorkerCancelled を握り潰して with を抜けるので、
    # cancel されたときでも戻り値の形 (source, interval, DataFrame) を保つ。
    result = (source, int(interval), pd.DataFrame())
    try:
        if _st is None:
            return _job_one_source_core(
                source=source, interval=interval, now=now, display=display, run_entry=run_entry, t0=t0
            )
        with _st.worker("%s:%s" % (source, interval), generation=inflight_token, owner=owner) as _ctx:
            # P0-1: parent から cancel を届けられるようにする。
            _inflight_attach_ctx(source, interval, inflight_token, _ctx)
            with _st.stage("freshness_timeline"):
                _log_freshness_timeline("%s:%s" % (source, interval))
            result = _job_one_source_core(
                source=source, interval=interval, now=now, display=display, run_entry=run_entry, t0=t0
            )
        return result
    finally:
        # 親 tick が timeout で先に抜けても、ワーカーはここまで来て必ず解放する。
        # token 一致時のみ解放するので、古いワーカーが新しい世代を消すことはない。
        _inflight_release(source, interval, inflight_token)


def _job_one_source_core(
    *, source: str, interval: int, now: dt.datetime, display: bool, run_entry: bool, t0: float
) -> tuple[str, int, pd.DataFrame]:
    try:
        from scheduler_jobs.summary.runner_core import job_summary, job_ranking_summary
        if source == "push":
            logger.info("[SUMMARY PARALLEL] job start source=push interval=%s now=%s display=%s run_entry=%s", interval, now, display, run_entry)
            df = job_summary(int(interval), display=display, now=now, run_entry=run_entry)
        else:
            logger.info("[SUMMARY PARALLEL] job start source=ranking interval=%s now=%s display=%s run_entry=%s", interval, now, display, run_entry)
            df = job_ranking_summary(int(interval), display=display, now=now, run_entry=False)
        rows = len(df) if isinstance(df, pd.DataFrame) else 0
        logger.warning("[SUMMARY PARALLEL] job done source=%s interval=%s rows=%s latest_dt=%s elapsed=%.3fs", source, interval, rows, _latest_dt_str(df) if isinstance(df, pd.DataFrame) else None, time.perf_counter() - t0)
        return source, interval, df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception as e:
        logger.exception("[SUMMARY PARALLEL] job failed source=%s interval=%s now=%s err=%s elapsed=%.3fs", source, interval, now, e, time.perf_counter() - t0)
        return source, interval, pd.DataFrame()


def _submit_bg_push_interval_core(*, interval: int, now: dt.datetime, display: bool, run_entry: bool) -> None:
    bg_key = f"{now.strftime('%Y%m%d%H%M')}:push:{int(interval)}"
    with _RUNNING_LOCK:
        if bg_key in _BG_RUNNING_KEYS:
            logger.warning("[SUMMARY PARALLEL] bg push skipped reason=already_running key=%s", bg_key)
            return
        _BG_RUNNING_KEYS.add(bg_key)

    def _task() -> None:
        try:
            logger.warning("[SUMMARY PARALLEL] bg push start key=%s interval=%s now=%s display=%s run_entry=%s", bg_key, interval, now, display, run_entry)
            _job_one_source(source="push", interval=int(interval), now=now, display=display, run_entry=run_entry)
        finally:
            with _RUNNING_LOCK:
                _BG_RUNNING_KEYS.discard(bg_key)
            logger.warning("[SUMMARY PARALLEL] bg push done key=%s interval=%s now=%s", bg_key, interval, now)

    _bg_executor().submit(_task)
    logger.warning("[SUMMARY PARALLEL] bg push submitted key=%s interval=%s now=%s", bg_key, interval, now)


def _patched_run_time_locked_summary_jobs_legacy(*, now: Optional[dt.datetime] = None, run_push: bool = True, run_ranking: bool = True, display: bool = True, run_entry: bool = True) -> dict[str, dict[int, pd.DataFrame]]:
    if not _env_bool("SUMMARY_PARALLEL_INTERVALS_ENABLED", True):
        if callable(_ORIG_TIME_LOCKED):
            return _ORIG_TIME_LOCKED(now=now, run_push=run_push, run_ranking=run_ranking, display=display, run_entry=run_entry)
        return {"push": {}, "ranking": {}}

    _ensure_timeout_min()
    n = (now or _now_naive()).replace(microsecond=0)
    in_session = _is_market_session(n)
    push_targets, ranking_targets = _resolve_targets(n, in_session)
    wait_push_targets, bg_push_targets = _split_push_wait_and_bg(push_targets, in_session=in_session)
    out: dict[str, dict[int, pd.DataFrame]] = {"push": {}, "ranking": {}}

    key = n.strftime("%Y%m%d%H%M")
    with _RUNNING_LOCK:
        if key in _RUNNING_KEYS:
            logger.warning("[SUMMARY PARALLEL] skipped reason=previous_same_tick_running key=%s push_targets=%s wait_push_targets=%s bg_push_targets=%s ranking_targets=%s", key, push_targets, wait_push_targets, bg_push_targets, ranking_targets)
            return out
        _RUNNING_KEYS.add(key)

    t0 = time.perf_counter()
    futures = []
    timeout = _env_float("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", 30.0)
    try:
        logger.warning(
            "[SUMMARY PARALLEL] tick start now=%s push_targets=%s wait_push_targets=%s bg_push_targets=%s ranking_targets=%s run_push=%s run_ranking=%s display=%s run_entry=%s in_session=%s workers=%s bg_workers=%s timeout=%.1f force_1_3_5=%s push_all_intervals=%s push_bg_all=%s push_bg_long=%s main_entry_only=%s main_wait_1m_only=%s",
            n, push_targets, wait_push_targets, bg_push_targets, ranking_targets, run_push, run_ranking, display, run_entry,
            in_session, _env_int("SUMMARY_PARALLEL_INTERVAL_WORKERS", 1), _env_int("SUMMARY_PUSH_BG_INTERVAL_WORKERS", 1), timeout,
            _force_all_targets_enabled(), _push_all_intervals_enabled(), _push_bg_all_intervals_enabled(),
            _push_bg_long_intervals_enabled(), _is_main_entry_only_process(), _main_wait_push_1m_only_enabled(),
        )
        ex = _executor()

        if run_push:
            for interval in wait_push_targets:
                futures.append(ex.submit(_job_one_source, source="push", interval=int(interval), now=n, display=display, run_entry=bool(run_entry) and bool(in_session)))
            for interval in bg_push_targets:
                _submit_bg_push_interval(interval=int(interval), now=n, display=display, run_entry=bool(run_entry) and bool(in_session))
            _publish_skipped_long_targets(push_targets, wait_push_targets, bg_push_targets, now=n)

        if run_ranking and in_session and _env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True):
            for interval in ranking_targets:
                futures.append(ex.submit(_job_one_source, source="ranking", interval=int(interval), now=n, display=display, run_entry=False))
        elif run_ranking and not in_session:
            logger.info("[SUMMARY PARALLEL] ranking skipped reason=closed_market_or_lunch targets=%s", ranking_targets)
        elif run_ranking:
            logger.info("[SUMMARY PARALLEL] ranking skipped reason=parallel_ranking_disabled targets=%s", ranking_targets)

        if not futures:
            logger.warning("[SUMMARY PARALLEL] tick no wait futures now=%s bg_push_targets=%s elapsed=%.3fs", n, bg_push_targets, time.perf_counter() - t0)
            return out

        done_count = 0
        try:
            for fut in as_completed(futures, timeout=timeout):
                source, interval, df = fut.result()
                out.setdefault(source, {})[int(interval)] = df
                done_count += 1
        except FuturesTimeoutError:
            logger.error("[SUMMARY PARALLEL] tick timeout now=%s timeout=%.1fs done=%s total=%s wait_push_targets=%s bg_push_targets=%s ranking_targets=%s", n, timeout, done_count, len(futures), wait_push_targets, bg_push_targets, ranking_targets)
            for fut in futures:
                try:
                    fut.cancel()
                except Exception:
                    pass

        logger.warning("[SUMMARY PARALLEL] tick done now=%s push_targets=%s wait_push_targets=%s bg_push_targets=%s ranking_targets=%s push_done=%s ranking_done=%s elapsed=%.3fs", n, push_targets, wait_push_targets, bg_push_targets, ranking_targets, sorted(out.get("push", {}).keys()), sorted(out.get("ranking", {}).keys()), time.perf_counter() - t0)
        return out
    finally:
        with _RUNNING_LOCK:
            _RUNNING_KEYS.discard(key)





__all__ = ["install"]


# ============================================================
# main.py で summary PUSH BG をスキップする
# ------------------------------------------------------------
# 旧 core/startup/main_skip_summary_push_bg_patch.py をこのホストへ統合。
# (このモジュール自体がまだパッチなので「本文化」ではなくホストへの統合)
#
# main.py プロセスでは wait/bg の push target を空にし、bg push interval の
# 投入もスキップする。無効化は AUTOSTOCK_MAIN_SKIP_SUMMARY_PUSH_BG=0。
# 判定ヘルパは旧パッチのものを _msk_* として移植した。
# ============================================================

os.environ.setdefault("AUTOSTOCK_MAIN_SKIP_SUMMARY_PUSH_BG", "1")

def _msk_env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _msk_is_main_py() -> bool:
    try:
        if _msk_env_bool("AUTOSTOCK_MAIN_DATABASE_PROCESS", False):
            return False
        if _msk_env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False):
            return False
        argv0 = Path(str(sys.argv[0] or "")).name.lower()
        if argv0 == "main.py":
            return True
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return role in {"entry_only", "main_entry_only", "read_only", "no_save"} or _msk_env_bool("SUMMARY_MAIN_ENTRY_ONLY", False)
    except Exception:
        return False


def _msk_enabled() -> bool:
    return _msk_env_bool("AUTOSTOCK_MAIN_SKIP_SUMMARY_PUSH_BG", True) and _msk_is_main_py()


def _split_push_wait_and_bg(push_targets: list[int], *, in_session: bool) -> tuple[list[int], list[int]]:
    if _msk_enabled():
        try:
            logger.warning(
                "[MAIN SKIP SUMMARY PUSH BG] skip wait/bg push targets in main.py targets=%s in_session=%s",
                push_targets, in_session,
            )
        except Exception:
            pass
        return [], []
    return _split_push_wait_and_bg_core(push_targets, in_session=in_session)


def _submit_bg_push_interval_legacy(*, interval, now, display: bool, run_entry: bool) -> None:
    if _msk_enabled():
        logger.warning(
            "[MAIN SKIP SUMMARY PUSH BG] skipped bg push interval=%s now=%s display=%s run_entry=%s",
            interval, now, display, run_entry,
        )
        return None
    try:
        return _submit_bg_push_interval_core(interval=interval, now=now, display=display, run_entry=run_entry)
    except Exception:
        logger.exception("[MAIN SKIP SUMMARY PUSH BG] original bg submit failed")
    return None


# ============================================================
# SUMMARY PUSH BG DUE GUARD
# (旧 core/startup/summary_push_bg_due_interval_guard_patch.py を本文化)
# ------------------------------------------------------------
# 3m/5m は due のときだけ、1m は専用スレッドで、同一 interval の
# 多重起動は stale 判定で抑止する。場外は main.py で丸ごとスキップ。
#
# **重要**: 旧パッチのラッパーは元関数 (_submit_bg_push_interval) を
# 一度も呼ばず、_job_one_source / _bg_executor を直接叩く完全置換だった。
# つまりこのモジュールに前回畳んだ _msk_enabled() の main-skip 層は
# 実行時には到達していなかった。上の _submit_bg_push_interval_legacy が
# その旧経路で、現在は呼び出し元が無い。
#
# 旧 install() の _install_display_first_patch / _install_seed_fast_patch は
# どちらも既に本文化済みで True を返すだけだった。
# ============================================================

_BGD_RUNNING_BY_INTERVAL: dict[int, tuple[float, str]] = {}
_BGD_LOCK = threading.RLock()
_BGD_SESSION_SKIP_LOGGED: dict[str, float] = {}


def _bgd_env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _bgd_env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return max(1.0, float(raw))
    except Exception:
        return float(default)


def _bgd_is_main_py_context() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in argv:
            return False
        if any(x in argv for x in (
            "summary_database_runner.py",
            "yahoo_complement_runner.py",
            "ranking_collector_runner.py",
            "push_receiver_runner.py",
            "data_collectors_runner.py",
        )):
            return False
        if any(os.getenv(k) == "1" for k in (
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
        )):
            return False
        return "main.py" in argv
    except Exception:
        return False


def _bgd_is_market_session(now: dt.datetime) -> bool:
    """JP cash session approximation for main.py background work."""
    try:
        t = now.time()
        am_start = dt.time(9, 0)
        am_end = dt.time(11, 30)
        pm_start = dt.time(12, 30)
        pm_end = dt.time(15, 30)
        return (am_start <= t < am_end) or (pm_start <= t < pm_end)
    except Exception:
        return True


def _bgd_should_skip_main_outside_session(now: dt.datetime, interval: int) -> tuple[bool, str]:
    try:
        if not _bgd_env_bool("SUMMARY_PUSH_BG_SKIP_OUTSIDE_SESSION_IN_MAIN", True):
            return False, "disabled"
        if not _bgd_is_main_py_context():
            return False, "not_main"
        if _bgd_is_market_session(now):
            return False, "in_session"
        if _bgd_env_bool("SUMMARY_PUSH_BG_ALLOW_OUTSIDE_SESSION_IN_MAIN", False):
            return False, "allow_env"
        return True, f"outside_session interval={interval} now={now}"
    except Exception:
        return False, "check_failed"


def _bgd_log_session_skip_once(key: str, msg: str, *args) -> None:
    try:
        now_ts = time.time()
        interval = _bgd_env_float("SUMMARY_PUSH_BG_SESSION_SKIP_LOG_INTERVAL_SEC", 60.0)
        last = float(_BGD_SESSION_SKIP_LOGGED.get(key, 0.0) or 0.0)
        if now_ts - last >= interval:
            _BGD_SESSION_SKIP_LOGGED[key] = now_ts
            logger.warning(msg, *args)
    except Exception:
        logger.warning(msg, *args)


def _bgd_stale_sec_for_interval(interval: int) -> float:
    iv = int(interval)
    if iv == 1:
        return _bgd_env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC_1M", 60.0)
    if iv == 3:
        return _bgd_env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC_3M", _bgd_env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC", 240.0))
    if iv == 5:
        return _bgd_env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC_5M", _bgd_env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC", 240.0))
    return _bgd_env_float("SUMMARY_PUSH_BG_INTERVAL_STALE_SEC", 240.0)


def _bgd_is_due(interval: int, now: dt.datetime) -> bool:
    try:
        iv = int(interval)
        if iv <= 1:
            return True
        return int(now.minute) % iv == 0
    except Exception:
        return True


def _bgd_run_task_direct_thread(task, *, bg_key: str, interval: int) -> bool:
    try:
        th = threading.Thread(
            target=task,
            name=f"summary-push-{int(interval)}m-{bg_key}",
            daemon=True,
        )
        th.start()
        logger.warning(
            "[SUMMARY PUSH BG DUE GUARD] bg push dedicated thread started key=%s interval=%s thread=%s",
            bg_key,
            interval,
            th.name,
        )
        return True
    except Exception:
        logger.exception("[SUMMARY PUSH BG DUE GUARD] dedicated thread start failed key=%s interval=%s", bg_key, interval)
        return False


def _submit_bg_push_interval(*, interval: int, now: dt.datetime, display: bool, run_entry: bool) -> None:
    iv = int(interval)

    skip_session, reason_session = _bgd_should_skip_main_outside_session(now, iv)
    if skip_session:
        _bgd_log_session_skip_once(
            f"session:{iv}",
            "[SUMMARY PUSH BG DUE GUARD] skipped reason=main_outside_session interval=%s now=%s detail=%s",
            iv,
            now,
            reason_session,
        )
        return None

    due_only = _bgd_env_bool("SUMMARY_PUSH_BG_LONG_INTERVAL_DUE_ONLY", True)
    if due_only and iv in (3, 5) and not _bgd_is_due(iv, now):
        logger.warning(
            "[SUMMARY PUSH BG DUE GUARD] skipped reason=not_due interval=%s now=%s minute=%s",
            iv,
            now,
            getattr(now, "minute", None),
        )
        return None

    stale_sec = _bgd_stale_sec_for_interval(iv)
    now_ts = time.time()
    with _BGD_LOCK:
        old = _BGD_RUNNING_BY_INTERVAL.get(iv)
        if old:
            started, key_old = old
            elapsed = now_ts - float(started)
            if elapsed < stale_sec:
                logger.warning(
                    "[SUMMARY PUSH BG DUE GUARD] skipped reason=interval_still_running interval=%s elapsed=%.1fs stale_sec=%.1fs key=%s",
                    iv,
                    elapsed,
                    stale_sec,
                    key_old,
                )
                return None

            logger.warning(
                "[SUMMARY PUSH BG DUE GUARD] stale interval running cleared interval=%s elapsed=%.1fs stale_sec=%.1fs key=%s",
                iv,
                elapsed,
                stale_sec,
                key_old,
            )
            _BGD_RUNNING_BY_INTERVAL.pop(iv, None)

        bg_key = f"{now.strftime('%Y%m%d%H%M')}:push:{iv}"
        _BGD_RUNNING_BY_INTERVAL[iv] = (now_ts, bg_key)

    def _task() -> None:
        try:
            logger.warning(
                "[SUMMARY PUSH BG DUE GUARD] bg push start key=%s interval=%s now=%s display=%s run_entry=%s stale_sec=%.1f dedicated=%s",
                bg_key,
                iv,
                now,
                display,
                run_entry,
                stale_sec,
                iv == 1,
            )
            _job_one_source(source="push", interval=iv, now=now, display=display, run_entry=run_entry)
        except Exception:
            logger.exception("[SUMMARY PUSH BG DUE GUARD] bg push failed key=%s interval=%s", bg_key, iv)
        finally:
            with _BGD_LOCK:
                cur = _BGD_RUNNING_BY_INTERVAL.get(iv)
                if cur and cur[1] == bg_key:
                    _BGD_RUNNING_BY_INTERVAL.pop(iv, None)
            logger.warning("[SUMMARY PUSH BG DUE GUARD] bg push done key=%s interval=%s now=%s", bg_key, iv, now)

    try:
        if iv == 1 and _bgd_env_bool("SUMMARY_PUSH_1M_DEDICATED_THREAD", True):
            ok = _bgd_run_task_direct_thread(_task, bg_key=bg_key, interval=iv)
            if not ok:
                with _BGD_LOCK:
                    cur = _BGD_RUNNING_BY_INTERVAL.get(iv)
                    if cur and cur[1] == bg_key:
                        _BGD_RUNNING_BY_INTERVAL.pop(iv, None)
            return None

        _bg_executor().submit(_task)
        logger.warning(
            "[SUMMARY PUSH BG DUE GUARD] bg push submitted key=%s interval=%s now=%s stale_sec=%.1f",
            bg_key,
            iv,
            now,
            stale_sec,
        )
    except Exception:
        with _BGD_LOCK:
            cur = _BGD_RUNNING_BY_INTERVAL.get(iv)
            if cur and cur[1] == bg_key:
                _BGD_RUNNING_BY_INTERVAL.pop(iv, None)
        logger.exception("[SUMMARY PUSH BG DUE GUARD] submit failed key=%s interval=%s", bg_key, iv)


# ============================================================
# SUMMARY PARALLEL EXECUTOR RESET
# (旧 core/startup/summary_parallel_executor_reset_patch.py を本文化)
# ------------------------------------------------------------
# tick ごとにローカル executor を立てて使い捨て、timeout 時は
# グローバル executor を作り直す。同一 tick の多重起動も抑止する。
#
# **重要**: 旧パッチのラッパーはティック処理を丸ごと再実装しており、
# 元関数 (_ORIG) を呼ぶのは SUMMARY_PARALLEL_INTERVALS_ENABLED が偽の
# ときだけ。しかもその分岐は native (_ORIG_TIME_LOCKED) を直接呼ぶ。
# 実呼び出しで確認済み。よってこのモジュール本来の
# _patched_run_time_locked_summary_jobs_legacy は実行時に到達しない
# (呼び出し元が無い)。同モジュールの _job_one_source /
# _submit_bg_push_interval 等のヘルパは下から呼ばれるので生きている。
#
# 旧 install() の watcher (2 秒 x 90 回の再適用) は、この属性を包む
# 他のパッチが無くなったため不要になり撤去した。
# 有効/無効は SUMMARY_PARALLEL_EXECUTOR_RESET_PATCH_ENABLED ではなく、
# 本文化後はこの実装が既定経路になる (旧構成でも既定 True だった)。
# ============================================================

_RST_VERSION = "V1-MAIN-TICK-LOCAL-EXECUTOR-RESET"
_RST_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_RST_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _rst_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in _RST_TRUE:
            return True
        if s in _RST_FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _rst_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return max(1, int(float(v)))
    except Exception:
        return int(default)


def _rst_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return max(1.0, float(v))
    except Exception:
        return float(default)


def _rst_use_local_executor() -> bool:
    try:
        if not _rst_env_bool("SUMMARY_PARALLEL_RESET_EXECUTOR_PER_TICK", True):
            return False
        fn = globals().get("_main_tick_timeout_cap_enabled")
        return bool(fn()) if callable(fn) else True
    except Exception:
        return True


def _rst_safe_latest(df: Any) -> str | None:
    try:
        fn = globals().get("_latest_dt_str")
        if callable(fn):
            return fn(df)
    except Exception:
        pass
    return None


def _rst_reset_global_executor(reason: str) -> None:
    try:
        ex = globals().get("_EXECUTOR")
        if ex is not None:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                ex.shutdown(wait=False)
            except Exception:
                pass
        globals()["_EXECUTOR"] = None
        logger.warning("[SUMMARY PARALLEL RESET] global executor reset reason=%s version=%s", reason, _RST_VERSION)
    except Exception:
        logger.exception("[SUMMARY PARALLEL RESET] global executor reset failed reason=%s", reason)


def run_parallel_interval_tick(*, now: Optional[dt.datetime] = None, run_push: bool = True, run_ranking: bool = True, display: bool = True, run_entry: bool = True) -> dict[str, dict[int, pd.DataFrame]]:
    if not _rst_env_bool("SUMMARY_PARALLEL_INTERVALS_ENABLED", True):
        orig = _ORIG_TIME_LOCKED
        if callable(orig):
            return orig(now=now, run_push=run_push, run_ranking=run_ranking, display=display, run_entry=run_entry)
        return {"push": {}, "ranking": {}}

    _ensure_timeout_min()
    n = (now or _now_naive()).replace(microsecond=0)
    in_session = bool(_is_market_session(n))
    push_targets, ranking_targets = _resolve_targets(n, in_session)
    wait_push_targets, bg_push_targets = _split_push_wait_and_bg(push_targets, in_session=in_session)
    out: dict[str, dict[int, pd.DataFrame]] = {"push": {}, "ranking": {}}

    key = n.strftime("%Y%m%d%H%M")
    with _RUNNING_LOCK:
        if key in _RUNNING_KEYS:
            logger.warning("[SUMMARY PARALLEL RESET] skipped reason=previous_same_tick_running key=%s", key)
            return out
        _RUNNING_KEYS.add(key)

    t0 = time.perf_counter()
    timeout = _rst_env_float("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", 25.0)
    local_ex = None
    ex = None
    futures = []
    use_local = _rst_use_local_executor()
    try:
        logger.warning(
            "[SUMMARY PARALLEL RESET] tick start now=%s push_targets=%s wait_push_targets=%s bg_push_targets=%s ranking_targets=%s run_push=%s run_ranking=%s display=%s run_entry=%s local_executor=%s timeout=%.1f version=%s",
            n, push_targets, wait_push_targets, bg_push_targets, ranking_targets, run_push, run_ranking, display, run_entry, use_local, timeout, "V-EXECUTOR-RESET",
        )
        if use_local:
            workers = _rst_env_int("SUMMARY_PARALLEL_INTERVAL_WORKERS", 1)
            local_ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="summary-main-tick-local")
            ex = local_ex
        else:
            ex = _executor()

        skip_busy = _skip_when_busy_enabled()
        now_ts = time.perf_counter()
        skipped: list[str] = []

        owner = "tick:%s" % n.strftime("%Y%m%d%H%M%S")

        def _submit(source: str, interval: int, *, run_entry_flag: bool) -> None:
            token = None
            if skip_busy:
                token = _inflight_try_acquire(source, interval, now_ts, owner=owner)
                if token is None:
                    # 実体を必ず確認してからでないと skip も回収も判断できない
                    info = _inflight_diagnose(source, interval, now_ts)
                    # P0-1: 前世代がまだ走っているなら、まず「降りろ」を届ける。
                    # 登録だけ消してワーカーを放置するのは禁止なので、
                    # 回収の前に必ず協調キャンセルを要求する。
                    if info.get("state") != "absent" and not info.get("cancel_requested"):
                        _inflight_request_cancel(
                            source, interval,
                            reason="superseded_by_tick:%s" % owner,
                            generation=info.get("generation"),
                        )
                    reclaimed = _inflight_try_reclaim(source, interval, info)
                    if reclaimed:
                        logger.error(
                            "[SUMMARY PARALLEL RESET] stale inflight reclaimed reason=%s info=%s",
                            reclaimed,
                            info,
                        )
                        token = _inflight_try_acquire(source, interval, now_ts, owner=owner)
                    if token is None:
                        logger.warning(
                            "[SUMMARY PARALLEL RESET] inflight busy detail %s "
                            "(worker alive -> not reclaimed; double-start is forbidden)",
                            info,
                        )
                        skipped.append("%s:%s(age=%.0fs)" % (source, interval, _inflight_age(source, interval, now_ts)))
                        return
            try:
                fut = ex.submit(
                    _job_one_source,
                    source=source,
                    interval=int(interval),
                    now=n,
                    display=display,
                    run_entry=run_entry_flag,
                    inflight_token=token,
                )
            except Exception:
                # submit できなければ登録を残さない (残すと永久 skip になる)
                _inflight_release(source, interval, token)
                raise
            if token is not None:
                _inflight_attach_future(source, interval, token, fut)
            futures.append(fut)

        if run_push:
            for interval in wait_push_targets:
                _submit("push", int(interval), run_entry_flag=bool(run_entry) and in_session)
            for interval in bg_push_targets:
                _submit_bg_push_interval(interval=int(interval), now=n, display=display, run_entry=bool(run_entry) and in_session)
            # wait にも bg にも入らなかった 3分/5分は、計算済みのものを
            # summary DB から読んで publish する (別スレッド)。
            _publish_skipped_long_targets(push_targets, wait_push_targets, bg_push_targets, now=n)

        if run_ranking and in_session and _rst_env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True):
            for interval in ranking_targets:
                _submit("ranking", int(interval), run_entry_flag=False)
        elif run_ranking:
            logger.info("[SUMMARY PARALLEL RESET] ranking skipped targets=%s in_session=%s enabled=%s", ranking_targets, in_session, _rst_env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True))

        if skipped:
            logger.warning(
                "[SUMMARY PARALLEL RESET] skipped busy jobs now=%s skipped=%s inflight=%s "
                "reason=previous_job_still_running (SUMMARY_PARALLEL_SKIP_WHEN_BUSY)",
                n, skipped, inflight_snapshot(),
            )

        if not futures:
            logger.warning(
                "[SUMMARY PARALLEL RESET] tick no wait futures now=%s elapsed=%.3fs skipped=%s",
                n, time.perf_counter() - t0, skipped,
            )
            return out

        done_count = 0
        try:
            for fut in as_completed(futures, timeout=timeout):
                source, interval, df = fut.result()
                out.setdefault(source, {})[int(interval)] = df
                done_count += 1
        except FuturesTimeoutError:
            logger.error("[SUMMARY PARALLEL RESET] tick timeout now=%s timeout=%.1fs done=%s total=%s wait_push_targets=%s ranking_targets=%s local_executor=%s", n, timeout, done_count, len(futures), wait_push_targets, ranking_targets, use_local)
            for fut in futures:
                try:
                    fut.cancel()
                except Exception:
                    pass
            if not use_local:
                _rst_reset_global_executor("timeout")

        logger.warning(
            "[SUMMARY PARALLEL RESET] tick done now=%s push_done=%s ranking_done=%s elapsed=%.3fs local_executor=%s",
            n, sorted(out.get("push", {}).keys()), sorted(out.get("ranking", {}).keys()), time.perf_counter() - t0, use_local,
        )
        return out
    finally:
        if local_ex is not None:
            try:
                local_ex.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                local_ex.shutdown(wait=False)
            except Exception:
                pass
        with _RUNNING_LOCK:
            _RUNNING_KEYS.discard(key)


def apply_env_defaults() -> None:
    """旧 install() 冒頭の軽量既定値。"""
    _setdefault_env("SUMMARY_PARALLEL_FORCE_1_3_5", "0")
    _setdefault_env("SUMMARY_PUSH_BG_ALL_INTERVALS", "0")
    _setdefault_env("SUMMARY_PUSH_BG_LONG_INTERVALS", "0")
    _setdefault_env("SUMMARY_PUSH_DISPLAY_ALL_INTERVALS", "0")
    _setdefault_env("SUMMARY_MAIN_WAIT_PUSH_1M_ONLY", "1")
    _setdefault_env("SUMMARY_MAIN_BG_LONG_PUSH_ENABLED", "0")
    _setdefault_env("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC", "30")
    _setdefault_env("SUMMARY_PARALLEL_INTERVAL_WORKERS", "1")
    _setdefault_env("SUMMARY_PUSH_BG_INTERVAL_WORKERS", "1")
    _ensure_timeout_min()


apply_env_defaults()
