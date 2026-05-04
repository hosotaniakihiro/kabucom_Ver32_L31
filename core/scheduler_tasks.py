# ============================================================
# File   : core/scheduler_tasks.py
# Function:
#   - アプリ全体の scheduler タスク登録を担当する
#   - summary系 scheduler の登録を統合親tick優先で実行する
#   - ranking_snapshot_1min 毎分保存タスクを登録する
#   - 既存の push / yahoo / entry_exit 系タスクと共存させる
#   - summary scheduler が壊れていても fallback で生かす
#   - import 半壊時も利用可能な関数だけ登録し、全体停止を避ける
# ------------------------------------------------------------
# Version: Ver32.1-CORE-SCHEDULER-TASKS-RANKING-SAVE-ALWAYS-REGISTERED
#          -SUMMARY-SOURCE-SEPARATED
#          -RANKING-SAVE-TICK-02SEC
#          -ROBUST-IMPORT-RESOLUTION
#          -FALLBACK-KEEPALIVE
#          -UNIFIED-SUMMARY-PARENT-PRIORITY
#          -COMPAT-REGISTER-SUMMARY-TASKS-INCLUDES-RANKING-SAVE
# ------------------------------------------------------------
# 機能:
#   - アプリ全体の scheduler タスク登録
#   - PUSH由来サマリーの登録
#   - ランキング由来サマリーの登録
#   - ranking_snapshot_1min 毎分保存タスクの登録
#   - 既存の push / yahoo / entry_exit 系タスクと共存
#
# 改善点:
#   - summary scheduler の import を個別解決
#   - ranking保存タスク job_save_ranking を明示追加
#   - 1つ欠けても他の関数は生かす
#   - scheduler module が半壊でも fallback で動作
#   - summary系と ranking保存系の責務を分離
#   - 統合親tick方式を優先し、二重登録を避ける
#   - register_summary_tasks() 経由でも ranking保存を登録
#   - ranking保存tickを :02 にずらし、summary :00 との衝突を軽減
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import inspect
import logging
from typing import Any, Callable, Optional

import schedule

logger = logging.getLogger(__name__)


# ============================================================
# tags
# ============================================================

_TAG_SUMMARY_FALLBACK_TICK = "summary_fallback_tick"
_TAG_RANKING_SAVE_TICK = "ranking_save_tick"

# ranking保存は summary :00 と衝突しないよう :02 にする
_RANKING_SAVE_SECOND = 2


# ============================================================
# helper
# ============================================================

def _resolve_attr(module_name: str, attr_name: str) -> Optional[Callable[..., Any]]:
    """
    module.attr を安全に解決する。

    import 半壊時でも全体を止めない。
    """
    try:
        mod = importlib.import_module(module_name)
        fn = getattr(mod, attr_name, None)

        if callable(fn):
            logger.info(
                "[core.scheduler_tasks] resolved %s.%s",
                module_name,
                attr_name,
            )
            return fn

        logger.info(
            "[core.scheduler_tasks] unresolved %s.%s (not callable)",
            module_name,
            attr_name,
        )
        return None

    except Exception:
        logger.info(
            "[core.scheduler_tasks] unresolved %s.%s",
            module_name,
            attr_name,
            exc_info=False,
        )
        return None



def _call_with_supported_kwargs(fn: Callable[..., Any], **kwargs) -> Any:
    """
    関数が受け取れる kwargs だけ渡して呼ぶ。
    新版 job_save_ranking(mode="fast") なら軽量モードで呼び、
    旧版 job_save_ranking() なら引数なしで壊さず呼ぶ。
    """
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if accepts_var_kw:
            return fn(**kwargs)
        call_kwargs = {k: v for k, v in kwargs.items() if k in params}
        return fn(**call_kwargs)
    except ValueError:
        return fn(**kwargs)

def _safe_call(fn: Optional[Callable[..., Any]], name: str) -> None:
    """
    任意の登録関数を安全に呼ぶ。
    """
    try:
        if callable(fn):
            fn()
            logger.info("[core.scheduler_tasks] %s ok", name)
        else:
            logger.info("[core.scheduler_tasks] %s skipped (not available)", name)

    except Exception:
        logger.exception("[core.scheduler_tasks] %s failed", name)


def _safe_job_name(fn: Any) -> str:
    try:
        return getattr(fn, "__name__", repr(fn))
    except Exception:
        return "unknown"


def _has_schedule_tag(tag: str) -> bool:
    """
    schedule に指定 tag の job が存在するか確認する。
    """
    try:
        for job in list(getattr(schedule, "jobs", []) or []):
            tags = getattr(job, "tags", set()) or set()
            if tag in tags:
                return True
    except Exception:
        pass

    return False


def _clear_schedule_tag(tag: str) -> None:
    """
    指定 tag の schedule job を削除する。
    """
    try:
        schedule.clear(tag)
        logger.info("[core.scheduler_tasks] cleared existing scheduled jobs tag=%s", tag)
    except Exception:
        logger.warning(
            "[core.scheduler_tasks] schedule.clear failed tag=%s",
            tag,
            exc_info=True,
        )


def _log_registered_jobs(context: str) -> None:
    """
    現在の schedule.jobs を軽くログ出力する。
    """
    try:
        rows = []
        for job in list(getattr(schedule, "jobs", []) or []):
            try:
                rows.append(
                    {
                        "job": str(job),
                        "tags": sorted(list(getattr(job, "tags", set()) or set())),
                        "next_run": str(getattr(job, "next_run", None)),
                        "last_run": str(getattr(job, "last_run", None)),
                    }
                )
            except Exception:
                rows.append({"job": str(job)})

        logger.info(
            "[core.scheduler_tasks] schedule snapshot context=%s count=%s jobs=%s",
            context,
            len(rows),
            rows,
        )

    except Exception:
        logger.debug(
            "[core.scheduler_tasks] schedule snapshot failed context=%s",
            context,
            exc_info=True,
        )


# ============================================================
# summary scheduler resolvers
# ============================================================

_register_push_summary_tasks = _resolve_attr(
    "scheduler_jobs.summary.scheduler",
    "register_push_summary_tasks",
)

_register_ranking_summary_tasks = _resolve_attr(
    "scheduler_jobs.summary.scheduler",
    "register_ranking_summary_tasks",
)

_register_summary_tasks_impl = _resolve_attr(
    "scheduler_jobs.summary.scheduler",
    "register_summary_tasks",
)

_register_time_locked_summary_tasks = _resolve_attr(
    "scheduler_jobs.summary.scheduler",
    "register_time_locked_summary_tasks",
)


# ============================================================
# summary runners fallback
# ============================================================

_job_push_summary = _resolve_attr(
    "scheduler_jobs.summary.runners",
    "job_summary",
)

_job_ranking_summary = _resolve_attr(
    "scheduler_jobs.summary.runners",
    "job_ranking_summary",
)


# ============================================================
# ranking SAVE task resolver
# 実体候補:
#   - trading.ranking.scheduler.job_save_ranking
#   - trading.ranking.scheduler.save_ranking_data_loop
# ============================================================

_job_save_ranking = _resolve_attr(
    "trading.ranking.scheduler",
    "job_save_ranking",
)

_save_ranking_data_loop = _resolve_attr(
    "trading.ranking.scheduler",
    "save_ranking_data_loop",
)


# ============================================================
# existing other task resolvers
# ============================================================

register_yahoo_tasks = _resolve_attr(
    "core.yahoo_tasks",
    "register_yahoo_tasks",
)

register_push_tasks = _resolve_attr(
    "core.push_tasks",
    "register_push_tasks",
)

register_entry_exit_tasks = _resolve_attr(
    "core.entry_exit_tasks",
    "register_entry_exit_tasks",
)


# ============================================================
# fallback runners: summary
# ============================================================

def _run_push_interval(interval: int) -> None:
    try:
        if callable(_job_push_summary):
            _job_push_summary(int(interval))
            logger.info(
                "[core.scheduler_tasks] push summary fired interval=%s",
                interval,
            )
        else:
            logger.warning(
                "[core.scheduler_tasks] push summary runner unavailable interval=%s",
                interval,
            )

    except Exception:
        logger.exception(
            "[core.scheduler_tasks] push summary failed interval=%s",
            interval,
        )


def _run_ranking_interval(interval: int) -> None:
    try:
        if callable(_job_ranking_summary):
            _job_ranking_summary(int(interval))
            logger.info(
                "[core.scheduler_tasks] ranking summary fired interval=%s",
                interval,
            )
        else:
            logger.warning(
                "[core.scheduler_tasks] ranking summary runner unavailable interval=%s",
                interval,
            )

    except Exception:
        logger.exception(
            "[core.scheduler_tasks] ranking summary failed interval=%s",
            interval,
        )


def _summary_tick() -> None:
    """
    毎分 :00 に呼ばれる summary 統合 tick。

    0分起点で 1分 / 3分 / 5分 を判定する。
    PUSH と ranking summary を同じ基準で実行する。
    fallback用途。
    """
    try:
        now = dt.datetime.now().replace(second=0, microsecond=0)
        minute = int(now.minute)

        logger.info(
            "[core.scheduler_tasks] summary tick start hhmm=%s interval_base_minute=%s",
            now.strftime("%H:%M"),
            minute,
        )

        # PUSH summary
        _run_push_interval(1)
        if minute % 3 == 0:
            _run_push_interval(3)
        if minute % 5 == 0:
            _run_push_interval(5)

        # ranking summary
        _run_ranking_interval(1)
        if minute % 3 == 0:
            _run_ranking_interval(3)
        if minute % 5 == 0:
            _run_ranking_interval(5)

        logger.info("[core.scheduler_tasks] summary tick finished")

    except Exception:
        logger.exception("[core.scheduler_tasks] summary tick failed")


def _register_summary_fallback_tasks() -> None:
    """
    scheduler_jobs.summary.scheduler が import できない場合の
    自前 fallback 登録。
    """
    try:
        _clear_schedule_tag(_TAG_SUMMARY_FALLBACK_TICK)

        schedule.every().minute.at(":00").do(_summary_tick).tag(_TAG_SUMMARY_FALLBACK_TICK)

        logger.info(
            "[core.scheduler_tasks] fallback summary schedule registered "
            "(every minute :00, base=0min, push/ranking summary 1m/3m/5m)"
        )

    except Exception:
        logger.exception(
            "[core.scheduler_tasks] fallback summary schedule registration failed"
        )


# ============================================================
# ranking SAVE
# ============================================================

def _run_ranking_save_tick() -> None:
    """
    ranking_snapshot_1min 保存用の毎分 tick。

    summary とは別責務。
    """
    try:
        now = dt.datetime.now().replace(second=0, microsecond=0)

        logger.info(
            "[core.scheduler_tasks] ranking save tick start hhmm=%s",
            now.strftime("%H:%M"),
        )

        if callable(_job_save_ranking):
            result = _call_with_supported_kwargs(
                _job_save_ranking,
                mode="fast",
                run_full_postprocess=False,
                save_legacy=False,
            )
            logger.info(
                "[core.scheduler_tasks] ranking save fired FAST fn=%s result_type=%s",
                _safe_job_name(_job_save_ranking),
                type(result).__name__,
            )

        elif callable(_save_ranking_data_loop):
            result = _call_with_supported_kwargs(
                _save_ranking_data_loop,
                mode="fast",
                run_full_postprocess=False,
                save_legacy=False,
            )
            logger.info(
                "[core.scheduler_tasks] ranking save fired FAST fn=%s result_type=%s",
                _safe_job_name(_save_ranking_data_loop),
                type(result).__name__,
            )

        else:
            logger.warning(
                "[core.scheduler_tasks] ranking save runner unavailable "
                "fn=job_save_ranking/save_ranking_data_loop"
            )

        logger.info(
            "[core.scheduler_tasks] ranking save tick finished hhmm=%s",
            now.strftime("%H:%M"),
        )

    except Exception:
        logger.exception("[core.scheduler_tasks] ranking save tick failed")


def register_ranking_save_tasks() -> None:
    """
    ranking_snapshot_1min 保存タスク登録。

    毎分 :02 に ranking save job を実行する。

    理由:
      - summary系が :00 で動く構成と衝突しやすいため
      - ranking保存を先に走らせる場合でも、DBロック集中を少し避けるため
    """
    try:
        _clear_schedule_tag(_TAG_RANKING_SAVE_TICK)

        at_text = f":{int(_RANKING_SAVE_SECOND):02d}"

        schedule.every().minute.at(at_text).do(_run_ranking_save_tick).tag(_TAG_RANKING_SAVE_TICK)

        logger.info(
            "[core.scheduler_tasks] registered ranking save every minute at %s "
            "fn=trading.ranking.scheduler.job_save_ranking/save_ranking_data_loop "
            "tag=%s job_save_available=%s loop_available=%s",
            at_text,
            _TAG_RANKING_SAVE_TICK,
            callable(_job_save_ranking),
            callable(_save_ranking_data_loop),
        )

        if not callable(_job_save_ranking) and not callable(_save_ranking_data_loop):
            logger.warning(
                "[core.scheduler_tasks] ranking save task registered but runner unavailable. "
                "Check trading.ranking.scheduler.job_save_ranking or save_ranking_data_loop"
            )

    except Exception:
        logger.exception("[core.scheduler_tasks] register_ranking_save_tasks failed")


def ensure_ranking_save_tasks_registered() -> None:
    """
    ranking保存タスクが未登録なら登録する。

    複数入口から呼ばれても安全にするための保険。
    """
    try:
        if _has_schedule_tag(_TAG_RANKING_SAVE_TICK):
            logger.info(
                "[core.scheduler_tasks] ranking save task already registered tag=%s",
                _TAG_RANKING_SAVE_TICK,
            )
            return

        logger.warning(
            "[core.scheduler_tasks] ranking save task not found. registering now tag=%s",
            _TAG_RANKING_SAVE_TICK,
        )
        register_ranking_save_tasks()

    except Exception:
        logger.exception("[core.scheduler_tasks] ensure_ranking_save_tasks_registered failed")


# ============================================================
# public api
# ============================================================

def register_summary_only_tasks() -> None:
    """
    summary系のみ登録。

    優先順位:
      1) scheduler_jobs.summary.scheduler.register_summary_tasks
      2) scheduler_jobs.summary.scheduler.register_time_locked_summary_tasks
      3) push/ranking 個別登録
      4) fallback

    注意:
      - この関数は名前通り summary のみ。
      - ranking保存は register_ranking_save_tasks() で別登録する。
      - ただし旧互換 register_summary_tasks() では ranking保存も呼ぶ。
    """
    try:
        logger.info("[core.scheduler_tasks] register_summary_only_tasks start")

        registered = False

        # ----------------------------------------------------
        # 最優先: 統合親tick登録
        # ----------------------------------------------------
        if callable(_register_summary_tasks_impl):
            try:
                _register_summary_tasks_impl()
                logger.info("[core.scheduler_tasks] register_summary_tasks impl ok")
                registered = True
            except Exception:
                logger.exception("[core.scheduler_tasks] register_summary_tasks impl failed")

        # ----------------------------------------------------
        # 次点: time-locked summary tasks
        # ----------------------------------------------------
        if (not registered) and callable(_register_time_locked_summary_tasks):
            try:
                _register_time_locked_summary_tasks()
                logger.info("[core.scheduler_tasks] register_time_locked_summary_tasks ok")
                registered = True
            except Exception:
                logger.exception("[core.scheduler_tasks] register_time_locked_summary_tasks failed")

        # ----------------------------------------------------
        # 互換: 個別登録
        # ----------------------------------------------------
        if not registered:
            dedicated_registered = False

            if callable(_register_push_summary_tasks):
                try:
                    _register_push_summary_tasks()
                    logger.info("[core.scheduler_tasks] register_push_summary_tasks ok")
                    dedicated_registered = True
                except Exception:
                    logger.exception("[core.scheduler_tasks] register_push_summary_tasks failed")

            if callable(_register_ranking_summary_tasks):
                try:
                    _register_ranking_summary_tasks()
                    logger.info("[core.scheduler_tasks] register_ranking_summary_tasks ok")
                    dedicated_registered = True
                except Exception:
                    logger.exception("[core.scheduler_tasks] register_ranking_summary_tasks failed")

            registered = dedicated_registered

        # ----------------------------------------------------
        # 最終fallback
        # ----------------------------------------------------
        if not registered:
            _register_summary_fallback_tasks()

        logger.info("[core.scheduler_tasks] register_summary_only_tasks finished")
        _log_registered_jobs("after_register_summary_only_tasks")

    except Exception:
        logger.exception("[core.scheduler_tasks] register_summary_only_tasks failed")


def register_summary_tasks_compat() -> None:
    """
    旧互換名。

    以前は summary のみだったが、main.py / startup 側がこの関数だけを
    呼ぶケースで ranking保存が登録されない事故を防ぐため、
    ranking保存も登録する。
    """
    try:
        logger.info("[core.scheduler_tasks] register_summary_tasks_compat start")

        register_summary_only_tasks()
        register_ranking_save_tasks()

        logger.info("[core.scheduler_tasks] register_summary_tasks_compat finished")
        _log_registered_jobs("after_register_summary_tasks_compat")

    except Exception:
        logger.exception("[core.scheduler_tasks] register_summary_tasks_compat failed")


def register_summary_entry_exit_tasks() -> None:
    """
    既存互換用の総合登録関数。

    main.py などがこの名前を呼んでいる前提に対応。
    """
    try:
        logger.info("[core.scheduler_tasks] register_summary_entry_exit_tasks start")

        # summary系
        register_summary_only_tasks()

        # ranking保存系
        register_ranking_save_tasks()

        # 既存他タスク
        _safe_call(register_push_tasks, "register_push_tasks")
        _safe_call(register_yahoo_tasks, "register_yahoo_tasks")
        _safe_call(register_entry_exit_tasks, "register_entry_exit_tasks")

        # 保険
        ensure_ranking_save_tasks_registered()

        logger.info("[core.scheduler_tasks] register_summary_entry_exit_tasks finished")
        _log_registered_jobs("after_register_summary_entry_exit_tasks")

    except Exception:
        logger.exception("[core.scheduler_tasks] register_summary_entry_exit_tasks failed")


def register_summary_tasks() -> None:
    """
    旧互換エクスポート。

    重要:
      startup_bootstrap / main.py がこの関数だけを採用した場合でも、
      ranking_snapshot_1min 毎分保存を落とさないため、
      summaryだけでなく ranking保存も登録する。
    """
    try:
        logger.info("[core.scheduler_tasks] register_summary_tasks compat start")

        register_summary_only_tasks()
        register_ranking_save_tasks()

        logger.info("[core.scheduler_tasks] register_summary_tasks compat finished")
        _log_registered_jobs("after_register_summary_tasks")

    except Exception:
        logger.exception("[core.scheduler_tasks] register_summary_tasks compat failed")


def register_all_tasks() -> None:
    """
    明示的な総合登録名。

    新しい呼び出し元では、この関数を使うのが最も分かりやすい。
    """
    register_summary_entry_exit_tasks()


__all__ = [
    "register_summary_only_tasks",
    "register_summary_tasks_compat",
    "register_summary_entry_exit_tasks",
    "register_summary_tasks",
    "register_ranking_save_tasks",
    "ensure_ranking_save_tasks_registered",
    "register_all_tasks",
]