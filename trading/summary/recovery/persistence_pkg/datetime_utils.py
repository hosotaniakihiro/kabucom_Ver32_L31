# ============================================================
# File   : trading/summary/recovery/persistence_pkg/datetime_utils.py
# Ver    : PRODUCTION-STABLE-REV9.1-DATETIME-RECOVERY-KEY-GUARD
# ------------------------------------------------------------
# 【概要】
#   datetime / time / date safe parser
#
# 【主な機能】
#   - datetime / start_time / end_time / time / last_update の安全変換
#   - pandas UserWarning を抑えた日時parse
#   - HH:MM / HH:MM:SS の時刻だけ値を date と結合して復元
#   - datetime が無い/壊れている場合に date + time/end_time/start_time から復元
#   - UPSERT key である datetime を保存前に守る
#
# 【REV9.1 修正】
#   - normalize_datetime_like() を強化
#   - datetime列が無い場合でも作成
#   - datetimeが全NaTの場合:
#       1. date + time
#       2. date + end_time
#       3. date + start_time
#       4. date単体
#     の順で復元
#   - 一部NaTの場合も同様に部分補完
#   - datetime復元後、date/time を再生成
#   - start_time/end_time は原則 HH:MM:SS 文字列へ正規化
#
# 【重要】
#   - UTC変換しない
#   - JSTの壁時計時刻を維持する
#   - datetimeを失うとUPSERT側で rows dropped after column filter が発生する
# ============================================================

from __future__ import annotations

import logging
import warnings
from typing import Optional

import pandas as pd

from .constants import TIME_COLS

logger = logging.getLogger(__name__)


# ============================================================
# basic clean helpers
# ============================================================

def clean_time_like_series(s: pd.Series) -> pd.Series:
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
        try:
            return pd.Series(None, index=s.index, dtype="object")
        except Exception:
            return pd.Series(dtype="object")


def _as_clean_str_series(s: pd.Series) -> pd.Series:
    try:
        return (
            s.astype(str)
             .str.strip()
             .replace(
                 {
                     "": pd.NA,
                     "nan": pd.NA,
                     "NaN": pd.NA,
                     "None": pd.NA,
                     "NaT": pd.NA,
                     "<NA>": pd.NA,
                     "null": pd.NA,
                     "NULL": pd.NA,
                 }
             )
        )
    except Exception:
        try:
            return pd.Series(pd.NA, index=s.index, dtype="object")
        except Exception:
            return pd.Series(dtype="object")


def _strip_tz_wallclock_series(s: pd.Series) -> pd.Series:
    """
    timezone付き値をUTC変換せず、壁時計時刻を維持してtzだけ外す。
    """
    try:
        out = pd.to_datetime(s, errors="coerce")

        try:
            if getattr(out.dt, "tz", None) is not None:
                out = out.dt.tz_localize(None)
        except Exception:
            pass

        return out
    except Exception:
        try:
            return pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")


# ============================================================
# parser
# ============================================================

def get_base_date_series(df: pd.DataFrame, exclude_col: Optional[str] = None) -> pd.Series:
    """
    時刻だけの列をdatetime化するための基準日を返す。

    優先順位:
      1. date
      2. datetime
      3. end_time
      4. start_time
      5. last_update
      6. today
    """
    today = pd.Timestamp.now().strftime("%Y-%m-%d")

    candidates = ["date", "datetime", "end_time", "start_time", "last_update"]

    for col in candidates:
        if col == exclude_col:
            continue
        if col not in df.columns:
            continue

        try:
            raw = df[col]

            if pd.api.types.is_datetime64_any_dtype(raw):
                parsed = pd.to_datetime(raw, errors="coerce")
                try:
                    if getattr(parsed.dt, "tz", None) is not None:
                        parsed = parsed.dt.tz_localize(None)
                except Exception:
                    pass
            else:
                parsed = parse_datetime_series_safely(
                    raw,
                    base_df=df,
                    col_name=col,
                    allow_time_only=False,
                )

            if isinstance(parsed, pd.Series) and parsed.notna().any():
                out = parsed.dt.strftime("%Y-%m-%d")
                out = out.fillna(today).replace("NaT", today)
                return out

        except Exception:
            logger.debug(
                "[summary.recovery.persistence] base date candidate failed col=%s exclude=%s",
                col,
                exclude_col,
                exc_info=True,
            )

    return pd.Series(today, index=df.index, dtype="object")


def parse_datetime_series_safely(
    series: pd.Series,
    *,
    base_df: Optional[pd.DataFrame] = None,
    col_name: Optional[str] = None,
    allow_time_only: bool = True,
) -> pd.Series:
    """
    pandasの形式推定warningを抑えながらdatetime化する。

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

        s = clean_time_like_series(series)
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
                    "[summary.recovery.persistence] datetime pattern parse failed fmt=%s col=%s",
                    fmt,
                    col_name,
                    exc_info=True,
                )

        if allow_time_only:
            time_hms = s.str.match(r"^\d{1,2}:\d{2}:\d{2}$", na=False)
            time_hm = s.str.match(r"^\d{1,2}:\d{2}$", na=False)

            if time_hms.any() or time_hm.any():
                if base_df is not None:
                    base_date = get_base_date_series(base_df, exclude_col=col_name)
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
        logger.exception(
            "[summary.recovery.persistence] safe datetime parse failed col=%s",
            col_name,
        )
        try:
            return pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")


# ============================================================
# datetime recovery
# ============================================================

def recover_datetime_from_date_time(df: pd.DataFrame) -> pd.Series:
    """
    datetimeが無い/壊れている場合に date + time/end_time/start_time から復元する。

    優先順位:
      1. date + time
      2. date + end_time
      3. date + start_time
      4. date単体
    """
    idx = df.index

    try:
        if "date" not in df.columns:
            return pd.Series(pd.NaT, index=idx, dtype="datetime64[ns]")

        date_s = _as_clean_str_series(df["date"])

        time_candidates = ["time", "end_time", "start_time"]

        best = pd.Series(pd.NaT, index=idx, dtype="datetime64[ns]")

        for tcol in time_candidates:
            if tcol not in df.columns:
                continue

            try:
                time_s = _as_clean_str_series(df[tcol])

                # full datetime が入っている可能性も見る
                direct = parse_datetime_series_safely(
                    time_s,
                    base_df=df,
                    col_name=tcol,
                    allow_time_only=False,
                )

                combined = date_s.astype(str) + " " + time_s.astype(str)
                combined = combined.replace(
                    {
                        "<NA> <NA>": pd.NA,
                        "nan nan": pd.NA,
                        "None None": pd.NA,
                    }
                )

                parsed = parse_datetime_series_safely(
                    combined,
                    base_df=df,
                    col_name=f"date+{tcol}",
                    allow_time_only=False,
                )

                out = parsed.where(parsed.notna(), direct)

                mask = best.isna() & out.notna()
                if mask.any():
                    best.loc[mask] = out.loc[mask]

                if best.notna().all():
                    return best

            except Exception:
                logger.debug(
                    "[summary.recovery.persistence] recover datetime failed tcol=%s",
                    tcol,
                    exc_info=True,
                )

        if best.notna().any():
            return best

        # date単体
        date_only = parse_datetime_series_safely(
            date_s,
            base_df=df,
            col_name="date",
            allow_time_only=False,
        )
        return date_only

    except Exception:
        logger.exception("[summary.recovery.persistence] recover_datetime_from_date_time failed")
        return pd.Series(pd.NaT, index=idx, dtype="datetime64[ns]")


def normalize_datetime_like(df: pd.DataFrame) -> pd.DataFrame:
    """
    datetime / start_time / end_time / time / last_update を安全に正規化する。

    REV9.1:
      - datetime列が無い場合でも作成
      - datetimeが全NaTの場合、date + time/end_time/start_time から復元
      - 一部NaTの場合も復元
      - datetimeが無効な行はここでは極力残すが、復元ログを出す
        最終dropはdb_normalizer側のkey guardで行う
    """
    out = df.copy()

    try:
        # まずdatetime以外のTIME_COLSもparseするが、
        # time/start_time/end_timeはHH:MM:SS文字列のことがあるため、
        # datetime列の復元後に再度整形する。
        for c in TIME_COLS:
            if c in out.columns and c != "datetime":
                try:
                    # time/start_time/end_time が時刻だけでもdateと合わせてparse可能
                    out[c] = parse_datetime_series_safely(
                        out[c],
                        base_df=out,
                        col_name=c,
                        allow_time_only=True,
                    )
                except Exception:
                    logger.debug(
                        "[summary.recovery.persistence] non-datetime time col parse failed col=%s",
                        c,
                        exc_info=True,
                    )

        if "datetime" in out.columns:
            out["datetime"] = parse_datetime_series_safely(
                out["datetime"],
                base_df=out,
                col_name="datetime",
                allow_time_only=True,
            )
        else:
            out["datetime"] = pd.NaT

        # datetime全滅なら復元
        if out["datetime"].isna().all():
            recovered = recover_datetime_from_date_time(out)
            if recovered.notna().any():
                out["datetime"] = recovered
                logger.info(
                    "[summary.recovery.persistence] datetime recovered from date/time rows=%s recovered=%s",
                    len(out),
                    int(recovered.notna().sum()),
                )

        # 一部NaTも補完
        elif out["datetime"].isna().any():
            recovered = recover_datetime_from_date_time(out)
            mask = out["datetime"].isna() & recovered.notna()
            if mask.any():
                out.loc[mask, "datetime"] = recovered.loc[mask]
                logger.info(
                    "[summary.recovery.persistence] datetime partially recovered rows=%s recovered=%s",
                    len(out),
                    int(mask.sum()),
                )

        # timezoneを外し、分単位へ
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                if getattr(out["datetime"].dt, "tz", None) is not None:
                    out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass

            valid_mask = out["datetime"].notna()
            if valid_mask.any():
                out.loc[valid_mask, "datetime"] = out.loc[valid_mask, "datetime"].dt.floor("min")

        # datetimeがある行について date/time を再生成
        if "datetime" in out.columns and out["datetime"].notna().any():
            dt_s = pd.to_datetime(out["datetime"], errors="coerce")
            valid = dt_s.notna()

            if "date" not in out.columns:
                out["date"] = pd.NA
            out.loc[valid, "date"] = dt_s.loc[valid].dt.strftime("%Y-%m-%d")

            if "time" not in out.columns:
                out["time"] = pd.NA
            out.loc[valid, "time"] = dt_s.loc[valid].dt.strftime("%H:%M:%S")

        # start_time/end_time は最終的にはHH:MM:SS文字列へ寄せる
        if "datetime" in out.columns and out["datetime"].notna().any():
            dt_s = pd.to_datetime(out["datetime"], errors="coerce")
            valid = dt_s.notna()

            if "start_time" in out.columns:
                parsed_start = parse_datetime_series_safely(
                    out["start_time"],
                    base_df=out,
                    col_name="start_time",
                    allow_time_only=True,
                )
                if parsed_start.notna().any():
                    out["start_time"] = parsed_start.dt.strftime("%H:%M:%S")
                else:
                    out["start_time"] = pd.NA
                    out.loc[valid, "start_time"] = dt_s.loc[valid].dt.strftime("%H:%M:%S")
            else:
                out["start_time"] = pd.NA
                out.loc[valid, "start_time"] = dt_s.loc[valid].dt.strftime("%H:%M:%S")

            if "end_time" in out.columns:
                parsed_end = parse_datetime_series_safely(
                    out["end_time"],
                    base_df=out,
                    col_name="end_time",
                    allow_time_only=True,
                )
                if parsed_end.notna().any():
                    out["end_time"] = parsed_end.dt.strftime("%H:%M:%S")
                else:
                    out["end_time"] = pd.NA
                    out.loc[valid, "end_time"] = dt_s.loc[valid].dt.strftime("%H:%M:%S")
            else:
                out["end_time"] = pd.NA
                out.loc[valid, "end_time"] = dt_s.loc[valid].dt.strftime("%H:%M:%S")

        if "last_update" in out.columns:
            out["last_update"] = parse_datetime_series_safely(
                out["last_update"],
                base_df=out,
                col_name="last_update",
                allow_time_only=True,
            )
        else:
            out["last_update"] = pd.Timestamp.now()

        invalid = int(out["datetime"].isna().sum()) if "datetime" in out.columns else len(out)
        if invalid > 0:
            logger.warning(
                "[summary.recovery.persistence] datetime invalid rows remain after normalize invalid=%s total=%s",
                invalid,
                len(out),
            )

        return out

    except Exception:
        logger.exception("[summary.recovery.persistence] normalize_datetime_like failed")
        return df.copy()


def normalize_date_columns_for_db(df: pd.DataFrame) -> pd.DataFrame:
    """
    DB保存用 date/time 補完。

    datetimeが壊れている場合は復元を試みる。
    """
    out = df.copy()

    try:
        out = normalize_datetime_like(out)

        if "datetime" not in out.columns:
            return out

        dt_s = parse_datetime_series_safely(
            out["datetime"],
            base_df=out,
            col_name="datetime",
            allow_time_only=True,
        )
        out["datetime"] = dt_s

        valid = dt_s.notna()

        if "date" not in out.columns:
            out["date"] = pd.NA
        out.loc[valid, "date"] = dt_s.loc[valid].dt.strftime("%Y-%m-%d")

        if "time" not in out.columns:
            out["time"] = pd.NA
        out.loc[valid, "time"] = dt_s.loc[valid].dt.strftime("%H:%M:%S")

        if "end_time" not in out.columns:
            out["end_time"] = pd.NA
        out.loc[valid, "end_time"] = dt_s.loc[valid].dt.strftime("%H:%M:%S")

        if "start_time" not in out.columns:
            out["start_time"] = pd.NA
        # interval別の正確なstart_timeはcompute/resample側で作成済みなら保持される。
        # 無い場合のみdatetimeと同じ時刻で補完。
        st_empty = out["start_time"].isna() | out["start_time"].astype(str).isin(["", "NaT", "nan", "None", "<NA>"])
        out.loc[valid & st_empty, "start_time"] = dt_s.loc[valid & st_empty].dt.strftime("%H:%M:%S")

    except Exception:
        logger.exception("[summary.recovery.persistence] date columns normalize failed")

    return out


__all__ = [
    "clean_time_like_series",
    "get_base_date_series",
    "parse_datetime_series_safely",
    "recover_datetime_from_date_time",
    "normalize_datetime_like",
    "normalize_date_columns_for_db",
]