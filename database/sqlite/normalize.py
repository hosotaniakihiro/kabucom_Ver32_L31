# ============================================================
# File   : database/sqlite/normalize.py
# Version: PRODUCTION-STABLE-REV1.1-SQLITE-NORMALIZE-SUMMARY-UPSERT
# ------------------------------------------------------------
# Purpose:
#   pandas.Timestamp / NaT / numpy scalar / date / time を
#   SQLite bind 可能値へ正規化する。
#
# Add:
#   - stock_summary_*min UPSERT 前の DataFrame 正規化
#   - datetime から date / time / time_range を補完
#   - OHLC alias open_price/high_price/low_price/close_price を補完
#   - symbol/datetime 重複除去
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence

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
                if not is_null_like(x) and isinstance(x, pd.Timestamp):
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
        logger.debug("[SQLITE NORMALIZE] datetime normalize failed value=%r", v, exc_info=True)

    try:
        if pd is not None:
            x = pd.to_datetime(v, errors="coerce")
            if not is_null_like(x) and isinstance(x, pd.Timestamp):
                return x.to_pydatetime().replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        logger.debug("[SQLITE NORMALIZE] datetime normalize fallback failed value=%r", v, exc_info=True)

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
        logger.debug("[SQLITE NORMALIZE] date normalize failed value=%r", v, exc_info=True)

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
        logger.debug("[SQLITE NORMALIZE] time normalize failed value=%r", v, exc_info=True)

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
        logger.debug("[SQLITE NORMALIZE] scalar normalize failed value=%r", v, exc_info=True)

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


# ============================================================
# Summary DataFrame normalize
# ============================================================

OHLC_ALIAS_MAP = {
    "open": "open_price",
    "high": "high_price",
    "low": "low_price",
    "close": "close_price",
}


def _require_pandas() -> None:
    if pd is None:
        raise RuntimeError("[SQLITE NORMALIZE] pandas is required for DataFrame normalization")


def _series_blank_or_null(s: Any) -> bool:
    try:
        return bool(s.isna().any() or (s.astype(str).str.strip() == "").any())
    except Exception:
        return True


def normalize_summary_df_for_sqlite_upsert(
    df: Any,
    *,
    interval: Optional[int | str] = None,
    source: Optional[str] = None,
    table_name: Optional[str] = None,
    keep_columns: Optional[Iterable[str]] = None,
) -> Any:
    """
    stock_summary_*min へ UPSERT する直前の DataFrame を正規化する。

    主な目的:
      - NOT NULL constraint failed: stock_summary_1min.date を防ぐ
      - datetime から date / time / time_range を必ず補完する
      - open/high/low/close と open_price/high_price/low_price/close_price を相互補完する
      - symbol/datetime の欠損・重複を除外する

    Parameters
    ----------
    df:
        pandas.DataFrame
    interval:
        1 / 3 / 5 など
    source:
        push_stream_direct_ohlc_fallback など
    table_name:
        ログ用
    keep_columns:
        DB実カラム一覧。指定した場合、存在する列だけに絞る。

    Returns
    -------
    pandas.DataFrame
    """
    _require_pandas()

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            logger.exception("[SQLITE NORMALIZE] failed to convert object to DataFrame table=%s", table_name)
            return pd.DataFrame()

    if df.empty:
        return df.copy()

    out = df.copy()

    # --------------------------------------------------------
    # symbol
    # --------------------------------------------------------
    if "symbol" not in out.columns:
        raise ValueError("[SQLITE NORMALIZE] required column missing: symbol")

    out["symbol"] = out["symbol"].astype(str).str.strip()
    out = out[
        out["symbol"].notna()
        & (out["symbol"] != "")
        & (out["symbol"].str.lower() != "nan")
        & (out["symbol"].str.lower() != "none")
    ].copy()

    if out.empty:
        return out

    # --------------------------------------------------------
    # datetime
    # --------------------------------------------------------
    if "datetime" not in out.columns:
        for alt in ("dt", "timestamp", "created_at", "updated_at", "inserted_at", "snapshot_time"):
            if alt in out.columns:
                out["datetime"] = out[alt]
                break

    if "datetime" not in out.columns:
        raise ValueError("[SQLITE NORMALIZE] required column missing: datetime")

    dt_ser = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.loc[dt_ser.notna()].copy()

    if out.empty:
        return out

    dt_ser = pd.to_datetime(out["datetime"], errors="coerce")
    out["datetime"] = dt_ser.dt.strftime("%Y-%m-%d %H:%M:%S")

    # --------------------------------------------------------
    # date / time / time_range
    # --------------------------------------------------------
    dt_ser = pd.to_datetime(out["datetime"], errors="coerce")

    if "date" not in out.columns or _series_blank_or_null(out["date"]):
        out["date"] = dt_ser.dt.strftime("%Y-%m-%d")
    else:
        out["date"] = out["date"].map(normalize_date_value)
        mask = out["date"].isna() | (out["date"].astype(str).str.strip() == "")
        if mask.any():
            out.loc[mask, "date"] = dt_ser.loc[mask].dt.strftime("%Y-%m-%d")

    if "time" not in out.columns or _series_blank_or_null(out["time"]):
        out["time"] = dt_ser.dt.strftime("%H:%M:%S")
    else:
        out["time"] = out["time"].map(normalize_time_value)
        mask = out["time"].isna() | (out["time"].astype(str).str.strip() == "")
        if mask.any():
            out.loc[mask, "time"] = dt_ser.loc[mask].dt.strftime("%H:%M:%S")

    if "time_range" not in out.columns or _series_blank_or_null(out["time_range"]):
        out["time_range"] = dt_ser.dt.strftime("%H:%M")
    else:
        mask = out["time_range"].isna() | (out["time_range"].astype(str).str.strip() == "")
        if mask.any():
            out.loc[mask, "time_range"] = dt_ser.loc[mask].dt.strftime("%H:%M")

    # --------------------------------------------------------
    # OHLC alias
    # --------------------------------------------------------
    for src, dst in OHLC_ALIAS_MAP.items():
        if src in out.columns and dst not in out.columns:
            out[dst] = out[src]
        elif dst in out.columns and src not in out.columns:
            out[src] = out[dst]

    # --------------------------------------------------------
    # numeric OHLC cleanup
    # --------------------------------------------------------
    ohlc_cols = [c for c in ("open", "high", "low", "close") if c in out.columns]
    if ohlc_cols:
        before = len(out)

        for c in ohlc_cols:
            out[c] = pd.to_numeric(out[c], errors="coerce")

        out = out.dropna(subset=ohlc_cols)

        if "close" in out.columns:
            out = out[out["close"] > 0]

        after = len(out)
        if before != after:
            logger.warning(
                "[SQLITE NORMALIZE] dropped invalid OHLC rows table=%s rows=%s -> %s dropped=%s",
                table_name,
                before,
                after,
                before - after,
            )

    if out.empty:
        return out

    # --------------------------------------------------------
    # interval / source
    # --------------------------------------------------------
    if interval is not None:
        if "interval" not in out.columns:
            out["interval"] = interval
        else:
            mask = out["interval"].isna() | (out["interval"].astype(str).str.strip() == "")
            if mask.any():
                out.loc[mask, "interval"] = interval

    if source is not None:
        if "source" not in out.columns:
            out["source"] = source
        else:
            mask = out["source"].isna() | (out["source"].astype(str).str.strip() == "")
            if mask.any():
                out.loc[mask, "source"] = source

    # --------------------------------------------------------
    # dedupe
    # --------------------------------------------------------
    before = len(out)
    out = out.sort_values(["symbol", "datetime"]).drop_duplicates(["symbol", "datetime"], keep="last")
    after = len(out)

    if before != after:
        logger.info(
            "[SQLITE NORMALIZE] dedupe summary rows table=%s rows=%s -> %s dropped=%s",
            table_name,
            before,
            after,
            before - after,
        )

    # --------------------------------------------------------
    # keep DB columns only
    # --------------------------------------------------------
    if keep_columns is not None:
        keep_columns = list(keep_columns)
        keep = [c for c in keep_columns if c in out.columns]

        critical = ["symbol", "datetime", "date"]
        missing_critical = [c for c in critical if c in keep_columns and c not in keep]

        if missing_critical:
            logger.warning(
                "[SQLITE NORMALIZE] critical columns missing after normalize table=%s missing=%s",
                table_name,
                missing_critical,
            )

        out = out[keep].copy()

    logger.info(
        "[SQLITE NORMALIZE] summary df normalized table=%s interval=%s rows=%s cols=%s has_date=%s has_time=%s has_time_range=%s",
        table_name,
        interval,
        len(out),
        len(out.columns),
        "date" in out.columns,
        "time" in out.columns,
        "time_range" in out.columns,
    )

    return out