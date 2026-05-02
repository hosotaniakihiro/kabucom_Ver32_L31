# ============================================================
# File   : trading/scoring/core/preprocess/datetime_sanitizer.py
# Version: Ver1.1-PRODUCTION-DATETIME-SANITIZER-DUPLICATE-SAFE
# ------------------------------------------------------------
# ✔ datetime/date/start_time fallback
# ✔ duplicate datetime label safe coalesce
# ✔ stable sort by symbol+datetime
# ✔ NaT/drop safe
# ✔ legacy compatibility keep
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


def _build_datetime_from_date_and_start_time(df: pd.DataFrame) -> pd.Series:
    if "date" not in df.columns or "start_time" not in df.columns:
        return pd.Series([pd.NaT] * len(df), index=df.index)

    date_s = df["date"].astype(str).str.strip()
    time_s = df["start_time"].astype(str).str.strip()
    return pd.to_datetime(date_s + " " + time_s, errors="coerce")


def _extract_datetime_series(df: pd.DataFrame) -> pd.Series:
    if "datetime" not in df.columns:
        return pd.Series([pd.NaT] * len(df), index=df.index)

    obj = df["datetime"]

    if isinstance(obj, pd.DataFrame):
        if obj.empty:
            return pd.Series([pd.NaT] * len(df), index=df.index)

        cols = [pd.to_datetime(obj.iloc[:, i], errors="coerce") for i in range(obj.shape[1])]
        base = cols[0]
        for cur in cols[1:]:
            base = base.where(base.notna(), cur)

        logger.warning(
            "[SCORING PIPELINE] duplicate datetime label coalesced -> count=%d",
            obj.shape[1],
        )
        return base

    return pd.to_datetime(obj, errors="coerce")


def _drop_duplicate_named_columns(df: pd.DataFrame, target: str) -> pd.DataFrame:
    idxs = [i for i, c in enumerate(df.columns) if c == target]
    if len(idxs) <= 1:
        return df

    keep_positions = [i for i in range(df.shape[1]) if i not in idxs[1:]]
    return df.iloc[:, keep_positions].copy()


def sanitize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    # 1) duplicate datetime を Series に畳む
    dt_series = _extract_datetime_series(out)

    # 2) duplicate datetime label を物理的に1本へする
    if "datetime" in out.columns:
        out = _drop_duplicate_named_columns(out, "datetime")
        out["datetime"] = dt_series
    else:
        out["datetime"] = dt_series

    # 3) fallback
    need_fill = out["datetime"].isna()
    if bool(need_fill.any()):
        dt_from_parts = _build_datetime_from_date_and_start_time(out)
        out.loc[need_fill, "datetime"] = dt_from_parts.loc[need_fill]

    # 4) symbol normalize
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.strip()

    # 5) drop invalid
    before = len(out)
    out = out[out["datetime"].notna()].copy()
    dropped = before - len(out)
    if dropped > 0:
        logger.warning("[SCORING PIPELINE] dropped NaT datetime rows=%d", dropped)

    # 6) stable sort
    sort_keys = [c for c in ("symbol", "datetime") if c in out.columns]
    if sort_keys:
        out = out.sort_values(sort_keys, kind="mergesort").reset_index(drop=True)

    if not out.empty:
        logger.info(
            "[SCORING PIPELINE] datetime sanitized rows=%d range=[%s .. %s]",
            len(out),
            out["datetime"].min(),
            out["datetime"].max(),
        )

    return out