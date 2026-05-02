# ============================================================
# File   : trading/summary/recovery/checkpoints.py
# Ver    : PRODUCTION-STABLE-REV1-CHECKPOINTS-SPLIT
# ------------------------------------------------------------
# ✔ anchor / max_allowed_dt 解決
# ✔ datetime normalize
# ✔ checkpoint freshness 判定
# ✔ delta-empty skip 判定
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def normalize_dt_like(value):
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if ts is None or pd.isna(ts):
            return None
        if getattr(ts, "tzinfo", None) is not None:
            try:
                ts = ts.tz_localize(None)
            except Exception:
                try:
                    ts = ts.tz_convert(None)
                except Exception:
                    pass
        return ts
    except Exception:
        return None


def resolve_anchor_context(dates) -> tuple[Optional[object], Optional[pd.Timestamp]]:
    """
    loader に渡す閉場日クランプ用コンテキストを作る。
    dates の最大日付を anchor_day とし、15:35 を上限候補にする。
    """
    try:
        if not dates:
            return None, None

        anchor_day = max(dates)
        max_allowed_dt = pd.Timestamp(f"{anchor_day} 15:35:00")
        return anchor_day, max_allowed_dt
    except Exception:
        logger.exception("[summary_recovery] resolve anchor context failed dates=%s", dates)
        return None, None


def checkpoint_is_fresh(last_dt, interval: int, anchor_day, max_allowed_dt) -> bool:
    """
    delta_push が空でも、その interval のDB最終時刻が十分新しければ
    preload/rebuild を省略できるとみなす。
    """
    try:
        ts = normalize_dt_like(last_dt)
        if ts is None:
            return False

        if anchor_day is not None and ts.date() != anchor_day:
            return False

        if max_allowed_dt is not None:
            madt = normalize_dt_like(max_allowed_dt)
            if madt is not None and ts > madt:
                ts = madt

        interval = int(interval)

        if interval == 1:
            threshold = pd.Timestamp(f"{anchor_day} 15:30:00")
        elif interval == 3:
            threshold = pd.Timestamp(f"{anchor_day} 15:33:00")
        elif interval == 5:
            threshold = pd.Timestamp(f"{anchor_day} 15:35:00")
        else:
            return False

        ok = ts >= threshold
        logger.info(
            "[summary_recovery] checkpoint fresh check interval=%s last_dt=%s threshold=%s ok=%s",
            interval,
            ts,
            threshold,
            ok,
        )
        return bool(ok)
    except Exception:
        logger.exception(
            "[summary_recovery] checkpoint fresh check failed interval=%s last_dt=%s",
            interval,
            last_dt,
        )
        return False


def can_skip_rebuild_when_delta_empty(
    *,
    delta_push_empty: bool,
    startup_delta_only: bool,
    last_1m_dt,
    last_3m_dt,
    last_5m_dt,
    anchor_day,
    max_allowed_dt,
) -> bool:
    try:
        if not startup_delta_only or not delta_push_empty:
            return False

        fresh_1m = checkpoint_is_fresh(last_1m_dt, 1, anchor_day, max_allowed_dt)
        fresh_3m = checkpoint_is_fresh(last_3m_dt, 3, anchor_day, max_allowed_dt)
        fresh_5m = checkpoint_is_fresh(last_5m_dt, 5, anchor_day, max_allowed_dt)

        logger.info(
            "[summary_recovery] skip-check delta_empty=%s fresh_1m=%s fresh_3m=%s fresh_5m=%s",
            delta_push_empty,
            fresh_1m,
            fresh_3m,
            fresh_5m,
        )
        return bool(fresh_1m and fresh_3m and fresh_5m)
    except Exception:
        logger.exception("[summary_recovery] can_skip_rebuild_when_delta_empty failed")
        return False