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


def _job_one_source(*, source: str, interval: int, now: dt.datetime, display: bool, run_entry: bool) -> tuple[str, int, pd.DataFrame]:
    t0 = time.perf_counter()
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


def install() -> bool:
    global _INSTALLED, _ORIG_TIME_LOCKED

    # 軽量既定: 環境変数で明示された時だけ全足/長足BGを有効化する。
    _setdefault_env("SUMMARY_PARALLEL_FORCE_1_3_5", "0")
    _setdefault_env("SUMMARY_PUSH_BG_ALL_INTERVALS", "0")
    _setdefault_env("SUMMARY_PUSH_BG_LONG_INTERVALS", "0")
    _setdefault_env("SUMMARY_PUSH_DISPLAY_ALL_INTERVALS", "0")
    _setdefault_env("SUMMARY_MAIN_WAIT_PUSH_1M_ONLY", "1")
    _setdefault_env("SUMMARY_MAIN_BG_LONG_PUSH_ENABLED", "0")
    _setdefault_env("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC", "30")
    _setdefault_env("SUMMARY_PARALLEL_INTERVAL_WORKERS", "1")
    _setdefault_env("SUMMARY_PUSH_BG_INTERVAL_WORKERS", "1")

    if _INSTALLED:
        _ensure_timeout_min()
        return True

    _ensure_timeout_min()

    try:
        import scheduler_jobs.summary.time_locked_runner as tlr
        import scheduler_jobs.summary.runners as runners
        import scheduler_jobs.summary.scheduler as scheduler
        cur = getattr(tlr, "run_time_locked_summary_jobs", None)
        if getattr(cur, "_summary_parallel_intervals_v12", False):
            _INSTALLED = True
            return True
        _ORIG_TIME_LOCKED = cur
        _patched_run_time_locked_summary_jobs._summary_parallel_intervals_v12 = True  # type: ignore[attr-defined]
        tlr.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs
        runners.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs
        scheduler.run_time_locked_summary_jobs = _patched_run_time_locked_summary_jobs
        _INSTALLED = True
        logger.warning(
            "[SUMMARY PARALLEL] installed v12 enabled=%s workers=%s bg_workers=%s timeout=%.1f ranking_parallel=%s force_1_3_5=%s push_all_intervals=%s push_bg_all=%s push_bg_long=%s main_entry_only=%s main_wait_1m_only=%s min_timeout=%s cap_enabled=%s",
            _env_bool("SUMMARY_PARALLEL_INTERVALS_ENABLED", True),
            _env_int("SUMMARY_PARALLEL_INTERVAL_WORKERS", 1),
            _env_int("SUMMARY_PUSH_BG_INTERVAL_WORKERS", 1),
            _env_float("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", 30.0),
            _env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True),
            _force_all_targets_enabled(),
            _push_all_intervals_enabled(),
            _push_bg_all_intervals_enabled(),
            _push_bg_long_intervals_enabled(),
            _is_main_entry_only_process(),
            _main_wait_push_1m_only_enabled(),
            os.getenv("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC"),
            _main_tick_timeout_cap_enabled(),
        )
        return True
    except Exception as e:
        logger.exception("[SUMMARY PARALLEL] install failed err=%s", e)
        return False


try:
    install()
except Exception as e:
    logger.exception("[SUMMARY PARALLEL] auto install failed err=%s", e)

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


def _patched_run_time_locked_summary_jobs(*, now: Optional[dt.datetime] = None, run_push: bool = True, run_ranking: bool = True, display: bool = True, run_entry: bool = True) -> dict[str, dict[int, pd.DataFrame]]:
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

        if run_push:
            for interval in wait_push_targets:
                futures.append(ex.submit(_job_one_source, source="push", interval=int(interval), now=n, display=display, run_entry=bool(run_entry) and in_session))
            for interval in bg_push_targets:
                _submit_bg_push_interval(interval=int(interval), now=n, display=display, run_entry=bool(run_entry) and in_session)

        if run_ranking and in_session and _rst_env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True):
            for interval in ranking_targets:
                futures.append(ex.submit(_job_one_source, source="ranking", interval=int(interval), now=n, display=display, run_entry=False))
        elif run_ranking:
            logger.info("[SUMMARY PARALLEL RESET] ranking skipped targets=%s in_session=%s enabled=%s", ranking_targets, in_session, _rst_env_bool("SUMMARY_PARALLEL_RANKING_ENABLED", True))

        if not futures:
            logger.warning("[SUMMARY PARALLEL RESET] tick no wait futures now=%s elapsed=%.3fs", n, time.perf_counter() - t0)
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
