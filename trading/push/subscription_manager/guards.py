# ============================================================
# File   : trading/push/subscription_manager/guards.py
# Function:
#   - on_open 理由判定
#   - push stale 判定
#   - last_push_received_at / push_df 行数の安全取得
# ------------------------------------------------------------
# Notes:
#   - current=100 target=100 でも PUSH stale なら skip しない
#   - on_open guard の二次判定に利用する
# ============================================================

from __future__ import annotations

import logging
import time

import pandas as pd

from .globals_access import safe_get_global_data, safe_getattr

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL_SEC = 30.0
ON_OPEN_MIN_REFRESH_GAP_SEC = 4.0
ON_OPEN_FORCE_SKIP_IF_UNCHANGED = True

PUSH_STALE_SEC = 20.0
PUSH_STALE_ON_OPEN_SEC = 8.0


def reason_key(reason: str) -> str:
    s = str(reason).strip().lower()
    return s or "unknown"


def is_on_open_reason(reason: str) -> bool:
    s = reason_key(reason)
    return s in ("on_open", "market_open", "open", "startup_on_open")


def get_last_push_received_ts() -> float:
    gd = safe_get_global_data()
    if gd is None:
        return 0.0

    candidates = [
        "last_push_received_at",
        "push_last_received_at",
        "last_recv_ts",
        "push_last_recv_ts",
    ]

    for name in candidates:
        v = safe_getattr(gd, name, None)
        if v is None:
            continue

        if isinstance(v, (int, float)):
            ts = float(v)
            if ts > 0:
                return ts

        try:
            if hasattr(v, "timestamp"):
                ts = float(v.timestamp())
                if ts > 0:
                    return ts
        except Exception:
            pass

        try:
            s = str(v).strip()
            if s:
                dt_obj = pd.to_datetime(s, errors="coerce")
                if pd.notna(dt_obj):
                    return float(dt_obj.timestamp())
        except Exception:
            pass

    return 0.0


def get_push_row_count() -> int:
    gd = safe_get_global_data()
    if gd is None:
        return 0

    try:
        df = None
        getter = safe_getattr(gd, "get_push_df", None)
        if callable(getter):
            df = getter()
        elif hasattr(gd, "push_df"):
            df = safe_getattr(gd, "push_df", None)

        if isinstance(df, pd.DataFrame):
            return int(len(df))
    except Exception:
        pass

    return 0


def is_push_stale(reason: str = "") -> bool:
    now = time.time()
    last_ts = get_last_push_received_ts()
    rows = get_push_row_count()

    if last_ts <= 0:
        logger.warning(
            "[SUB MANAGER] push stale: no last_push_received_at reason=%s rows=%d",
            reason,
            rows,
        )
        return True

    age = max(0.0, now - last_ts)
    threshold = PUSH_STALE_ON_OPEN_SEC if is_on_open_reason(reason) else PUSH_STALE_SEC

    stale = age >= threshold
    if stale:
        logger.warning(
            "[SUB MANAGER] push stale detected reason=%s age=%.1fs threshold=%.1fs rows=%d",
            reason,
            age,
            threshold,
            rows,
        )
    else:
        logger.info(
            "[SUB MANAGER] push freshness ok reason=%s age=%.1fs threshold=%.1fs rows=%d",
            reason,
            age,
            threshold,
            rows,
        )

    return stale