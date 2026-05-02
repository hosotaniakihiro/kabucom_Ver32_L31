# ============================================================
# File   : trading/summary/postprocess/normalize.py
# Version: PRODUCTION-STABLE-REV1.1-POSTPROCESS-NORMALIZE
#          -DATETIME-WARNING-FIX
#          -DATETIME-RECOVERY
#          -SLOPE-MTF-PRESERVE
# ------------------------------------------------------------
# 【概要】
#   summary postprocess 用 DataFrame 正規化
#
# 【主な機能】
#   ✔ DataFrame 基本安全化
#   ✔ duplicate columns coalesce
#   ✔ datetime / symbol normalize
#   ✔ date + time/end_time/start_time から datetime 復元
#   ✔ name / symbolname 保護
#   ✔ OHLC / volume 補完
#   ✔ indicator列の存在保証
#   ✔ score / slope / mtf 系列の保護
#
# 【REV1.1 修正】
#   ✔ pd.to_datetime(..., errors="coerce") の直接呼び出しによる
#     UserWarning: Could not infer format...
#     を抑制
#
#   ✔ datetime が無い/壊れている場合:
#       1. datetime
#       2. date + time
#       3. date + end_time
#       4. date + start_time
#       5. end_time 単体
#     の順で復元
#
#   ✔ slope / slope_atr_scaled / score_slope を相互補完
#   ✔ mtf / score_mtf / mtf_alignment 系を相互補完
#   ✔ indicator列は「存在保証」はするが、既存の非ゼロ値を潰さない
#
# 【重要】
#   - datetime は UPSERT key の一部なので、極力復元する
#   - slope/mtf は scoring に渡す前に 0 埋めしすぎない
#   - rsi/macd/signal は無い場合だけ NaN で作る
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import warnings
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# business day helpers
# ============================================================

def safe_previous_business_day(base_date: dt.date) -> dt.date:
    try:
        from utils.business_day_utils import get_previous_business_day
        return get_previous_business_day(base_date)
    except Exception:
        d = base_date - dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d


def is_today_business_day() -> bool:
    try:
        from utils.business_day_utils import is_today_business_day
        return bool(is_today_business_day())
    except Exception:
        return dt.date.today().weekday() < 5


def allowed_post_dates_fallback() -> set[dt.date]:
    today = dt.date.today()
    prev_bd = safe_previous_business_day(today)
    if is_today_business_day():
        return {prev_bd, today}
    return {prev_bd}


# ============================================================
# datetime safe parser
# ============================================================

def _clean_time_like_series(s: pd.Series) -> pd.Series:
    try:
        out = s.astype(str).str.strip()
        out = out.replace(
            {
                "": None,
                "nan": None,
                "NaN": None,
                "None": None,
                "NaT": None,
                "<NA>": None,
                "null": None,
                "NULL": None,
            }
        )
        return out
    except Exception:
        return pd.Series(None, index=getattr(s, "index", None), dtype="object")


def _safe_to_datetime_series(
    series: pd.Series,
    *,
    base_df: Optional[pd.DataFrame] = None,
    col_name: Optional[str] = None,
    allow_time_only: bool = True,
) -> pd.Series:
    """
    pandas の形式推定 warning を抑えながら datetime 化する。

    対応:
      - 2026-04-20 09:45:00
      - 2026-04-20 09:45
      - 2026/04/20 09:45:00
      - 2026/04/20 09:45
      - 2026-04-20
      - 2026/04/20
      - 09:45
      - 09:45:00
    """
    try:
        if series is None:
            return pd.Series(pd.NaT, dtype="datetime64[ns]")

        if not isinstance(series, pd.Series):
            series = pd.Series(series)

        if pd.api.types.is_datetime64_any_dtype(series):
            out = pd.to_datetime(series, errors="coerce")
            try:
                if getattr(out.dt, "tz", None) is not None:
                    out = out.dt.tz_localize(None)
            except Exception:
                pass
            return out

        s = _clean_time_like_series(series)
        result = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")

        patterns = [
            (
                s.str.match(r"^\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}:\d{2}$", na=False),
                "%Y-%m-%d %H:%M:%S",
            ),
            (
                s.str.match(r"^\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}$", na=False),
                "%Y-%m-%d %H:%M",
            ),
            (
                s.str.match(r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2}$", na=False),
                "%Y/%m/%d %H:%M:%S",
            ),
            (
                s.str.match(r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}$", na=False),
                "%Y/%m/%d %H:%M",
            ),
            (
                s.str.match(r"^\d{4}-\d{1,2}-\d{1,2}$", na=False),
                "%Y-%m-%d",
            ),
            (
                s.str.match(r"^\d{4}/\d{1,2}/\d{1,2}$", na=False),
                "%Y/%m/%d",
            ),
        ]

        for mask, fmt in patterns:
            try:
                if mask.any():
                    idx = mask[mask].index
                    result.loc[idx] = pd.to_datetime(
                        s.loc[idx],
                        errors="coerce",
                        format=fmt,
                    )
            except Exception:
                logger.debug(
                    "[POST.NORMALIZE] datetime pattern parse failed col=%s fmt=%s",
                    col_name,
                    fmt,
                    exc_info=True,
                )

        if allow_time_only:
            time_hms = s.str.match(r"^\d{1,2}:\d{2}:\d{2}$", na=False)
            time_hm = s.str.match(r"^\d{1,2}:\d{2}$", na=False)

            if time_hms.any() or time_hm.any():
                if base_df is not None:
                    base_date = _get_base_date_series(base_df, exclude_col=col_name)
                else:
                    today = pd.Timestamp.now().strftime("%Y-%m-%d")
                    base_date = pd.Series(today, index=series.index, dtype="object")

                today = pd.Timestamp.now().strftime("%Y-%m-%d")
                try:
                    base_date = base_date.fillna(today).replace("NaT", today)
                except Exception:
                    base_date = pd.Series(today, index=series.index, dtype="object")

                combined = base_date.astype(str) + " " + s.astype(str)

                if time_hms.any():
                    idx = time_hms[time_hms].index
                    result.loc[idx] = pd.to_datetime(
                        combined.loc[idx],
                        errors="coerce",
                        format="%Y-%m-%d %H:%M:%S",
                    )

                if time_hm.any():
                    idx = time_hm[time_hm].index
                    result.loc[idx] = pd.to_datetime(
                        combined.loc[idx],
                        errors="coerce",
                        format="%Y-%m-%d %H:%M",
                    )

        remaining = result.isna() & s.notna()
        if remaining.any():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                result.loc[remaining] = pd.to_datetime(
                    s.loc[remaining],
                    errors="coerce",
                )

        try:
            if getattr(result.dt, "tz", None) is not None:
                result = result.dt.tz_localize(None)
        except Exception:
            pass

        return result

    except Exception:
        logger.exception("[POST.NORMALIZE] safe datetime parse failed col=%s", col_name)
        try:
            return pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")


def _get_base_date_series(df: pd.DataFrame, exclude_col: Optional[str] = None) -> pd.Series:
    today = pd.Timestamp.now().strftime("%Y-%m-%d")

    for col in ["date", "datetime", "end_time", "start_time", "last_update"]:
        if col == exclude_col or col not in df.columns:
            continue

        try:
            parsed = _safe_to_datetime_series(
                df[col],
                base_df=df,
                col_name=col,
                allow_time_only=False,
            )

            if parsed.notna().any():
                out = parsed.dt.strftime("%Y-%m-%d")
                out = out.fillna(today).replace("NaT", today)
                return out
        except Exception:
            logger.debug("[POST.NORMALIZE] base date candidate failed col=%s", col, exc_info=True)

    return pd.Series(today, index=df.index, dtype="object")


def _recover_datetime_from_date_time(df: pd.DataFrame) -> pd.Series:
    """
    datetime が無い/壊れている場合に date + time/end_time/start_time から復元する。
    """
    idx = df.index

    try:
        if "date" not in df.columns:
            return pd.Series(pd.NaT, index=idx, dtype="datetime64[ns]")

        date_s = _clean_time_like_series(df["date"])
        best = pd.Series(pd.NaT, index=idx, dtype="datetime64[ns]")

        for tcol in ["time", "end_time", "start_time"]:
            if tcol not in df.columns:
                continue

            try:
                time_s = _clean_time_like_series(df[tcol])

                direct = _safe_to_datetime_series(
                    time_s,
                    base_df=df,
                    col_name=tcol,
                    allow_time_only=False,
                )

                combined = date_s.astype(str) + " " + time_s.astype(str)
                combined = combined.replace(
                    {
                        "None None": None,
                        "nan nan": None,
                        "<NA> <NA>": None,
                    }
                )

                parsed = _safe_to_datetime_series(
                    combined,
                    base_df=df,
                    col_name=f"date+{tcol}",
                    allow_time_only=False,
                )

                recovered = parsed.where(parsed.notna(), direct)
                mask = best.isna() & recovered.notna()
                if mask.any():
                    best.loc[mask] = recovered.loc[mask]

                if best.notna().all():
                    return best

            except Exception:
                logger.debug("[POST.NORMALIZE] datetime recovery failed tcol=%s", tcol, exc_info=True)

        if best.notna().any():
            return best

        return _safe_to_datetime_series(
            date_s,
            base_df=df,
            col_name="date",
            allow_time_only=False,
        )

    except Exception:
        logger.exception("[POST.NORMALIZE] recover datetime failed")
        return pd.Series(pd.NaT, index=idx, dtype="datetime64[ns]")


def normalize_datetime_column(df: pd.DataFrame, *, drop_invalid: bool = True) -> pd.DataFrame:
    """
    datetime を安全に正規化する。
    """
    out = ensure_dataframe(df)
    if out.empty:
        return out

    try:
        if "datetime" in out.columns:
            out["datetime"] = _safe_to_datetime_series(
                out["datetime"],
                base_df=out,
                col_name="datetime",
                allow_time_only=True,
            )
        elif "end_time" in out.columns:
            out["datetime"] = _safe_to_datetime_series(
                out["end_time"],
                base_df=out,
                col_name="end_time",
                allow_time_only=True,
            )
        else:
            out["datetime"] = pd.NaT

        if out["datetime"].isna().all():
            recovered = _recover_datetime_from_date_time(out)
            if recovered.notna().any():
                out["datetime"] = recovered
                logger.info(
                    "[POST.NORMALIZE] datetime recovered rows=%s recovered=%s",
                    len(out),
                    int(recovered.notna().sum()),
                )
        elif out["datetime"].isna().any():
            recovered = _recover_datetime_from_date_time(out)
            mask = out["datetime"].isna() & recovered.notna()
            if mask.any():
                out.loc[mask, "datetime"] = recovered.loc[mask]
                logger.info(
                    "[POST.NORMALIZE] datetime partially recovered rows=%s recovered=%s",
                    len(out),
                    int(mask.sum()),
                )

        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            if getattr(out["datetime"].dt, "tz", None) is not None:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        valid = out["datetime"].notna()

        if drop_invalid:
            before = len(out)
            out = out[valid].copy()
            dropped = before - len(out)
            if dropped > 0:
                logger.warning(
                    "[POST.NORMALIZE] invalid datetime rows dropped dropped=%s remain=%s",
                    dropped,
                    len(out),
                )
            if out.empty:
                return out

        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.floor("min")

        out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
        out["time"] = out["datetime"].dt.strftime("%H:%M:%S")

        return out

    except Exception:
        logger.exception("[POST.NORMALIZE] datetime normalize failed; fallback now()")
        out["datetime"] = pd.Timestamp.now().floor("min")
        out["date"] = out["datetime"].dt.strftime("%Y-%m-%d") if isinstance(out["datetime"], pd.Series) else pd.Timestamp.now().strftime("%Y-%m-%d")
        out["time"] = out["datetime"].dt.strftime("%H:%M:%S") if isinstance(out["datetime"], pd.Series) else pd.Timestamp.now().strftime("%H:%M:%S")
        return out


def extract_actual_dates_from_df(df: pd.DataFrame) -> set[dt.date]:
    try:
        if df is None or df.empty:
            return set()

        if "datetime" in df.columns:
            dt_s = _safe_to_datetime_series(
                df["datetime"],
                base_df=df,
                col_name="datetime",
                allow_time_only=True,
            )
            vals = {x.date() for x in dt_s.dropna()}
            if vals:
                return vals

        if "date" in df.columns:
            date_s = _safe_to_datetime_series(
                df["date"],
                base_df=df,
                col_name="date",
                allow_time_only=False,
            )
            vals = {x.date() for x in date_s.dropna()}
            if vals:
                return vals

        if "end_time" in df.columns:
            dt_s = _safe_to_datetime_series(
                df["end_time"],
                base_df=df,
                col_name="end_time",
                allow_time_only=True,
            )
            vals = {x.date() for x in dt_s.dropna()}
            if vals:
                return vals

    except Exception:
        logger.exception("[POST.NORMALIZE] extract actual dates failed")

    return set()


# ============================================================
# dataframe / duplicate helpers
# ============================================================

def ensure_dataframe(df) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()

    if isinstance(df, pd.DataFrame):
        out = df.copy()
    elif isinstance(df, pd.Series):
        out = pd.DataFrame([df.to_dict()])
    elif isinstance(df, dict):
        out = pd.DataFrame([df])
    else:
        try:
            out = pd.DataFrame(df).copy()
        except Exception:
            logger.exception("[POST.NORMALIZE] dataframe conversion failed")
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in col if str(x) != ""])
                for col in out.columns.to_flat_index()
            ]
    except Exception:
        pass

    try:
        out = out.reset_index(drop=True)
    except Exception:
        pass

    return out


def coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    try:
        cols = list(df.columns)
        if len(cols) == len(set(cols)):
            return df
    except Exception:
        return df

    df = df.copy()

    try:
        unique_cols = []
        seen = set()
        for c in df.columns:
            if c not in seen:
                unique_cols.append(c)
                seen.add(c)

        out = {}
        for c in unique_cols:
            idxs = [i for i, name in enumerate(df.columns) if name == c]
            if len(idxs) == 1:
                out[c] = df.iloc[:, idxs[0]]
                continue

            logger.warning("[POST.NORMALIZE] duplicate label coalesced -> %s count=%s", c, len(idxs))

            s = df.iloc[:, idxs[0]]
            for j in idxs[1:]:
                try:
                    s = s.combine_first(df.iloc[:, j])
                except Exception:
                    try:
                        s = s.where(s.notna(), df.iloc[:, j])
                    except Exception:
                        pass
            out[c] = s

        return pd.DataFrame(out).reset_index(drop=True)

    except Exception:
        logger.exception("[POST.NORMALIZE] duplicate column coalesce failed")
        try:
            return df.loc[:, ~df.columns.duplicated()].copy()
        except Exception:
            return df


# ============================================================
# series helpers
# ============================================================

def safe_series(s, index=None, default=0.0, fillna: bool = True):
    if isinstance(s, pd.Series):
        try:
            s = pd.to_numeric(s, errors="coerce")
            s = s.replace([np.inf, -np.inf], np.nan)
            if fillna:
                s = s.fillna(default)
            s = s.clip(-1e12, 1e12)
            if index is not None and len(s) != len(index):
                s = s.reindex(index)
                if fillna:
                    s = s.fillna(default)
            return s
        except Exception:
            pass

    if index is None:
        return pd.Series(dtype="float64")

    try:
        if fillna:
            return pd.Series(default, index=index, dtype="float64")
        return pd.Series(np.nan, index=index, dtype="float64")
    except Exception:
        return pd.Series(dtype="float64")


def pick_series(df: pd.DataFrame, candidates, default=0.0, fillna: bool = True) -> pd.Series:
    idx = df.index
    for c in candidates:
        if c in df.columns:
            return safe_series(df[c], idx, default=default, fillna=fillna)
    return safe_series(None, idx, default=default, fillna=fillna)


def pick_text_series(df: pd.DataFrame, candidates, default="") -> pd.Series:
    idx = df.index
    for c in candidates:
        if c in df.columns:
            try:
                s = df[c].astype(str).fillna(default)
                s = s.replace({"nan": default, "None": default, "<NA>": default})
                return s.reindex(idx, fill_value=default)
            except Exception:
                pass
    return pd.Series(default, index=idx, dtype="object")


def normalize_name_series(s: pd.Series, index) -> pd.Series:
    try:
        if not isinstance(s, pd.Series):
            return pd.Series("", index=index, dtype="object")

        out = s.astype(str).str.strip()
        out = out.replace(
            {
                "0.0": "",
                "0": "",
                "nan": "",
                "None": "",
                "none": "",
                "<NA>": "",
            }
        )
        return out.fillna("")
    except Exception:
        return pd.Series("", index=index, dtype="object")


# ============================================================
# name / symbol
# ============================================================

def protect_name_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()
    idx = df.index

    if "symbolname" in df.columns:
        df["symbolname"] = normalize_name_series(df["symbolname"], idx)

    if "name" in df.columns:
        df["name"] = normalize_name_series(df["name"], idx)

    if "symbolname" not in df.columns and "name" in df.columns:
        df["symbolname"] = normalize_name_series(df["name"], idx)

    if "name" not in df.columns and "symbolname" in df.columns:
        df["name"] = normalize_name_series(df["symbolname"], idx)

    if "symbolname" not in df.columns:
        df["symbolname"] = pick_text_series(df, ["symbol"], default="")

    if "name" not in df.columns:
        df["name"] = pick_text_series(df, ["symbolname", "symbol"], default="")

    try:
        miss = (
            df["symbolname"].astype(str).str.strip().eq("")
            | df["symbolname"].astype(str).eq(df["symbol"].astype(str))
        )
        if miss.any() and "name" in df.columns:
            candidate = normalize_name_series(df["name"], idx)
            fill_mask = miss & candidate.astype(str).str.strip().ne("")
            df.loc[fill_mask, "symbolname"] = candidate.loc[fill_mask]
    except Exception:
        logger.debug("[POST.NORMALIZE] symbolname fallback fill failed", exc_info=True)

    try:
        miss = (
            df["name"].astype(str).str.strip().eq("")
            | df["name"].astype(str).eq(df["symbol"].astype(str))
        )
        if miss.any() and "symbolname" in df.columns:
            candidate = normalize_name_series(df["symbolname"], idx)
            fill_mask = miss & candidate.astype(str).str.strip().ne("")
            df.loc[fill_mask, "name"] = candidate.loc[fill_mask]
    except Exception:
        logger.debug("[POST.NORMALIZE] name fallback fill failed", exc_info=True)

    return df


def normalize_basic(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = ensure_dataframe(df)
    if df.empty:
        return df

    df = coalesce_duplicate_columns(df)

    try:
        df.replace([np.inf, -np.inf], np.nan, inplace=True)
    except Exception:
        pass

    df = normalize_datetime_column(df, drop_invalid=True)
    if df.empty:
        return df

    if "symbol" not in df.columns:
        logger.warning("[POST.NORMALIZE] symbol column missing")
        return pd.DataFrame()

    try:
        df["symbol"] = (
            df["symbol"]
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
        )
        df = df[df["symbol"] != ""]
    except Exception:
        logger.warning("[POST.NORMALIZE] symbol normalize failed")
        return pd.DataFrame()

    df = protect_name_columns(df)

    try:
        df = df.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)
    except Exception:
        pass

    return df


# ============================================================
# numeric coalesce / price
# ============================================================

def coalesce_numeric_column(df: pd.DataFrame, dest: str, candidates: list[str], default=np.nan) -> pd.DataFrame:
    idx = df.index
    s = safe_series(df[dest], idx, default=default, fillna=False) if dest in df.columns else safe_series(None, idx, default=default, fillna=False)

    for c in candidates:
        if c in df.columns:
            try:
                cur = safe_series(df[c], idx, default=default, fillna=False)
                s = s.where(s.notna(), cur)
            except Exception:
                pass

    df[dest] = s
    return df


def ensure_price_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    df = coalesce_numeric_column(
        df,
        "close",
        ["close_price", "last_price", "current_price", "price", "value", "settlement_price", "ask_price", "bid_price"],
        default=np.nan,
    )
    df = coalesce_numeric_column(
        df,
        "open",
        ["open_price", "opening_price", "opening", "price", "current_price", "close"],
        default=np.nan,
    )
    df = coalesce_numeric_column(
        df,
        "high",
        ["high_price", "high_value", "close", "price", "current_price"],
        default=np.nan,
    )
    df = coalesce_numeric_column(
        df,
        "low",
        ["low_price", "low_value", "close", "price", "current_price"],
        default=np.nan,
    )
    df = coalesce_numeric_column(
        df,
        "volume",
        ["trading_volume", "qty", "total_volume"],
        default=np.nan,
    )

    close_s = safe_series(df["close"], df.index, default=np.nan, fillna=False)

    for c in ("open", "high", "low"):
        try:
            s = safe_series(df[c], df.index, default=np.nan, fillna=False)
            df[c] = s.where(s.notna(), close_s)
        except Exception:
            df[c] = close_s

    alias_map = {
        "close_price": "close",
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "last_price": "close",
        "current_price": "close",
        "price": "close",
    }

    for alias, src in alias_map.items():
        try:
            if alias not in df.columns:
                df[alias] = safe_series(df[src], df.index, default=np.nan, fillna=False)
            else:
                alias_s = safe_series(df[alias], df.index, default=np.nan, fillna=False)
                src_s = safe_series(df[src], df.index, default=np.nan, fillna=False)
                df[alias] = alias_s.where(alias_s.notna(), src_s)
        except Exception:
            logger.debug("[POST.NORMALIZE] alias fill failed alias=%s src=%s", alias, src, exc_info=True)

    if "trading_volume" not in df.columns and "volume" in df.columns:
        df["trading_volume"] = safe_series(df["volume"], df.index, default=0.0)
    elif "trading_volume" in df.columns:
        df["trading_volume"] = pick_series(df, ["trading_volume", "volume"], default=0.0)

    return df


# ============================================================
# indicator / score preservation
# ============================================================

def coalesce_preserve_nonzero(
    df: pd.DataFrame,
    dest: str,
    candidates: Iterable[str],
    *,
    default=np.nan,
) -> pd.DataFrame:
    """
    dest の NaN または 0 を、候補列の非NaN/非0で補完する。
    既存の非0値は潰さない。
    """
    if df is None or df.empty:
        return df

    out = df.copy()
    idx = out.index

    if dest in out.columns:
        base = safe_series(out[dest], idx, default=default, fillna=False)
    else:
        base = safe_series(None, idx, default=default, fillna=False)

    for c in candidates:
        if c not in out.columns:
            continue

        cur = safe_series(out[c], idx, default=default, fillna=False)

        try:
            fill_mask = base.isna() | base.fillna(0).eq(0)
            candidate_mask = cur.notna() & cur.fillna(0).ne(0)
            base = base.where(~(fill_mask & candidate_mask), cur)
        except Exception:
            base = base.where(base.notna(), cur)

    out[dest] = base
    return out


def preserve_slope_mtf_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    slope / mtf 系列を相互補完する。
    0埋めで情報を潰さないため、非0値を優先して拾う。
    """
    if df is None or df.empty:
        return df

    out = df.copy()

    # slope系
    out = coalesce_preserve_nonzero(
        out,
        "slope",
        [
            "slope",
            "score_slope",
            "slope_atr_scaled",
            "slope_atr_scaled_1m",
            "slope_atr_scaled_3m",
            "slope_atr_scaled_5m",
            "ma75_slope",
            "vwap_slope",
            "volume_slope",
        ],
        default=np.nan,
    )

    out = coalesce_preserve_nonzero(
        out,
        "slope_atr_scaled",
        [
            "slope_atr_scaled",
            "slope",
            "score_slope",
            "slope_atr_scaled_1m",
            "slope_atr_scaled_3m",
            "slope_atr_scaled_5m",
        ],
        default=np.nan,
    )

    out = coalesce_preserve_nonzero(
        out,
        "score_slope",
        [
            "score_slope",
            "slope",
            "slope_atr_scaled",
            "slope_atr_scaled_1m",
            "slope_atr_scaled_3m",
            "slope_atr_scaled_5m",
        ],
        default=np.nan,
    )

    # mtf系
    out = coalesce_preserve_nonzero(
        out,
        "mtf",
        [
            "mtf",
            "score_mtf",
            "mtf_alignment",
            "mtf_alignment_bonus",
            "mtf_bonus",
            "mtf_raw",
        ],
        default=np.nan,
    )

    out = coalesce_preserve_nonzero(
        out,
        "score_mtf",
        [
            "score_mtf",
            "mtf",
            "mtf_alignment",
            "mtf_alignment_bonus",
            "mtf_bonus",
            "mtf_raw",
        ],
        default=np.nan,
    )

    return out


def ensure_indicator_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    # テクニカル主要列は無ければNaNで作る。
    # 既存値は絶対に潰さない。
    for col in (
        "rsi",
        "macd",
        "signal",
        "hist",
        "ma5",
        "ma25",
        "ma75",
        "ema12",
        "ema26",
        "atr",
        "bb_mid",
        "bb_upper",
        "bb_lower",
        "bb_width",
    ):
        if col not in df.columns:
            df[col] = np.nan

    df = preserve_slope_mtf_columns(df)

    for col in ("slope", "slope_atr_scaled", "score_slope", "mtf", "score_mtf"):
        if col not in df.columns:
            df[col] = np.nan

    return df


def ensure_score_like_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    score系の存在保証。
    既存の非0値は潰さない。
    """
    if df is None or df.empty:
        return df

    out = df.copy()

    out = coalesce_preserve_nonzero(
        out,
        "score",
        ["score", "score_total", "display_score", "final_score", "combined_score"],
        default=np.nan,
    )
    out = coalesce_preserve_nonzero(
        out,
        "score_total",
        ["score_total", "score", "display_score", "final_score", "combined_score"],
        default=np.nan,
    )
    out = coalesce_preserve_nonzero(
        out,
        "final_score",
        ["final_score", "display_score", "score_total", "score"],
        default=np.nan,
    )
    out = coalesce_preserve_nonzero(
        out,
        "display_score",
        ["display_score", "final_score", "score_total", "score"],
        default=np.nan,
    )
    out = coalesce_preserve_nonzero(
        out,
        "score_buy",
        ["score_buy", "buy_score", "buy"],
        default=np.nan,
    )
    out = coalesce_preserve_nonzero(
        out,
        "score_sell",
        ["score_sell", "sell_score", "sell"],
        default=np.nan,
    )

    if "buy_score" not in out.columns and "score_buy" in out.columns:
        out["buy_score"] = out["score_buy"]

    if "sell_score" not in out.columns and "score_sell" in out.columns:
        out["sell_score"] = out["score_sell"]

    return out


# ============================================================
# public main helper
# ============================================================

def normalize_postprocess_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    postprocess全体で使いやすい統合正規化。
    既存コードが個別関数を呼ぶ場合も壊さないよう、公開関数として追加。
    """
    out = ensure_dataframe(df)
    if out.empty:
        return out

    out = coalesce_duplicate_columns(out)
    out = normalize_basic(out)
    if out.empty:
        return out

    out = ensure_price_columns(out)
    out = ensure_indicator_columns(out)
    out = ensure_score_like_columns(out)
    out = protect_name_columns(out)

    return out


__all__ = [
    "safe_previous_business_day",
    "is_today_business_day",
    "allowed_post_dates_fallback",
    "extract_actual_dates_from_df",
    "ensure_dataframe",
    "coalesce_duplicate_columns",
    "safe_series",
    "pick_series",
    "pick_text_series",
    "normalize_name_series",
    "protect_name_columns",
    "normalize_basic",
    "coalesce_numeric_column",
    "ensure_price_columns",
    "coalesce_preserve_nonzero",
    "preserve_slope_mtf_columns",
    "ensure_indicator_columns",
    "ensure_score_like_columns",
    "normalize_datetime_column",
    "normalize_postprocess_df",
]