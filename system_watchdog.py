# ============================================================
# File   : system_watchdog.py
# Version: Ver2.1-PRODUCTION-HARDENED-WATCHDOG-TZSAFE
# ------------------------------------------------------------
# ✔ push stream watchdog
# ✔ incremental engine watchdog
# ✔ ranking watchdog
# ✔ summary watchdog
# ✔ scheduler watchdog
# ✔ auto logging
# ✔ datetime / str / dict 自動変換
# ✔ pandas Timestamp 対応
# ✔ NaN / None 防御
# ✔ naive / aware datetime 混在防御
# ✔ ISO8601 / Z / timezone-aware 対応
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
import math
from typing import Any, Optional

import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# Watchdog thresholds
# ============================================================

PUSH_TIMEOUT_SEC = 10
SUMMARY_TIMEOUT_SEC = 120
RANKING_TIMEOUT_SEC = 120
ENGINE_TIMEOUT_SEC = 30
SCHEDULER_TIMEOUT_SEC = 120


# ============================================================
# datetime helpers
# ============================================================

def _is_nan_like(value: Any) -> bool:
    try:
        if value is None:
            return True

        if isinstance(value, float) and math.isnan(value):
            return True

        # pandas / numpy NaT, NaN
        if pd.isna(value):
            return True

        return False
    except Exception:
        return False


def _normalize_iso_string(value: str) -> str:
    s = str(value).strip()
    if not s:
        return s

    # ISO 8601 "Z" 対応
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    return s


# ============================================================
# datetime safety converter
# ============================================================

def _safe_to_datetime(value: Any) -> Optional[dt.datetime]:
    try:
        if _is_nan_like(value):
            return None

        # dict support
        if isinstance(value, dict):
            if "datetime" in value:
                return _safe_to_datetime(value.get("datetime"))
            if "time" in value:
                return _safe_to_datetime(value.get("time"))
            if "timestamp" in value:
                return _safe_to_datetime(value.get("timestamp"))
            return None

        # already datetime
        if isinstance(value, dt.datetime):
            return value

        # pandas Timestamp
        if isinstance(value, pd.Timestamp):
            try:
                return value.to_pydatetime()
            except Exception:
                logger.exception("[watchdog] pandas Timestamp conversion failed")
                return None

        # string
        if isinstance(value, str):
            s = _normalize_iso_string(value)
            if not s:
                return None

            # まず pandas で広く吸収
            try:
                parsed = pd.to_datetime(s, errors="coerce")
                if pd.isna(parsed):
                    return None

                if isinstance(parsed, pd.Timestamp):
                    return parsed.to_pydatetime()

                # 稀な型にも念のため対応
                if hasattr(parsed, "to_pydatetime"):
                    return parsed.to_pydatetime()

            except Exception:
                logger.exception("[watchdog] string datetime conversion failed value=%r", value)
                return None

        return None

    except Exception:
        logger.exception("[watchdog] datetime conversion failed")
        return None


def _now_matching(last_dt: dt.datetime) -> dt.datetime:
    """
    last_dt が aware なら同じ tz の aware now を返す。
    last_dt が naive なら naive now を返す。
    """
    try:
        if (
            isinstance(last_dt, dt.datetime)
            and last_dt.tzinfo is not None
            and last_dt.utcoffset() is not None
        ):
            return dt.datetime.now(last_dt.tzinfo)

        return dt.datetime.now()

    except Exception:
        logger.exception("[watchdog] now matching failed")
        return dt.datetime.now()


# ============================================================
# safe delta
# ============================================================

def _safe_delta(last_dt: Any) -> Optional[float]:
    try:
        last_dt = _safe_to_datetime(last_dt)

        if last_dt is None:
            return None

        now_dt = _now_matching(last_dt)
        delta = (now_dt - last_dt).total_seconds()

        if delta is None:
            return None

        if isinstance(delta, float) and math.isnan(delta):
            return None

        # 未来時刻が入った場合の防御
        if delta < 0:
            logger.warning(
                "[watchdog] negative delta detected now=%s last=%s delta=%.3f",
                now_dt,
                last_dt,
                delta,
            )
            return 0.0

        return float(delta)

    except Exception:
        logger.exception("[watchdog] delta calculation failed")
        return None


# ============================================================
# Push watchdog
# ============================================================

def check_push_stream() -> None:
    try:
        last_push = getattr(global_data, "last_push_time", None)

        delta = _safe_delta(last_push)

        if delta is None:
            return

        if delta > PUSH_TIMEOUT_SEC:
            logger.error(
                "[WATCHDOG] PUSH STREAM STOPPED %.1f sec",
                delta,
            )

    except Exception:
        logger.exception("[watchdog_push]")


# ============================================================
# Summary watchdog
# ============================================================

def check_summary() -> None:
    try:
        df = global_data.get_merged_summary(1)

        if df is None:
            return

        if not hasattr(df, "empty") or df.empty:
            return

        if "datetime" not in df.columns:
            return

        last_dt = df["datetime"].max()

        delta = _safe_delta(last_dt)

        if delta is None:
            return

        if delta > SUMMARY_TIMEOUT_SEC:
            logger.error(
                "[WATCHDOG] SUMMARY FREEZE %.1f sec",
                delta,
            )

    except Exception:
        logger.exception("[watchdog_summary]")


# ============================================================
# Ranking watchdog
# ============================================================

def check_ranking() -> None:
    try:
        df = getattr(global_data, "latest_ranking_raw", None)

        if df is None:
            return

        if hasattr(df, "empty") and df.empty:
            return

        if not hasattr(df, "columns"):
            return

        if "snapshot_time" not in df.columns:
            return

        last_dt = df["snapshot_time"].max()

        delta = _safe_delta(last_dt)

        if delta is None:
            return

        if delta > RANKING_TIMEOUT_SEC:
            logger.error(
                "[WATCHDOG] RANKING FREEZE %.1f sec",
                delta,
            )

    except Exception:
        logger.exception("[watchdog_ranking]")


# ============================================================
# Incremental engine watchdog
# ============================================================

def check_incremental_engine() -> None:
    try:
        last_engine = getattr(global_data, "last_incremental_run", None)

        delta = _safe_delta(last_engine)

        if delta is None:
            return

        if delta > ENGINE_TIMEOUT_SEC:
            logger.error(
                "[WATCHDOG] INCREMENTAL ENGINE STOPPED %.1f sec",
                delta,
            )

    except Exception:
        logger.exception("[watchdog_engine]")


# ============================================================
# Scheduler watchdog
# ============================================================

def check_scheduler() -> None:
    try:
        last_scheduler = getattr(global_data, "last_scheduler_run", None)

        delta = _safe_delta(last_scheduler)

        if delta is None:
            return

        if delta > SCHEDULER_TIMEOUT_SEC:
            logger.error(
                "[WATCHDOG] SCHEDULER STOPPED %.1f sec",
                delta,
            )

    except Exception:
        logger.exception("[watchdog_scheduler]")


# ============================================================
# Watchdog main
# ============================================================

def run_system_watchdog() -> None:
    try:
        check_push_stream()
        check_incremental_engine()
        check_ranking()
        check_summary()
        check_scheduler()

    except Exception:
        logger.exception("[system_watchdog]")