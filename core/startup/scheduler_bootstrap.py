# ============================================================
# File   : core/startup/scheduler_bootstrap.py
# Version: Ver2.3-PRODUCTION-SCHEDULER-BOOTSTRAP-RANKING-DISCORD-SENDER-INJECT
# ------------------------------------------------------------
# 【概要】
#   startup から schedule ライブラリのジョブ登録を安全に起動する
#
# 【主な機能】
#   - core.scheduler_tasks の総合登録関数を本線として呼ぶ
#   - import 半壊時も summary only / fallback へ段階的に退避する
#   - scheduler 登録失敗で起動全体を止めないように保護する
#   - 実際に採用した登録関数と登録結果をログに残す
#   - schedule.jobs の snapshot をログ出力する
#   - ランキング由来サマリー定時ジョブを追加登録する
#   - PUSH由来サマリー親tickを direct fallback で必ず登録する
#   - alerts_util.send_discord_text を ranking summary job へ注入する
#
# 【Ver2.3 追加】
#   - utils.alerts_util.send_discord_text を安全解決
#   - job_ranking_summary_all に announce_bridge / discord_sender を互換的に渡す
#   - function が非対応でも _call_with_supported_kwargs で安全に呼ぶ
#   - startup force run / manual force run にも同じ kwargs を注入
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import inspect
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

import schedule

logger = logging.getLogger(__name__)


# ============================================================
# module state
# ============================================================

_REGISTER_LOCK = threading.RLock()
_RANKING_JOB_LOCK = threading.RLock()

_REGISTERED_ONCE = False

_RANKING_JOB_RUNNING = False
_RANKING_JOB_STARTED_AT: Optional[dt.datetime] = None

_TAG_BOOTSTRAP = "startup_scheduler_bootstrap"
_TAG_SUMMARY_PARENT = "summary_parent_tick"
_TAG_SUMMARY_PUSH = "summary_push_tick"
_TAG_RANKING_SUMMARY = "ranking_summary_all"
_TAG_RANKING_SUMMARY_1M = "ranking_summary_1m"
_TAG_RANKING_SUMMARY_3M = "ranking_summary_3m"
_TAG_RANKING_SUMMARY_5M = "ranking_summary_5m"

_DEFAULT_RANKING_LOOKBACK_MINUTES = 240
_DEFAULT_RANKING_TOP_N = 10
_DEFAULT_RANKING_JOB_WARN_SECONDS = 45.0


# ============================================================
# global_data helpers
# ============================================================

def _get_global_data():
    try:
        from global_state import global_data  # type: ignore
        return global_data
    except Exception:
        pass

    try:
        from core.global_context.context import global_data  # type: ignore
        return global_data
    except Exception:
        return None


def _set_global_attr(name: str, value: Any) -> None:
    gd = _get_global_data()
    if gd is None:
        return

    try:
        setattr(gd, name, value)
    except Exception:
        pass


def _get_global_attr(name: str, default: Any = None) -> Any:
    gd = _get_global_data()
    if gd is None:
        return default

    try:
        return getattr(gd, name, default)
    except Exception:
        return default


# ============================================================
# helpers
# ============================================================

def _resolve_attr(
    module_name: str,
    attr_name: str,
    *,
    quiet: bool = False,
) -> Optional[Callable]:
    """
    module.attr を安全に解決する。

    - ModuleNotFoundError は optional 扱いで警告のみ
    - それ以外の import error も起動全体を止めない
    - callable でなければ None
    """
    try:
        mod = importlib.import_module(module_name)

    except ModuleNotFoundError as e:
        if not quiet:
            logger.warning(
                "[startup.scheduler_bootstrap] optional module not found: %s.%s err=%s",
                module_name,
                attr_name,
                e,
            )
        return None

    except Exception:
        if not quiet:
            logger.exception(
                "[startup.scheduler_bootstrap] import failed %s.%s",
                module_name,
                attr_name,
            )
        return None

    try:
        fn = getattr(mod, attr_name, None)
    except Exception:
        if not quiet:
            logger.exception(
                "[startup.scheduler_bootstrap] getattr failed %s.%s",
                module_name,
                attr_name,
            )
        return None

    if not callable(fn):
        if not quiet:
            logger.warning(
                "[startup.scheduler_bootstrap] callable not found: %s.%s",
                module_name,
                attr_name,
            )
        return None

    logger.info(
        "[startup.scheduler_bootstrap] resolved %s.%s",
        module_name,
        attr_name,
    )
    return fn


def _safe_call(
    fn: Optional[Callable],
    name: str,
    *args,
    **kwargs,
) -> bool:
    """
    任意 callable を安全に呼ぶ。
    失敗しても起動全体を止めない。
    """
    try:
        if not callable(fn):
            logger.warning(
                "[startup.scheduler_bootstrap] %s skipped (not available)",
                name,
            )
            return False

        fn(*args, **kwargs)

        logger.info(
            "[startup.scheduler_bootstrap] %s ok",
            name,
        )
        return True

    except Exception:
        logger.exception(
            "[startup.scheduler_bootstrap] %s failed",
            name,
        )
        return False


def _log_schedule_snapshot(context: str) -> None:
    """
    schedule.jobs の状況をログに出す。
    """
    try:
        jobs = list(getattr(schedule, "jobs", []) or [])

        snapshot = []

        for j in jobs:
            try:
                snapshot.append(
                    {
                        "job": str(j),
                        "tags": list(getattr(j, "tags", set()) or set()),
                        "next_run": str(getattr(j, "next_run", None)),
                        "last_run": str(getattr(j, "last_run", None)),
                        "interval": getattr(j, "interval", None),
                        "unit": getattr(j, "unit", None),
                        "should_run": getattr(j, "should_run", None),
                    }
                )
            except Exception:
                snapshot.append({"job": str(j)})

        logger.info(
            "[startup.scheduler_bootstrap] %s scheduled_jobs=%s snapshot=%s",
            context,
            len(jobs),
            snapshot,
        )

    except Exception:
        logger.exception(
            "[startup.scheduler_bootstrap] %s snapshot failed",
            context,
        )


def _has_schedule_tag(tag: str) -> bool:
    """
    指定tagのジョブが既に登録済みか確認。
    """
    try:
        for j in list(getattr(schedule, "jobs", []) or []):
            tags = getattr(j, "tags", set()) or set()
            if tag in tags:
                return True
    except Exception:
        pass

    return False


def _clear_schedule_tag(tag: str) -> None:
    """
    指定tagのジョブを削除。
    """
    try:
        schedule.clear(tag)
        logger.info(
            "[startup.scheduler_bootstrap] schedule.clear tag=%s",
            tag,
        )
    except Exception:
        logger.warning(
            "[startup.scheduler_bootstrap] schedule.clear failed tag=%s",
            tag,
            exc_info=True,
        )


def _safe_schedule_every_minute_at(
    fn: Callable,
    *,
    second: int,
    tag: str,
    name: str,
    replace_existing: bool = True,
) -> bool:
    """
    schedule.every().minute.at(':SS').do(fn) を安全に登録する。
    """
    if not callable(fn):
        logger.warning(
            "[startup.scheduler_bootstrap] schedule register skipped not callable name=%s",
            name,
        )
        return False

    second = int(second)
    if second < 0:
        second = 0
    if second > 59:
        second = 59

    at_text = f":{second:02d}"

    try:
        if replace_existing and _has_schedule_tag(tag):
            _clear_schedule_tag(tag)

        if _has_schedule_tag(tag):
            logger.info(
                "[startup.scheduler_bootstrap] schedule job already exists name=%s tag=%s",
                name,
                tag,
            )
            return True

        job = schedule.every().minute.at(at_text).do(fn)
        job.tag(tag)
        job.tag(_TAG_BOOTSTRAP)

        logger.info(
            "[startup.scheduler_bootstrap] schedule job registered name=%s every minute at %s tag=%s",
            name,
            at_text,
            tag,
        )
        return True

    except Exception:
        logger.exception(
            "[startup.scheduler_bootstrap] schedule job register failed name=%s tag=%s",
            name,
            tag,
        )
        return False


def _call_with_supported_kwargs(fn: Callable, **kwargs) -> Any:
    """
    関数が受け取れる kwargs だけ渡して呼ぶ。

    目的:
      job_ranking_summary_all が use_discord / announce_bridge /
      discord_sender / return_details を受け取れる版/受け取れない版の
      どちらでも壊さずに呼び出す。
    """
    if not callable(fn):
        raise TypeError("fn is not callable")

    try:
        sig = inspect.signature(fn)
        params = sig.parameters

        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in params.values()
        )

        if accepts_var_kw:
            call_kwargs = dict(kwargs)
        else:
            call_kwargs = {
                k: v
                for k, v in kwargs.items()
                if k in params
            }

        dropped = sorted(set(kwargs) - set(call_kwargs))
        if dropped:
            logger.info(
                "[startup.scheduler_bootstrap] dropped unsupported kwargs for %s: %s",
                getattr(fn, "__name__", str(fn)),
                dropped,
            )

        return fn(**call_kwargs)

    except ValueError:
        return fn(**kwargs)


def _summarize_result(result: Any) -> Any:
    """
    job 戻り値をログ用に軽量化する。
    DataFrame自体を丸ごとログに出さない。
    """
    try:
        if result is None:
            return None

        if isinstance(result, dict):
            out = {}
            for k, v in result.items():
                try:
                    rows = len(v) if v is not None else 0
                    cols = list(getattr(v, "columns", []) or [])
                    out[k] = {
                        "type": type(v).__name__,
                        "rows": rows,
                        "cols": cols[:20],
                    }
                except Exception:
                    out[k] = {
                        "type": type(v).__name__,
                        "repr": repr(v)[:300],
                    }
            return out

        rows = len(result) if hasattr(result, "__len__") else None
        cols = list(getattr(result, "columns", []) or [])
        return {
            "type": type(result).__name__,
            "rows": rows,
            "cols": cols[:20],
            "repr": repr(result)[:300],
        }

    except Exception:
        return repr(result)[:500]


def _resolve_discord_sender() -> Optional[Callable[[str], Any]]:
    """
    既存 alerts_util を優先して Discord sender を解決する。
    announce_bridge / jobs / runners にそのまま渡せる Callable[[str], Any] を返す。
    """
    candidates = [
        ("utils.alerts_util", "send_discord_text"),
        ("utils.alerts_util", "send_discord_notify"),
    ]

    for module_name, attr_name in candidates:
        fn = _resolve_attr(module_name, attr_name, quiet=True)
        if callable(fn):
            logger.info(
                "[startup.scheduler_bootstrap] discord sender resolved %s.%s",
                module_name,
                attr_name,
            )
            return fn

    logger.warning("[startup.scheduler_bootstrap] discord sender unavailable")
    return None


def _build_ranking_job_kwargs(*, force: bool) -> dict[str, Any]:
    """
    ranking summary job に渡す共通 kwargs。
    新旧関数どちらでも壊れないよう、実際の呼び出し時に
    _call_with_supported_kwargs で受け取れるものだけ渡す。
    """
    return {
        "force": bool(force),
        "lookback_minutes": _DEFAULT_RANKING_LOOKBACK_MINUTES,
        "use_yahoo_fill": True,
        "persist": True,
        "display": True,
        "top_n": _DEFAULT_RANKING_TOP_N,
        "topn": _DEFAULT_RANKING_TOP_N,
        "use_discord": True,
        "announce_bridge": True,
        "discord_sender": _resolve_discord_sender(),
        "return_details": True,
        "sides": ("BUY", "SELL"),
    }


# ============================================================
# resolver set
# ============================================================

def _resolve_core_scheduler_tasks() -> dict[str, Optional[Callable]]:
    """
    core.scheduler_tasks 系の関数を遅延解決する。

    import 時点で解決すると、半壊 import の影響を受けやすいため、
    register_scheduler_safe 実行時に解決する。
    """
    return {
        "register_summary_entry_exit_tasks": _resolve_attr(
            "core.scheduler_tasks",
            "register_summary_entry_exit_tasks",
            quiet=False,
        ),
        "register_summary_only_tasks": _resolve_attr(
            "core.scheduler_tasks",
            "register_summary_only_tasks",
            quiet=False,
        ),
        "register_summary_tasks_compat": _resolve_attr(
            "core.scheduler_tasks",
            "register_summary_tasks_compat",
            quiet=False,
        ),
        "register_summary_tasks": _resolve_attr(
            "core.scheduler_tasks",
            "register_summary_tasks",
            quiet=False,
        ),
        "register_ranking_save_tasks": _resolve_attr(
            "core.scheduler_tasks",
            "register_ranking_save_tasks",
            quiet=False,
        ),
    }


def _resolve_direct_tasks() -> dict[str, Optional[Callable]]:
    """
    direct task 系の登録関数を遅延解決する。
    """
    return {
        "register_push_tasks": _resolve_attr(
            "core.push_tasks",
            "register_push_tasks",
            quiet=False,
        ),
        "register_yahoo_tasks": _resolve_attr(
            "core.yahoo_tasks",
            "register_yahoo_tasks",
            quiet=False,
        ),
        "register_entry_exit_tasks": _resolve_attr(
            "core.entry_exit_tasks",
            "register_entry_exit_tasks",
            quiet=False,
        ),
    }


def _register_direct_summary_parent_tick(
    *,
    replace_existing: bool = False,
) -> bool:
    """
    scheduler_jobs.summary.scheduler の統合親tickを直接登録する。
    """
    if not replace_existing and _has_schedule_tag(_TAG_SUMMARY_PARENT):
        logger.info(
            "[startup.scheduler_bootstrap] direct summary parent tick skipped: already registered tag=%s",
            _TAG_SUMMARY_PARENT,
        )
        _set_global_attr("scheduler_summary_parent_registered", True)
        return True

    fn = _resolve_attr(
        "scheduler_jobs.summary.scheduler",
        "register_summary_tasks",
        quiet=False,
    )

    if not callable(fn):
        logger.warning(
            "[startup.scheduler_bootstrap] direct summary parent tick unavailable: "
            "scheduler_jobs.summary.scheduler.register_summary_tasks not callable"
        )
        _set_global_attr("scheduler_summary_parent_registered", False)
        return False

    ok = _safe_call(
        fn,
        "scheduler_jobs.summary.scheduler.register_summary_tasks",
    )

    parent_registered = _has_schedule_tag(_TAG_SUMMARY_PARENT)
    push_registered = _has_schedule_tag(_TAG_SUMMARY_PUSH)

    logger.info(
        "[startup.scheduler_bootstrap] direct summary parent tick result ok=%s parent_tag=%s push_tag=%s",
        ok,
        parent_registered,
        push_registered,
    )

    _set_global_attr("scheduler_summary_parent_registered", bool(parent_registered or push_registered))
    _set_global_attr(
        "scheduler_summary_parent_register_result",
        {
            "ok": bool(ok),
            "parent_tag": bool(parent_registered),
            "push_tag": bool(push_registered),
            "replace_existing": bool(replace_existing),
        },
    )

    return bool(ok and (parent_registered or push_registered))


# ============================================================
# ranking summary announce insurance
# ============================================================

def _announce_ranking_summary_intervals_safe(
    *,
    top_n: int = _DEFAULT_RANKING_TOP_N,
    use_discord: bool = True,
    intervals: tuple[int, ...] = (1, 3, 5),
) -> dict[int, bool]:
    """
    job_ranking_summary_all の display/announce が効かなかった場合に備えて、
    cache_store の latest ranking summary を announce.py から再表示・Discord送信する。
    """
    results: dict[int, bool] = {}

    fn = _resolve_attr(
        "trading.ranking.summary.announce",
        "announce_ranking_summary",
        quiet=False,
    )

    if not callable(fn):
        logger.warning(
            "[startup.scheduler_bootstrap] ranking summary announce unavailable"
        )
        return results

    for interval in intervals:
        started = time.perf_counter()

        try:
            logger.info(
                "[startup.scheduler_bootstrap] ranking summary before announce interval=%s top_n=%s use_discord=%s",
                interval,
                top_n,
                use_discord,
            )

            ok = bool(
                _call_with_supported_kwargs(
                    fn,
                    interval=interval,
                    topn=top_n,
                    top_n=top_n,
                    use_discord=use_discord,
                )
            )

            results[int(interval)] = ok

            logger.info(
                "[startup.scheduler_bootstrap] ranking summary after announce interval=%s ok=%s elapsed=%.3fs",
                interval,
                ok,
                time.perf_counter() - started,
            )

        except Exception:
            results[int(interval)] = False
            logger.exception(
                "[startup.scheduler_bootstrap] ranking summary announce failed interval=%s",
                interval,
            )

    return results


# ============================================================
# ranking summary job wrapper
# ============================================================

def _run_ranking_summary_all_job_sync():
    """
    schedule から呼ばれるランキング由来サマリー job (同期実行版)。

    BG実行が無効なプロセス (DB専用子プロセス等) ではこちらが使われる。
    """
    global _RANKING_JOB_RUNNING
    global _RANKING_JOB_STARTED_AT

    started = dt.datetime.now()
    perf_started = time.perf_counter()

    with _RANKING_JOB_LOCK:
        if _RANKING_JOB_RUNNING:
            elapsed = None
            if _RANKING_JOB_STARTED_AT is not None:
                try:
                    elapsed = (dt.datetime.now() - _RANKING_JOB_STARTED_AT).total_seconds()
                except Exception:
                    elapsed = None

            logger.warning(
                "[startup.scheduler_bootstrap] ranking summary scheduled job skipped reason=internal_previous_still_running started_at=%s elapsed=%s",
                _RANKING_JOB_STARTED_AT,
                elapsed,
            )
            return None

        _RANKING_JOB_RUNNING = True
        _RANKING_JOB_STARTED_AT = started
        _set_global_attr("ranking_summary_job_running", True)
        _set_global_attr("ranking_summary_job_started_at", started)

    try:
        logger.info(
            "[startup.scheduler_bootstrap] ranking summary scheduled job fire at=%s",
            started.strftime("%Y-%m-%d %H:%M:%S"),
        )

        fn = _resolve_attr(
            "scheduler_jobs.summary.ranking_summary_jobs",
            "job_ranking_summary_all",
            quiet=False,
        )

        if not callable(fn):
            logger.warning(
                "[startup.scheduler_bootstrap] ranking summary job function unavailable"
            )
            return None

        kwargs = _build_ranking_job_kwargs(force=False)

        logger.info(
            "[startup.scheduler_bootstrap] ranking summary before core job kwargs=%s",
            {k: ("<callable>" if callable(v) else v) for k, v in kwargs.items()},
        )

        result = _call_with_supported_kwargs(fn, **kwargs)

        elapsed_core = time.perf_counter() - perf_started

        logger.info(
            "[startup.scheduler_bootstrap] ranking summary core job done elapsed=%.3fs result=%s",
            elapsed_core,
            _summarize_result(result),
        )

        if elapsed_core >= _DEFAULT_RANKING_JOB_WARN_SECONDS:
            logger.warning(
                "[startup.scheduler_bootstrap] ranking summary core job slow elapsed=%.3fs warn_threshold=%.3fs",
                elapsed_core,
                _DEFAULT_RANKING_JOB_WARN_SECONDS,
            )

        _set_global_attr("last_ranking_summary_job_at", dt.datetime.now())
        _set_global_attr("last_ranking_summary_job_result", _summarize_result(result))

        announce_results = _announce_ranking_summary_intervals_safe(
            top_n=_DEFAULT_RANKING_TOP_N,
            use_discord=True,
            intervals=(1, 3, 5),
        )

        _set_global_attr("last_ranking_summary_announce_results", announce_results)

        logger.info(
            "[startup.scheduler_bootstrap] ranking summary scheduled job done elapsed=%.3fs announce_results=%s",
            time.perf_counter() - perf_started,
            announce_results,
        )

        return result

    except Exception:
        logger.exception(
            "[startup.scheduler_bootstrap] ranking summary scheduled job failed"
        )
        return None

    finally:
        with _RANKING_JOB_LOCK:
            _RANKING_JOB_RUNNING = False
            _RANKING_JOB_STARTED_AT = None
            _set_global_attr("ranking_summary_job_running", False)
            _set_global_attr("ranking_summary_job_finished_at", dt.datetime.now())


# ============================================================
# ranking summary BG 実行 / timeout cooldown
# (旧 core/startup/ranking_summary_schedule_bg_patch.py V5 から移設)
# ------------------------------------------------------------
# ranking_summary_all の schedule job が長時間 running のまま残り、
# 毎分 bg_still_running でスキップされ続ける問題を抑える。
#   - schedule から呼ばれたら即BGへ投入して return する
#   - BG実行が一定秒数を超えたら timeout 扱いにして cooldown へ入れる
#   - timeout/cooldown 中は新しいBGを投入しない
#   - 古いBGスレッド自体は kill できないので scheduler 側だけ即復帰させる
#
# 旧パッチは core/startup/__init__.py 経由でのみ install されていたため
# DB専用子プロセスでは適用されず、同期版が使われていた。本文化後も
# _ranking_bg_scope_enabled() で同じスコープを保つ。
# ============================================================

_RANKING_BG_EXECUTOR: Optional[ThreadPoolExecutor] = None
_RANKING_BG_LOCK = threading.RLock()
_RANKING_BG_RUNNING = False
_RANKING_BG_STARTED_AT: Optional[dt.datetime] = None
_RANKING_BG_COOLDOWN_UNTIL: Optional[dt.datetime] = None
_RANKING_BG_TIMEOUT_STREAK = 0

_RANKING_BG_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)


def _ranking_bg_env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _ranking_bg_env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            return max(1.0, float(raw))
    except Exception:
        pass
    return float(default)


def _ranking_bg_scope_enabled() -> bool:
    """このプロセスで BG 実行経路を使うか。

    RANKING_SUMMARY_BG_SCOPE_ENABLED=1/0 で明示的に上書きできる。
    CORE_STARTUP_FORCE_FULL_INIT_PATCHES=1 のときは DB プロセスでも適用する。
    """
    try:
        override = os.getenv("RANKING_SUMMARY_BG_SCOPE_ENABLED")
        if override is not None and str(override).strip() != "":
            return _ranking_bg_env_bool("RANKING_SUMMARY_BG_SCOPE_ENABLED", True)
        if _ranking_bg_env_bool("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", False):
            return True
        argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        return not any(name in argv_text for name in _RANKING_BG_DB_PROCESS_NAMES)
    except Exception:
        return True


def _ranking_bg_executor() -> ThreadPoolExecutor:
    global _RANKING_BG_EXECUTOR
    if _RANKING_BG_EXECUTOR is None:
        _RANKING_BG_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ranking-summary-bg")
    return _RANKING_BG_EXECUTOR


def _ranking_bg_elapsed_sec() -> float:
    try:
        if _RANKING_BG_STARTED_AT is None:
            return 0.0
        return max(0.0, (dt.datetime.now() - _RANKING_BG_STARTED_AT).total_seconds())
    except Exception:
        return 0.0


def _ranking_bg_cooldown_seconds() -> float:
    try:
        base = _ranking_bg_env_float("RANKING_SUMMARY_BG_COOLDOWN_SEC", 180.0)
        max_sec = _ranking_bg_env_float("RANKING_SUMMARY_BG_COOLDOWN_MAX_SEC", 600.0)
        streak = max(1, int(_RANKING_BG_TIMEOUT_STREAK or 1))
        return min(max_sec, base * streak)
    except Exception:
        return 180.0


def _ranking_bg_enter_cooldown(reason: str, *, elapsed: float) -> None:
    global _RANKING_BG_RUNNING, _RANKING_BG_STARTED_AT
    global _RANKING_BG_COOLDOWN_UNTIL, _RANKING_BG_TIMEOUT_STREAK
    try:
        _RANKING_BG_TIMEOUT_STREAK += 1
        cool_sec = _ranking_bg_cooldown_seconds()
        _RANKING_BG_COOLDOWN_UNTIL = dt.datetime.now() + dt.timedelta(seconds=cool_sec)
        _RANKING_BG_RUNNING = False
        _RANKING_BG_STARTED_AT = None
        _set_ranking_job_running(False)
        logger.warning(
            "[RANKING SUMMARY BG] timeout cooldown reason=%s elapsed=%.3fs timeout_streak=%s cooldown_sec=%.1f until=%s",
            reason, elapsed, _RANKING_BG_TIMEOUT_STREAK, cool_sec, _RANKING_BG_COOLDOWN_UNTIL,
        )
    except Exception:
        logger.debug("[RANKING SUMMARY BG] enter cooldown failed", exc_info=True)


def _ranking_bg_check_timeout_or_cooldown_locked() -> bool:
    """Trueなら今回submitしない。_RANKING_BG_LOCK内から呼ぶ。"""
    global _RANKING_BG_COOLDOWN_UNTIL
    try:
        now = dt.datetime.now()
        if _RANKING_BG_COOLDOWN_UNTIL is not None and now < _RANKING_BG_COOLDOWN_UNTIL:
            remain = (_RANKING_BG_COOLDOWN_UNTIL - now).total_seconds()
            logger.warning(
                "[RANKING SUMMARY BG] submit skipped reason=timeout_cooldown remain=%.1fs until=%s timeout_streak=%s",
                remain, _RANKING_BG_COOLDOWN_UNTIL, _RANKING_BG_TIMEOUT_STREAK,
            )
            return True
        if _RANKING_BG_COOLDOWN_UNTIL is not None and now >= _RANKING_BG_COOLDOWN_UNTIL:
            logger.warning("[RANKING SUMMARY BG] cooldown expired until=%s", _RANKING_BG_COOLDOWN_UNTIL)
            _RANKING_BG_COOLDOWN_UNTIL = None

        if _RANKING_BG_RUNNING:
            elapsed = _ranking_bg_elapsed_sec()
            timeout_sec = _ranking_bg_env_float("RANKING_SUMMARY_BG_TIMEOUT_SEC", 55.0)
            if elapsed >= timeout_sec:
                _ranking_bg_enter_cooldown("bg_timeout", elapsed=elapsed)
                return True
            logger.warning(
                "[RANKING SUMMARY BG] submit skipped reason=bg_still_running elapsed=%.3fs timeout=%.1fs",
                elapsed, timeout_sec,
            )
            return True
        return False
    except Exception:
        logger.debug("[RANKING SUMMARY BG] timeout/cooldown check failed", exc_info=True)
        return False


def _ranking_bg_clear_if_stale() -> bool:
    global _RANKING_BG_RUNNING, _RANKING_BG_STARTED_AT
    try:
        if not _RANKING_BG_RUNNING:
            return False
        elapsed = _ranking_bg_elapsed_sec()
        stale = _ranking_bg_env_float("RANKING_SUMMARY_BG_STALE_SEC", 120.0)
        if elapsed < stale:
            return False
        _RANKING_BG_RUNNING = False
        _RANKING_BG_STARTED_AT = None
        logger.warning("[RANKING SUMMARY BG] bg stale running cleared elapsed=%.3fs stale=%.3fs", elapsed, stale)
        return True
    except Exception:
        logger.debug("[RANKING SUMMARY BG] bg stale clear failed", exc_info=True)
        return False


def _clear_ranking_job_running_if_stale(*, force: bool = False, reason: str = "stale_check") -> bool:
    """同期版が使う _RANKING_JOB_RUNNING が stale なら解除する。"""
    global _RANKING_JOB_RUNNING, _RANKING_JOB_STARTED_AT
    try:
        if not _RANKING_JOB_RUNNING:
            return False
        started_at = _RANKING_JOB_STARTED_AT
        elapsed = 999999.0
        if isinstance(started_at, dt.datetime):
            elapsed = max(0.0, (dt.datetime.now() - started_at).total_seconds())
        stale = _ranking_bg_env_float("RANKING_SUMMARY_INTERNAL_STALE_SEC", 120.0)
        if (not force) and elapsed < stale:
            return False
        with _RANKING_JOB_LOCK:
            _RANKING_JOB_RUNNING = False
            _RANKING_JOB_STARTED_AT = None
        try:
            _set_global_attr("ranking_summary_job_running", False)
            _set_global_attr("ranking_summary_job_stale_cleared_at", dt.datetime.now())
            _set_global_attr("ranking_summary_job_stale_clear_reason", reason)
        except Exception:
            pass
        logger.warning(
            "[RANKING SUMMARY BG] internal running stale cleared reason=%s started_at=%s elapsed=%.3fs stale=%.3fs force=%s",
            reason, started_at, elapsed, stale, force,
        )
        return True
    except Exception:
        logger.debug("[RANKING SUMMARY BG] internal stale clear failed", exc_info=True)
        return False


def _set_ranking_job_running(value: bool, *, started_at: Optional[dt.datetime] = None) -> None:
    global _RANKING_JOB_RUNNING, _RANKING_JOB_STARTED_AT
    try:
        with _RANKING_JOB_LOCK:
            _RANKING_JOB_RUNNING = bool(value)
            _RANKING_JOB_STARTED_AT = started_at if value else None
        try:
            _set_global_attr("ranking_summary_job_running", bool(value))
            if value:
                _set_global_attr("ranking_summary_job_started_at", started_at)
            else:
                _set_global_attr("ranking_summary_job_finished_at", dt.datetime.now())
        except Exception:
            pass
    except Exception:
        logger.debug("[RANKING SUMMARY BG] set ranking job running failed", exc_info=True)


def _run_ranking_summary_core_job() -> Any:
    """BG経路で実行するランキングサマリー本体。"""
    started = dt.datetime.now()
    perf_started = time.perf_counter()
    _set_ranking_job_running(True, started_at=started)
    try:
        logger.warning("[RANKING SUMMARY BG] core ranking job start at=%s", started.strftime("%Y-%m-%d %H:%M:%S"))
        fn = _resolve_attr(
            "scheduler_jobs.summary.ranking_summary_jobs",
            "job_ranking_summary_all",
            quiet=False,
        )
        if not callable(fn):
            logger.warning("[RANKING SUMMARY BG] core ranking job skipped function unavailable")
            return None
        kwargs = _build_ranking_job_kwargs(force=False)
        result = _call_with_supported_kwargs(fn, **kwargs)
        elapsed = time.perf_counter() - perf_started
        try:
            _set_global_attr("last_ranking_summary_job_at", dt.datetime.now())
            _set_global_attr("last_ranking_summary_job_result", _summarize_result(result))
        except Exception:
            pass
        logger.warning("[RANKING SUMMARY BG] core ranking job done elapsed=%.3fs result=%s", elapsed, _summarize_result(result))
        try:
            announce_results = _announce_ranking_summary_intervals_safe(top_n=10, use_discord=True, intervals=(1, 3, 5))
            _set_global_attr("last_ranking_summary_announce_results", announce_results)
            logger.warning("[RANKING SUMMARY BG] announce done results=%s", announce_results)
        except Exception:
            logger.exception("[RANKING SUMMARY BG] announce failed")
        return result
    except Exception:
        logger.exception("[RANKING SUMMARY BG] core ranking job failed")
        return None
    finally:
        _set_ranking_job_running(False)


def _ranking_bg_task(started_at: dt.datetime) -> None:
    global _RANKING_BG_RUNNING, _RANKING_BG_STARTED_AT, _RANKING_BG_TIMEOUT_STREAK
    t0 = time.perf_counter()
    try:
        _clear_ranking_job_running_if_stale(reason="bg_task_before_core")
        logger.warning("[RANKING SUMMARY BG] bg start started_at=%s", started_at)
        _run_ranking_summary_core_job()
        elapsed = time.perf_counter() - t0
        _RANKING_BG_TIMEOUT_STREAK = 0
        logger.warning("[RANKING SUMMARY BG] bg done elapsed=%.3fs", elapsed)
    except Exception:
        logger.exception("[RANKING SUMMARY BG] bg failed elapsed=%.3fs", time.perf_counter() - t0)
    finally:
        with _RANKING_BG_LOCK:
            _RANKING_BG_RUNNING = False
            _RANKING_BG_STARTED_AT = None


def _run_ranking_summary_all_job_safe(*args: Any, **kwargs: Any):
    """
    schedule から呼ばれるランキング由来サマリー job の入口。

    BG実行が有効なプロセス (main.py 等) では即BGへ投入して return し、
    schedule 側を毎分ブロックさせない。無効なプロセスでは同期版を実行する。
    """
    global _RANKING_BG_RUNNING, _RANKING_BG_STARTED_AT

    if not _ranking_bg_scope_enabled():
        return _run_ranking_summary_all_job_sync()

    _clear_ranking_job_running_if_stale(reason="direct_wrapper_entry")
    if not _ranking_bg_env_bool("RANKING_SUMMARY_SCHEDULE_BG", True):
        return _run_ranking_summary_core_job()

    with _RANKING_BG_LOCK:
        _ranking_bg_clear_if_stale()
        if _ranking_bg_check_timeout_or_cooldown_locked():
            return None
        _RANKING_BG_RUNNING = True
        _RANKING_BG_STARTED_AT = dt.datetime.now()
        started = _RANKING_BG_STARTED_AT

    _ranking_bg_executor().submit(_ranking_bg_task, started)
    logger.warning("[RANKING SUMMARY BG] submitted schedule job returns immediately started_at=%s", started)
    return None


def register_ranking_summary_schedule_job(
    *,
    second: int = 10,
    replace_existing: bool = True,
    run_once_on_startup: bool = False,
) -> bool:
    """
    ランキング由来サマリー定時ジョブを schedule に登録する。
    """
    logger.info(
        "[startup.scheduler_bootstrap] register_ranking_summary_schedule_job start second=%s replace=%s run_once=%s",
        second,
        replace_existing,
        run_once_on_startup,
    )

    fn = _resolve_attr(
        "scheduler_jobs.summary.ranking_summary_jobs",
        "job_ranking_summary_all",
        quiet=False,
    )

    if not callable(fn):
        logger.warning(
            "[startup.scheduler_bootstrap] ranking summary schedule skipped: job_ranking_summary_all unavailable"
        )
        return False

    ok = _safe_schedule_every_minute_at(
        _run_ranking_summary_all_job_safe,
        second=second,
        tag=_TAG_RANKING_SUMMARY,
        name="ranking_summary_all",
        replace_existing=replace_existing,
    )

    if not ok:
        return False

    if run_once_on_startup:
        try:
            logger.info(
                "[startup.scheduler_bootstrap] ranking summary startup force run start"
            )

            force_kwargs = _build_ranking_job_kwargs(force=True)

            ret = _call_with_supported_kwargs(fn, **force_kwargs)

            logger.info(
                "[startup.scheduler_bootstrap] ranking summary startup force run done result=%s",
                _summarize_result(ret),
            )

            announce_results = _announce_ranking_summary_intervals_safe(
                top_n=_DEFAULT_RANKING_TOP_N,
                use_discord=True,
                intervals=(1, 3, 5),
            )

            logger.info(
                "[startup.scheduler_bootstrap] ranking summary startup force announce results=%s",
                announce_results,
            )

        except Exception:
            logger.exception(
                "[startup.scheduler_bootstrap] ranking summary startup force run failed"
            )

    logger.info(
        "[startup.scheduler_bootstrap] ranking summary job registered every minute at :%02d",
        int(second),
    )

    return True


# ============================================================
# existing registration flow
# ============================================================

def _register_existing_scheduler_tasks() -> bool:
    """
    既存タスク群を従来優先順位で登録する。
    """
    funcs = _resolve_core_scheduler_tasks()
    direct = _resolve_direct_tasks()

    registered = False

    if _safe_call(
        funcs.get("register_summary_entry_exit_tasks"),
        "core.scheduler_tasks.register_summary_entry_exit_tasks",
    ):
        registered = True

    if not registered:
        logger.info(
            "[startup.scheduler_bootstrap] primary registration unavailable -> trying modular fallback"
        )

        ok_summary = _safe_call(
            funcs.get("register_summary_only_tasks"),
            "core.scheduler_tasks.register_summary_only_tasks",
        )

        ok_ranking_save = _safe_call(
            funcs.get("register_ranking_save_tasks"),
            "core.scheduler_tasks.register_ranking_save_tasks",
        )

        ok_push = _safe_call(
            direct.get("register_push_tasks"),
            "core.push_tasks.register_push_tasks",
        )

        ok_yahoo = _safe_call(
            direct.get("register_yahoo_tasks"),
            "core.yahoo_tasks.register_yahoo_tasks",
        )

        ok_entry_exit = _safe_call(
            direct.get("register_entry_exit_tasks"),
            "core.entry_exit_tasks.register_entry_exit_tasks",
        )

        registered = any(
            [
                ok_summary,
                ok_ranking_save,
                ok_push,
                ok_yahoo,
                ok_entry_exit,
            ]
        )

    if not registered:
        if _safe_call(
            funcs.get("register_summary_tasks_compat"),
            "core.scheduler_tasks.register_summary_tasks_compat",
        ):
            registered = True

    if not registered:
        if _safe_call(
            funcs.get("register_summary_tasks"),
            "core.scheduler_tasks.register_summary_tasks",
        ):
            registered = True

    summary_parent_exists = _has_schedule_tag(_TAG_SUMMARY_PARENT) or _has_schedule_tag(_TAG_SUMMARY_PUSH)

    if summary_parent_exists:
        logger.info(
            "[startup.scheduler_bootstrap] summary parent tick already present parent=%s push=%s",
            _has_schedule_tag(_TAG_SUMMARY_PARENT),
            _has_schedule_tag(_TAG_SUMMARY_PUSH),
        )
    else:
        logger.warning(
            "[startup.scheduler_bootstrap] summary parent tick missing -> trying direct scheduler_jobs.summary.scheduler.register_summary_tasks"
        )
        if _register_direct_summary_parent_tick(replace_existing=False):
            registered = True

    if not registered:
        logger.warning(
            "[startup.scheduler_bootstrap] summary registration unavailable -> direct task fallback"
        )

        ok_push = _safe_call(
            direct.get("register_push_tasks"),
            "core.push_tasks.register_push_tasks",
        )

        ok_yahoo = _safe_call(
            direct.get("register_yahoo_tasks"),
            "core.yahoo_tasks.register_yahoo_tasks",
        )

        ok_entry_exit = _safe_call(
            direct.get("register_entry_exit_tasks"),
            "core.entry_exit_tasks.register_entry_exit_tasks",
        )

        registered = any([ok_push, ok_yahoo, ok_entry_exit])

    return bool(registered)


# ============================================================
# public api
# ============================================================

def register_scheduler_safe(
    *,
    register_ranking_summary: bool = True,
    ranking_summary_second: int = 10,
    ranking_summary_run_once_on_startup: bool = False,
    replace_existing_ranking_summary: bool = True,
) -> None:
    """
    startup から呼ばれる安全な scheduler 登録入口。
    """
    global _REGISTERED_ONCE

    with _REGISTER_LOCK:
        logger.info("[startup.scheduler_bootstrap] register_scheduler_safe start")
        _log_schedule_snapshot("before register_scheduler_safe")

        _set_global_attr("scheduler_bootstrap_running", True)

        try:
            existing_registered = _register_existing_scheduler_tasks()

            ranking_summary_registered = False

            if register_ranking_summary:
                ranking_summary_registered = register_ranking_summary_schedule_job(
                    second=ranking_summary_second,
                    replace_existing=replace_existing_ranking_summary,
                    run_once_on_startup=ranking_summary_run_once_on_startup,
                )
            else:
                logger.info(
                    "[startup.scheduler_bootstrap] ranking summary registration disabled"
                )

            _log_schedule_snapshot("after register_scheduler_safe")

            summary_parent_registered = _has_schedule_tag(_TAG_SUMMARY_PARENT) or _has_schedule_tag(_TAG_SUMMARY_PUSH)

            _set_global_attr(
                "scheduler_bootstrap_register_results",
                {
                    "existing_tasks": bool(existing_registered),
                    "summary_parent": bool(summary_parent_registered),
                    "ranking_summary": bool(ranking_summary_registered),
                },
            )
            _set_global_attr(
                "scheduler_bootstrap_registered",
                bool(existing_registered or summary_parent_registered or ranking_summary_registered),
            )
            _set_global_attr("scheduler_summary_parent_registered", bool(summary_parent_registered))
            _set_global_attr("scheduler_ranking_summary_registered", bool(ranking_summary_registered))

            if existing_registered or summary_parent_registered or ranking_summary_registered:
                _REGISTERED_ONCE = True
                logger.info(
                    "[startup.scheduler_bootstrap] register_scheduler_safe complete "
                    "existing=%s summary_parent=%s ranking_summary=%s",
                    existing_registered,
                    summary_parent_registered,
                    ranking_summary_registered,
                )
            else:
                logger.error(
                    "[startup.scheduler_bootstrap] register_scheduler_safe failed: no scheduler task registered"
                )

        finally:
            _set_global_attr("scheduler_bootstrap_running", False)


def register_scheduler_safe_once(
    *,
    register_ranking_summary: bool = True,
    ranking_summary_second: int = 10,
    ranking_summary_run_once_on_startup: bool = False,
) -> None:
    """
    二重登録を避けたい場合の入口。
    """
    global _REGISTERED_ONCE

    with _REGISTER_LOCK:
        if _REGISTERED_ONCE:
            logger.info(
                "[startup.scheduler_bootstrap] register_scheduler_safe_once skipped already registered"
            )

            if register_ranking_summary and not _has_schedule_tag(_TAG_RANKING_SUMMARY):
                logger.warning(
                    "[startup.scheduler_bootstrap] ranking summary tag missing -> registering only ranking summary"
                )
                register_ranking_summary_schedule_job(
                    second=ranking_summary_second,
                    replace_existing=True,
                    run_once_on_startup=ranking_summary_run_once_on_startup,
                )

            return

    register_scheduler_safe(
        register_ranking_summary=register_ranking_summary,
        ranking_summary_second=ranking_summary_second,
        ranking_summary_run_once_on_startup=ranking_summary_run_once_on_startup,
        replace_existing_ranking_summary=True,
    )


def log_registered_schedule_jobs(context: str = "manual") -> None:
    """
    外部から schedule snapshot を出したい場合の公開関数。
    """
    _log_schedule_snapshot(context)


def clear_ranking_summary_schedule_job() -> None:
    """
    ランキング由来サマリーjobのみ削除する。
    """
    _clear_schedule_tag(_TAG_RANKING_SUMMARY)


def force_run_ranking_summary_once():
    """
    ランキング由来サマリーを手動で1回実行する。
    """
    fn = _resolve_attr(
        "scheduler_jobs.summary.ranking_summary_jobs",
        "job_ranking_summary_all",
        quiet=False,
    )

    if not callable(fn):
        logger.warning(
            "[startup.scheduler_bootstrap] force_run_ranking_summary_once skipped: function unavailable"
        )
        return None

    started = time.perf_counter()

    try:
        logger.info(
            "[startup.scheduler_bootstrap] force_run_ranking_summary_once start"
        )

        kwargs = _build_ranking_job_kwargs(force=True)

        ret = _call_with_supported_kwargs(fn, **kwargs)

        logger.info(
            "[startup.scheduler_bootstrap] force_run_ranking_summary_once core done elapsed=%.3fs result=%s",
            time.perf_counter() - started,
            _summarize_result(ret),
        )

        announce_results = _announce_ranking_summary_intervals_safe(
            top_n=_DEFAULT_RANKING_TOP_N,
            use_discord=True,
            intervals=(1, 3, 5),
        )

        logger.info(
            "[startup.scheduler_bootstrap] force_run_ranking_summary_once announce results=%s",
            announce_results,
        )

        logger.info(
            "[startup.scheduler_bootstrap] force_run_ranking_summary_once done elapsed=%.3fs",
            time.perf_counter() - started,
        )

        return ret

    except Exception:
        logger.exception(
            "[startup.scheduler_bootstrap] force_run_ranking_summary_once failed"
        )
        return None


# ============================================================
# compatibility aliases
# ============================================================

def bootstrap_scheduler(*args, **kwargs) -> None:
    """
    互換入口。
    """
    return register_scheduler_safe(*args, **kwargs)


def start_scheduler_bootstrap(*args, **kwargs) -> None:
    """
    互換入口。
    """
    return register_scheduler_safe(*args, **kwargs)


def setup_scheduler(*args, **kwargs) -> None:
    """
    互換入口。
    """
    return register_scheduler_safe(*args, **kwargs)


def init_scheduler(*args, **kwargs) -> None:
    """
    互換入口。
    """
    return register_scheduler_safe(*args, **kwargs)


# ============================================================
# exports
# ============================================================

__all__ = [
    "register_scheduler_safe",
    "register_scheduler_safe_once",
    "register_ranking_summary_schedule_job",
    "_register_direct_summary_parent_tick",
    "clear_ranking_summary_schedule_job",
    "force_run_ranking_summary_once",
    "log_registered_schedule_jobs",
    "bootstrap_scheduler",
    "start_scheduler_bootstrap",
    "setup_scheduler",
    "init_scheduler",
]