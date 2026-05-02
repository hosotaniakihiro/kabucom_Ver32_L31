# ============================================================
# File   : trading/summary/persistence/core/sqlite_normalize.py
# Version: PRODUCTION-STABLE-REV1.0
# ------------------------------------------------------------
# Purpose:
#   pandas.Timestamp / NaT / numpy scalar / date / time を
#   SQLite bind 可能値へ正規化する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

try:
    import pandas as pd  # type: ignore
except Exception:
    pd = None  # type: ignore


def is_null_like(v: Any) -> bool:
    if v is None:
        return True

    try:
        if pd is not None:
            if v is pd.NaT:
                return True
            if pd.isna(v):
                return True
    except Exception:
        pass

    try:
        if isinstance(v, float) and math.isnan(v):
            return True
    except Exception:
        pass

    return False


def normalize_datetime_value(v: Any) -> Optional[str]:
    if is_null_like(v):
        return None

    try:
        if isinstance(v, dt.datetime):
            x = v
            if x.tzinfo is not None:
                x = x.replace(tzinfo=None)
            return x.strftime("%Y-%m-%d %H:%M:%S")

        if pd is not None and isinstance(v, pd.Timestamp):
            x = v
            try:
                if x.tzinfo is not None:
                    try:
                        x = x.tz_convert(None)
                    except Exception:
                        x = x.tz_localize(None)
            except Exception:
                pass
            return x.to_pydatetime().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

        if isinstance(v, dt.date):
            return dt.datetime.combine(v, dt.time.min).strftime("%Y-%m-%d %H:%M:%S")

        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None

            if pd is not None:
                x = pd.to_datetime(s, errors="coerce")
                if not is_null_like(x):
                    if isinstance(x, pd.Timestamp):
                        try:
                            if x.tzinfo is not None:
                                try:
                                    x = x.tz_convert(None)
                                except Exception:
                                    x = x.tz_localize(None)
                        except Exception:
                            pass
                        return x.to_pydatetime().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

            return s

    except Exception:
        logger.debug("[UPSERT] datetime normalize failed value=%r", v, exc_info=True)

    try:
        if pd is not None:
            x = pd.to_datetime(v, errors="coerce")
            if not is_null_like(x) and isinstance(x, pd.Timestamp):
                return x.to_pydatetime().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        logger.debug("[UPSERT] datetime normalize fallback failed value=%r", v, exc_info=True)

    return None


def normalize_date_value(v: Any) -> Optional[str]:
    if is_null_like(v):
        return None

    try:
        if isinstance(v, dt.date) and not isinstance(v, dt.datetime):
            return v.strftime("%Y-%m-%d")

        if isinstance(v, dt.datetime):
            return v.date().strftime("%Y-%m-%d")

        if pd is not None and isinstance(v, pd.Timestamp):
            return v.to_pydatetime().date().strftime("%Y-%m-%d")

        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None

            if pd is not None:
                x = pd.to_datetime(s, errors="coerce")
                if not is_null_like(x) and isinstance(x, pd.Timestamp):
                    return x.to_pydatetime().date().strftime("%Y-%m-%d")

            return s

    except Exception:
        logger.debug("[UPSERT] date normalize failed value=%r", v, exc_info=True)

    return None


def normalize_time_value(v: Any) -> Optional[str]:
    if is_null_like(v):
        return None

    try:
        if isinstance(v, dt.time):
            return v.replace(tzinfo=None).strftime("%H:%M:%S")

        if isinstance(v, dt.datetime):
            return v.time().strftime("%H:%M:%S")

        if pd is not None and isinstance(v, pd.Timestamp):
            return v.to_pydatetime().time().strftime("%H:%M:%S")

        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            if len(s) == 5:
                return s + ":00"
            return s

    except Exception:
        logger.debug("[UPSERT] time normalize failed value=%r", v, exc_info=True)

    return None


def normalize_scalar_value(v: Any) -> Any:
    if is_null_like(v):
        return None

    try:
        if isinstance(v, bool):
            return int(v)

        if isinstance(v, (str, int, float)):
            if isinstance(v, float) and math.isnan(v):
                return None
            return v

        if isinstance(v, dt.datetime):
            return normalize_datetime_value(v)

        if isinstance(v, dt.date):
            return normalize_date_value(v)

        if isinstance(v, dt.time):
            return normalize_time_value(v)

        if pd is not None and isinstance(v, pd.Timestamp):
            return normalize_datetime_value(v)

        if hasattr(v, "item"):
            try:
                x = v.item()
                if x is not v:
                    return normalize_scalar_value(x)
            except Exception:
                pass

    except Exception:
        logger.debug("[UPSERT] scalar normalize failed value=%r", v, exc_info=True)

    return v


def normalize_row_for_sqlite(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    for k, v in row.items():
        lk = str(k).lower()

        if lk in {"datetime", "last_update", "updated_at", "created_at"}:
            out[k] = normalize_datetime_value(v)
        elif lk in {"date"}:
            out[k] = normalize_date_value(v)
        elif lk in {"time", "start_time", "end_time"}:
            out[k] = normalize_time_value(v)
        else:
            out[k] = normalize_scalar_value(v)

    return out


def normalize_rows_for_sqlite(rows: Sequence[dict]) -> List[dict]:
    return [normalize_row_for_sqlite(dict(r)) for r in rows if isinstance(r, dict) and r]