# ============================================================
# File   : core/startup/summary_runtime_pkg/db_seed_anchor.py
# Version: REV2.0-SUMMARY-RUNTIME-DB-SEED-ANCHOR-FUTURE-CUTOFF
# ------------------------------------------------------------
# 【概要】
#   起動時 summary DB seed 用の target date / DB path 解決
#
# 【主な機能】
#   ✔ 前営業日を含む target_dates
#   ✔ anchor_day / max_allowed_dt 解決
#   ✔ 現在 summary_engine の DB path 推定
#   ✔ summaryYYYYMMDD.db の path 解決
#   ✔ リアルタイム起動時の未来足 cutoff
#
# 【REV2.0 修正】
#   ✔ resolve_anchor_context() が 15:35 を返しても、
#     現在時刻ベースの safe cutoff で clamp する
#
#   ✔ 13:03 起動時に max_allowed_dt=15:35 となり、
#     3min=15:33 / 5min=15:35 が seed に混入する問題を修正
#
#   ✔ 当日 anchor_day の場合のみ runtime cutoff を適用
#      - 前営業日 DB は 15:30 まで読んでよい
#      - 当日 DB は現在時刻より未来を読まない
#
# 【重要】
#   - summary seed は履歴確保のため前営業日を読む
#   - ただし当日分は現在時刻より未来を読んではいけない
#   - 大引け後だけ 15:30 まで許可
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# datetime helpers
# ============================================================

def _to_timestamp(value: Any) -> Optional[pd.Timestamp]:
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None

        ts = pd.Timestamp(ts)

        try:
            if ts.tzinfo is not None:
                ts = ts.tz_localize(None)
        except Exception:
            try:
                ts = pd.Timestamp(ts.to_pydatetime().replace(tzinfo=None))
            except Exception:
                pass

        return ts.replace(second=0, microsecond=0)

    except Exception:
        return None


def _to_date(value: Any) -> Optional[dt.date]:
    try:
        ts = _to_timestamp(value)
        if ts is None:
            return None
        return ts.date()
    except Exception:
        return None


def _now_ts() -> pd.Timestamp:
    return pd.Timestamp.now().replace(second=0, microsecond=0)


def _runtime_cutoff_now() -> pd.Timestamp:
    """
    現在時刻から見て、当日リアルタイム処理で許可する最大 datetime。

    優先:
      utils.market_time.get_intraday_cutoff_datetime

    fallback:
      09:00前       now
      09:00-11:30  now
      11:30-12:30  11:30
      12:30-15:30  now
      15:30後       15:30
    """
    now = _now_ts()

    try:
        from utils.market_time import get_intraday_cutoff_datetime

        cutoff = get_intraday_cutoff_datetime(now.to_pydatetime())
        ts = _to_timestamp(cutoff)
        if ts is not None:
            return ts

    except Exception:
        logger.debug(
            "[summary_runtime] get_intraday_cutoff_datetime failed -> fallback",
            exc_info=True,
        )

    d = now.date()
    t = now.time()

    if t < dt.time(9, 0):
        return now

    if dt.time(9, 0) <= t <= dt.time(11, 30):
        return now

    if dt.time(11, 30) < t < dt.time(12, 30):
        return pd.Timestamp(dt.datetime.combine(d, dt.time(11, 30)))

    if dt.time(12, 30) <= t <= dt.time(15, 30):
        return now

    return pd.Timestamp(dt.datetime.combine(d, dt.time(15, 30)))


def _clamp_max_allowed_dt(
    max_allowed_dt: Any,
    *,
    anchor_day: Any,
    label: str,
) -> Optional[pd.Timestamp]:
    """
    resolve_anchor_context() が返した max_allowed_dt を、
    当日リアルタイム cutoff で clamp する。

    重要:
      - anchor_day が今日の場合のみ clamp
      - 前営業日 anchor の場合は、その日の終値まで読んでよい
    """
    original = _to_timestamp(max_allowed_dt)
    anchor_date = _to_date(anchor_day)
    today = _now_ts().date()

    if original is None:
        runtime_cutoff = _runtime_cutoff_now()
        logger.warning(
            "[summary_runtime] %s max_allowed_dt invalid -> use runtime_cutoff=%s anchor_day=%s",
            label,
            runtime_cutoff,
            anchor_day,
        )
        return runtime_cutoff

    if anchor_date != today:
        logger.info(
            "[summary_runtime] %s max_allowed_dt keep non-today anchor_day=%s max_allowed_dt=%s",
            label,
            anchor_day,
            original,
        )
        return original

    runtime_cutoff = _runtime_cutoff_now()
    corrected = min(original, runtime_cutoff)

    if corrected != original:
        logger.warning(
            "[summary_runtime] %s max_allowed_dt clamped by runtime cutoff "
            "anchor_day=%s original=%s runtime_cutoff=%s corrected=%s",
            label,
            anchor_day,
            original,
            runtime_cutoff,
            corrected,
        )
    else:
        logger.info(
            "[summary_runtime] %s max_allowed_dt ok "
            "anchor_day=%s max_allowed_dt=%s runtime_cutoff=%s",
            label,
            anchor_day,
            original,
            runtime_cutoff,
        )

    return corrected


# ============================================================
# public API
# ============================================================

def resolve_anchor_for_seed():
    """
    起動時 summary DB seed 用の対象日を決める。

    当日だけではテクニカル指標用の履歴が足りないため、
    前営業日も含める。

    ただし、当日分は現在時刻より未来を読まない。
    """
    try:
        from trading.summary.recovery.helpers import target_dates
        from trading.summary.recovery.checkpoints import resolve_anchor_context

        dates = target_dates(include_previous_business_day=True)
        anchor_day, raw_max_allowed_dt = resolve_anchor_context(dates)

        max_allowed_dt = _clamp_max_allowed_dt(
            raw_max_allowed_dt,
            anchor_day=anchor_day,
            label="seed_anchor",
        )

        logger.info(
            "[summary_runtime] seed anchor resolved include_previous_business_day=True "
            "dates=%s anchor_day=%s raw_max_allowed_dt=%s max_allowed_dt=%s",
            dates,
            anchor_day,
            raw_max_allowed_dt,
            max_allowed_dt,
        )

        return dates, anchor_day, max_allowed_dt

    except Exception:
        logger.debug("[summary_runtime] resolve anchor for seed failed", exc_info=True)
        return None, None, None


def coerce_dates_to_yyyymmdd(dates: Optional[Iterable[Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    for d in dates or []:
        try:
            ts = pd.to_datetime(d, errors="coerce")
            if pd.isna(ts):
                continue
            ymd = ts.strftime("%Y%m%d")
            if ymd not in seen:
                seen.add(ymd)
                out.append(ymd)
        except Exception:
            continue

    return out


def get_current_summary_db_path() -> Optional[str]:
    """
    現在 rebind 済みの summary_engine から DB path を推定する。
    """
    try:
        from database.session import get_summary_engine

        engine = get_summary_engine()
        db = getattr(getattr(engine, "url", None), "database", None)
        if db:
            return str(db)
    except Exception:
        logger.debug("[summary_runtime] get_summary_engine db path failed", exc_info=True)

    try:
        from database import session as db_session

        engine = getattr(db_session, "summary_engine", None)
        db = getattr(getattr(engine, "url", None), "database", None)
        if db:
            return str(db)
    except Exception:
        logger.debug("[summary_runtime] summary_engine db path fallback failed", exc_info=True)

    return None


def derive_summary_db_paths_for_dates(dates: Optional[Iterable[Any]]) -> list[str]:
    """
    summaryYYYYMMDD.db のパス一覧を作る。

    現在の summary_engine が
      \\...\\summary\\summary20260421.db
    なら、dates=['2026-04-20','2026-04-21'] から
      \\...\\summary\\summary20260420.db
      \\...\\summary\\summary20260421.db
    を作る。
    """
    cur = get_current_summary_db_path()
    if not cur:
        return []

    cur_path = Path(cur)
    parent = cur_path.parent

    ymds = coerce_dates_to_yyyymmdd(dates)
    paths: list[str] = []
    seen: set[str] = set()

    for ymd in ymds:
        p = str(parent / f"summary{ymd}.db")
        if p not in seen:
            seen.add(p)
            paths.append(p)

    cur_s = str(cur_path)
    if cur_s not in seen:
        paths.append(cur_s)

    existing: list[str] = []
    for p in paths:
        if os.path.exists(p):
            existing.append(p)
        else:
            logger.warning("[summary_runtime] summary seed db file not found path=%s", p)

    logger.info(
        "[summary_runtime] summary seed db paths resolved count=%d paths=%s",
        len(existing),
        existing,
    )

    return existing


__all__ = [
    "resolve_anchor_for_seed",
    "coerce_dates_to_yyyymmdd",
    "get_current_summary_db_path",
    "derive_summary_db_paths_for_dates",
]