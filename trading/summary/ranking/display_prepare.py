# ============================================================
# File   : trading/summary/ranking/display_prepare.py
# Ver    : PRODUCTION-STABLE-RANKING-DISPLAY-PREPARE-V1.0
#          -RANKING-ONLY
# ------------------------------------------------------------
# ✔ RANKING表示前の dataframe 整形
# ✔ datetime / symbol 正規化
# ✔ latest_dt / symbols_count helper
# ✔ future row clamp
# ✔ PUSH系依存なし
# ============================================================

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

import pandas as pd

from .time_utils import resolve_display_slot


# ============================================================
# basic normalize
# ============================================================

def _safe_copy_df(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()

    if isinstance(value, tuple) and len(value) >= 1 and isinstance(value[0], pd.DataFrame):
        return value[0].copy()

    if isinstance(value, dict):
        for key in (
            "result_df",
            "merged_df",
            "df",
            "summary_df",
            "output_df",
            "display_df",
            "latest_df",
            "latest_summary_df",
        ):
            v = value.get(key)
            if isinstance(v, pd.DataFrame):
                return v.copy()

    try:
        return pd.DataFrame(value).copy()
    except Exception:
        return pd.DataFrame()


def _ensure_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    if "symbol" not in out.columns:
        for c in ("Symbol", "symbol_code", "Code", "code"):
            if c in out.columns:
                out["symbol"] = out[c]
                break

    if "symbol" not in out.columns:
        out["symbol"] = ""

    out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    out = out[out["symbol"] != ""].copy()
    return out


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    elif "end_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["end_time"], errors="coerce")
    elif "snapshot_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["snapshot_time"], errors="coerce")
    elif "CurrentPriceTime" in out.columns:
        out["datetime"] = pd.to_datetime(out["CurrentPriceTime"], errors="coerce")
    elif "current_price_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["current_price_time"], errors="coerce")
    elif "received_at" in out.columns:
        out["datetime"] = pd.to_datetime(out["received_at"], errors="coerce")
    else:
        out["datetime"] = pd.NaT

    try:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.tz_localize(None)
    except Exception:
        pass

    return out


def normalize_df(df: Any) -> pd.DataFrame:
    out = _safe_copy_df(df)
    if out.empty:
        return out

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in col if x not in ("", None)])
                for col in out.columns.to_flat_index()
            ]
    except Exception:
        pass

    try:
        out.columns = [str(c) for c in out.columns]
    except Exception:
        pass

    try:
        if out.columns.duplicated().any():
            out = out.loc[:, ~out.columns.duplicated(keep="last")].copy()
    except Exception:
        pass

    out = _ensure_symbol(out)
    out = _ensure_datetime(out)

    if "source" not in out.columns:
        out["source"] = "ranking"

    out = out.dropna(subset=["symbol"], how="any")
    if "datetime" in out.columns:
        out = out.dropna(subset=["datetime"], how="all")

    return out.reset_index(drop=True)


# ============================================================
# small helpers
# ============================================================

def symbols_count(df: pd.DataFrame) -> int:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return 0
    try:
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def latest_dt(df: pd.DataFrame):
    if not isinstance(df, pd.DataFrame) or df.empty or "datetime" not in df.columns:
        return None
    try:
        s = pd.to_datetime(df["datetime"], errors="coerce")
        if s.notna().any():
            return s.max()
    except Exception:
        pass
    return None


def latest_dt_str(df: pd.DataFrame) -> Optional[str]:
    x = latest_dt(df)
    if x is None or pd.isna(x):
        return None
    try:
        return str(pd.Timestamp(x).to_pydatetime().replace(microsecond=0))
    except Exception:
        return str(x)


# ============================================================
# future clamp
# ============================================================

def clamp_future_rows(df: pd.DataFrame, interval: int, now: Optional[dt.datetime] = None) -> pd.DataFrame:
    """
    display slot を超える future row を削除する。
    """
    out = normalize_df(df)
    if out.empty or "datetime" not in out.columns:
        return out

    try:
        _, slot_dt = resolve_display_slot(interval=interval, now=now)
        slot_ts = pd.Timestamp(slot_dt)

        out = out.loc[pd.to_datetime(out["datetime"], errors="coerce") <= slot_ts].copy()
        out = out.reset_index(drop=True)
        return out
    except Exception:
        return out