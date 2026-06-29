# ============================================================
# File   : core/startup/summary_db_realtime_priority_patch.py
# Version: V1-DB-REALTIME-1M-FIRST
# ------------------------------------------------------------
# main_database.py / summary_database_runner の場中 summary 遅延対策。
#
# 問題:
#   summary_database_runner が 1m と 3m/5m、ranking summary を同じtickで
#   待つと、1m summary の latest_dt が 5〜8分遅れ、main.py 側では
#   SUMMARY_AI candidate が stale となり発注前にスキップされる。
#
# 方針:
#   DB/data collector プロセスでは、場中の time-locked summary 対象を
#   1分足 push summary に寄せる。
#   3m/5m は main.py 側の MTF fallback / 別tick / 後続処理に任せ、
#   まず1m DB鮮度を守る。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Callable

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_RESOLVE_TARGET_INTERVALS: Callable[..., Any] | None = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
    except Exception:
        return ""


def _is_db_process() -> bool:
    try:
        argv = _argv_text()
        if any(
            x in argv
            for x in (
                "main_database.py",
                "data_collectors_runner.py",
                "summary_database_runner.py",
                "push_receiver_runner.py",
                "ranking_collector_runner.py",
                "yahoo_complement_runner.py",
                "db_prepare_runner.py",
            )
        ):
            return True
        return any(
            _env_bool(k, False)
            for k in (
                "AUTOSTOCK_MAIN_DATABASE_PROCESS",
                "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
                "AUTOSTOCK_SUMMARY_DB_WRITER",
                "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
            )
        )
    except Exception:
        return False


def _is_market_session(now: Any) -> bool:
    try:
        from scheduler_jobs.summary.time_utils import is_market_session

        return bool(is_market_session(now))
    except Exception:
        return True


def _enabled() -> bool:
    return _env_bool("SUMMARY_DB_REALTIME_PRIORITY_ENABLED", True) and _is_db_process()


def _force_env_defaults() -> None:
    """DB側では1m保存を詰まらせる設定を既定で抑える。"""
    defaults = {
        # time-locked summary 内で ranking summary を待たない。
        "SUMMARY_PARALLEL_RANKING_ENABLED": "0",
        # 3m/5m を同じtickで強制しない。
        "SUMMARY_PARALLEL_FORCE_1_3_5": "0",
        "SUMMARY_PUSH_DISPLAY_ALL_INTERVALS": "0",
        "SUMMARY_PUSH_BG_ALL_INTERVALS": "0",
        "SUMMARY_PUSH_BG_LONG_INTERVALS": "0",
        # DB側は長時間待たず、次tickに鮮度を渡す。
        "SUMMARY_PARALLEL_INTERVAL_WORKERS": "1",
        "SUMMARY_PUSH_BG_INTERVAL_WORKERS": "1",
        "SUMMARY_PARALLEL_TIMEOUT_MIN_SEC": "15",
        "SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC": "25",
        # 表示/DiscordよりDB保存優先。
        "SUMMARY_DB_REALTIME_1M_ONLY_IN_SESSION": "1",
    }
    for k, v in defaults.items():
        try:
            old = os.getenv(k)
            if old is None or str(old).strip() == "":
                os.environ[k] = v
                logger.warning("[SUMMARY DB REALTIME PRIORITY] env default set %s=%s", k, v)
        except Exception:
            pass


def _patched_resolve_target_intervals(now=None):
    try:
        if _enabled() and _env_bool("SUMMARY_DB_REALTIME_1M_ONLY_IN_SESSION", True) and _is_market_session(now):
            logger.warning(
                "[SUMMARY DB REALTIME PRIORITY] force target intervals=[1] reason=db_realtime_1m_first now=%s",
                now,
            )
            return [1]
    except Exception:
        logger.debug("[SUMMARY DB REALTIME PRIORITY] realtime target check failed", exc_info=True)

    if callable(_ORIG_RESOLVE_TARGET_INTERVALS):
        try:
            return _ORIG_RESOLVE_TARGET_INTERVALS(now)
        except TypeError:
            return _ORIG_RESOLVE_TARGET_INTERVALS()
    return [1]


def install() -> bool:
    global _INSTALLED, _ORIG_RESOLVE_TARGET_INTERVALS
    if _INSTALLED:
        return True
    try:
        if not _enabled():
            logger.warning(
                "[SUMMARY DB REALTIME PRIORITY] skipped enabled=%s db_process=%s argv=%s",
                os.getenv("SUMMARY_DB_REALTIME_PRIORITY_ENABLED", "1"),
                _is_db_process(),
                sys.argv,
            )
            _INSTALLED = True
            return False

        _force_env_defaults()

        import scheduler_jobs.summary.time_utils as tu

        cur = getattr(tu, "resolve_target_intervals", None)
        if getattr(cur, "_summary_db_realtime_priority_v1", False):
            _INSTALLED = True
            return True

        _ORIG_RESOLVE_TARGET_INTERVALS = cur
        _patched_resolve_target_intervals._summary_db_realtime_priority_v1 = True  # type: ignore[attr-defined]
        tu.resolve_target_intervals = _patched_resolve_target_intervals

        _INSTALLED = True
        logger.warning(
            "[SUMMARY DB REALTIME PRIORITY] installed v1 db_process=%s ranking_enabled=%s 1m_only=%s timeout=%s",
            _is_db_process(),
            os.getenv("SUMMARY_PARALLEL_RANKING_ENABLED"),
            os.getenv("SUMMARY_DB_REALTIME_1M_ONLY_IN_SESSION"),
            os.getenv("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC"),
        )
        return True
    except Exception as e:
        logger.exception("[SUMMARY DB REALTIME PRIORITY] install failed err=%s", e)
        return False


try:
    install()
except Exception as e:
    logger.exception("[SUMMARY DB REALTIME PRIORITY] auto install failed err=%s", e)


__all__ = ["install"]
