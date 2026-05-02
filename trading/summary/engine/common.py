# ============================================================
# File   : trading/summary/engine/common.py
# Ver    : COMPAT-COMMON-SHIM-V1.2-INTERVAL-LABEL-EMPTY-RESULT-COMPAT
# ------------------------------------------------------------
# 【概要】
#   summary engine 共通 helper / 互換 shim
#
# 【主な機能】
#   - DataFrame / Series / dict の安全変換
#   - symbol 正規化
#   - datetime 正規化
#   - latest timestamp / symbol count / profile log
#   - 旧 incremental_engine / summary_pipeline 互換 API
#
# 【今回の修正】
#   - incremental_engine / summary_pipeline から import される
#       interval_label
#     を追加
#   - empty_result / empty_df / make_empty_result / empty_summary_result 維持
#   - ImportError:
#       cannot import name 'interval_label'
#     を解消する
#
# 【重要】
#   このファイルは薄い互換レイヤー。
#   重い計算ロジックは持たない。
# ============================================================

from __future__ import annotations

import logging
import warnings
from typing import Any, Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# interval helpers
# ============================================================

def interval_label(interval: Any) -> str:
    """
    旧コード互換:
      interval_label(1)     -> "1min"
      interval_label("5")   -> "5min"
      interval_label("5min")-> "5min"
      interval_label(None)  -> ""
    """
    try:
        if interval is None:
            return ""

        s = str(interval).strip()
        if not s:
            return ""

        if s.lower().endswith("min"):
            return s

        return f"{int(float(s))}min"
    except Exception:
        try:
            return str(interval).strip()
        except Exception:
            return ""


def normalize_interval_value(interval: Any, default: int = 1) -> int:
    """
    互換用:
      "1min" / "1" / 1 / 1.0 -> 1
    """
    try:
        if interval is None:
            return int(default)

        s = str(interval).strip().lower().replace("minutes", "").replace("minute", "").replace("mins", "").replace("min", "")
        s = s.strip()
        if not s:
            return int(default)

        return int(float(s))
    except Exception:
        return int(default)


# ============================================================
# dataframe helpers
# ============================================================

def ensure_dataframe(obj: Any) -> pd.DataFrame:
    """
    DataFrame / Series / dict / list-like を安全に DataFrame 化する。
    """
    if obj is None:
        return pd.DataFrame()

    if isinstance(obj, pd.DataFrame):
        try:
            return obj.copy().reset_index(drop=True)
        except Exception:
            return obj.copy()

    if isinstance(obj, pd.Series):
        try:
            return pd.DataFrame([obj.to_dict()]).reset_index(drop=True)
        except Exception:
            return pd.DataFrame()

    if isinstance(obj, dict):
        try:
            return pd.DataFrame([obj]).reset_index(drop=True)
        except Exception:
            return pd.DataFrame()

    try:
        df = pd.DataFrame(obj)
        try:
            return df.reset_index(drop=True)
        except Exception:
            return df
    except Exception:
        return pd.DataFrame()


def empty_df(columns: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """
    空 DataFrame を返す互換 helper。
    """
    try:
        if columns is None:
            return pd.DataFrame()
        return pd.DataFrame(columns=list(columns))
    except Exception:
        return pd.DataFrame()


def empty_result(
    interval: int | str | None = None,
    reason: str = "",
    *,
    columns: Optional[Iterable[str]] = None,
    as_tuple: bool = False,
    **kwargs: Any,
):
    """
    旧 incremental_engine / summary_pipeline 互換の空結果。

    呼び出し側が DataFrame 単体を期待する場合:
        return empty_result(...)

    呼び出し側が (df, meta) を期待する場合:
        return empty_result(..., as_tuple=True)
    """
    df = empty_df(columns)

    meta = {
        "ok": False,
        "empty": True,
        "rows": 0,
        "interval": interval,
        "interval_label": interval_label(interval),
        "reason": reason or "empty_result",
    }
    meta.update(kwargs)

    if as_tuple:
        return df, meta
    return df


def make_empty_result(
    interval: int | str | None = None,
    reason: str = "",
    **kwargs: Any,
):
    """
    empty_result の別名互換。
    """
    return empty_result(interval=interval, reason=reason, **kwargs)


def empty_summary_result(
    interval: int | str | None = None,
    reason: str = "",
    **kwargs: Any,
):
    """
    empty_result の別名互換。
    """
    return empty_result(interval=interval, reason=reason, **kwargs)


def safe_get_series(df: Any, col: str) -> pd.Series:
    """
    DataFrame から列 Series を安全取得。
    無い場合は空 Series を返す。
    """
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.Series(dtype="object")
        if col not in df.columns:
            return pd.Series(dtype="object")

        value = df[col]

        if isinstance(value, pd.DataFrame):
            if value.shape[1] <= 0:
                return pd.Series(dtype="object")

            out = value.iloc[:, 0]
            for i in range(1, value.shape[1]):
                try:
                    out = out.combine_first(value.iloc[:, i])
                except Exception:
                    try:
                        out = out.where(out.notna(), value.iloc[:, i])
                    except Exception:
                        pass
            return out

        if isinstance(value, pd.Series):
            return value

        return pd.Series(value, index=df.index)

    except Exception:
        return pd.Series(dtype="object")


def normalize_symbol_column(df: Any, symbol_col: str = "symbol") -> pd.DataFrame:
    """
    symbol 列を文字列化して空白除去。
    末尾 .0 も除去。
    """
    out = ensure_dataframe(df)
    if out.empty:
        return out

    if symbol_col not in out.columns:
        for c in (
            "Symbol",
            "SYMBOL",
            "symbol_code",
            "Code",
            "code",
            "ticker",
            "Ticker",
            "銘柄コード",
        ):
            if c in out.columns:
                out[symbol_col] = out[c]
                break

    if symbol_col not in out.columns:
        return out

    try:
        out[symbol_col] = (
            out[symbol_col]
            .astype(str)
            .str.strip()
            .str.replace(r"\.0$", "", regex=True)
        )
        out[symbol_col] = out[symbol_col].replace(
            {
                "": pd.NA,
                "nan": pd.NA,
                "NaN": pd.NA,
                "None": pd.NA,
                "NaT": pd.NA,
                "<NA>": pd.NA,
            }
        )
        out = out[out[symbol_col].notna()].copy()
    except Exception:
        pass

    return out.reset_index(drop=True)


# ============================================================
# datetime helpers
# ============================================================

def _clean_datetime_like_series(s: pd.Series) -> pd.Series:
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


def _safe_parse_datetime_series(
    series: Any,
    *,
    base_date: Any = None,
    allow_time_only: bool = True,
) -> pd.Series:
    """
    warning を出さずに datetime 化する。

    対応:
      - 2026-04-20 09:52:00
      - 2026-04-20 09:52
      - 2026/04/20 09:52:00
      - 2026/04/20 09:52
      - 2026-04-20
      - 2026/04/20
      - 09:52:00
      - 09:52
    """
    try:
        if series is None:
            return pd.Series(dtype="datetime64[ns]")

        if not isinstance(series, pd.Series):
            series = pd.Series(series)

        if pd.api.types.is_datetime64_any_dtype(series):
            out = pd.to_datetime(series, errors="coerce")
            try:
                out = out.dt.tz_localize(None)
            except Exception:
                pass
            return out

        raw = _clean_datetime_like_series(series)
        out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")

        patterns = [
            (
                raw.str.match(r"^\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}:\d{2}$", na=False),
                "%Y-%m-%d %H:%M:%S",
            ),
            (
                raw.str.match(r"^\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}$", na=False),
                "%Y-%m-%d %H:%M",
            ),
            (
                raw.str.match(r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2}$", na=False),
                "%Y/%m/%d %H:%M:%S",
            ),
            (
                raw.str.match(r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}$", na=False),
                "%Y/%m/%d %H:%M",
            ),
            (
                raw.str.match(r"^\d{4}-\d{1,2}-\d{1,2}$", na=False),
                "%Y-%m-%d",
            ),
            (
                raw.str.match(r"^\d{4}/\d{1,2}/\d{1,2}$", na=False),
                "%Y/%m/%d",
            ),
        ]

        for mask, fmt in patterns:
            if mask.any():
                idx = mask[mask].index
                out.loc[idx] = pd.to_datetime(raw.loc[idx], errors="coerce", format=fmt)

        if allow_time_only:
            time_hms = raw.str.match(r"^\d{1,2}:\d{2}:\d{2}$", na=False)
            time_hm = raw.str.match(r"^\d{1,2}:\d{2}$", na=False)

            if time_hms.any() or time_hm.any():
                today = pd.Timestamp.now().strftime("%Y-%m-%d")

                if base_date is None:
                    base = pd.Series(today, index=series.index, dtype="object")
                elif isinstance(base_date, pd.Series):
                    base_parsed = _safe_parse_datetime_series(
                        base_date,
                        allow_time_only=False,
                    )
                    if base_parsed.notna().any():
                        base = base_parsed.dt.strftime("%Y-%m-%d").fillna(today)
                    else:
                        base = pd.Series(today, index=series.index, dtype="object")
                else:
                    base_parsed = _safe_parse_datetime_series(
                        pd.Series(base_date, index=series.index),
                        allow_time_only=False,
                    )
                    if base_parsed.notna().any():
                        base = base_parsed.dt.strftime("%Y-%m-%d").fillna(today)
                    else:
                        base = pd.Series(today, index=series.index, dtype="object")

                combined = base.astype(str) + " " + raw.astype(str)

                if time_hms.any():
                    idx = time_hms[time_hms].index
                    out.loc[idx] = pd.to_datetime(
                        combined.loc[idx],
                        errors="coerce",
                        format="%Y-%m-%d %H:%M:%S",
                    )

                if time_hm.any():
                    idx = time_hm[time_hm].index
                    out.loc[idx] = pd.to_datetime(
                        combined.loc[idx],
                        errors="coerce",
                        format="%Y-%m-%d %H:%M",
                    )

        remaining = out.isna() & raw.notna()
        if remaining.any():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                out.loc[remaining] = pd.to_datetime(raw.loc[remaining], errors="coerce")

        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass

        return out

    except Exception:
        logger.debug("[summary.engine.common] safe datetime parse failed", exc_info=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                return pd.to_datetime(pd.Series(series), errors="coerce")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")


def coerce_datetime_series(series: Any) -> pd.Series:
    """
    Series を datetime に安全変換し、tz-naive 化する。
    """
    return _safe_parse_datetime_series(series, allow_time_only=True)


def normalize_datetime_columns(
    df: Any,
    columns: Iterable[str] = (
        "datetime",
        "end_time",
        "time",
        "start_time",
        "snapshot_time",
        "received_at",
        "CurrentPriceTime",
        "current_price_time",
    ),
) -> pd.DataFrame:
    """
    複数の datetime 候補列をまとめて datetime64[ns] / tz-naive に正規化。
    """
    out = ensure_dataframe(df)
    if out.empty:
        return out

    base_date = None
    if "date" in out.columns:
        base_date = out["date"]
    elif "datetime" in out.columns:
        base_date = out["datetime"]

    for c in columns:
        if c in out.columns:
            try:
                out[c] = _safe_parse_datetime_series(
                    out[c],
                    base_date=base_date,
                    allow_time_only=True,
                )
            except Exception:
                logger.debug("[summary.engine.common] normalize datetime failed col=%s", c, exc_info=True)

    return out


def ensure_datetime(df: Any) -> pd.DataFrame:
    """
    datetime 列が無い場合でも、既存の候補列から datetime を生成する。
    """
    out = ensure_dataframe(df)
    if out.empty:
        return out

    if "datetime" in out.columns:
        out["datetime"] = coerce_datetime_series(out["datetime"])
        return out

    try:
        if "date" in out.columns and "start_time" in out.columns:
            out["datetime"] = _safe_parse_datetime_series(
                out["date"].astype(str).str.strip()
                + " "
                + out["start_time"].astype(str).str.strip(),
                allow_time_only=False,
            )
        elif "date" in out.columns and "time" in out.columns:
            out["datetime"] = _safe_parse_datetime_series(
                out["date"].astype(str).str.strip()
                + " "
                + out["time"].astype(str).str.strip(),
                allow_time_only=False,
            )
        else:
            for c in (
                "end_time",
                "snapshot_time",
                "CurrentPriceTime",
                "current_price_time",
                "received_at",
                "time",
                "start_time",
            ):
                if c in out.columns:
                    base_date = out["date"] if "date" in out.columns else None
                    out["datetime"] = _safe_parse_datetime_series(
                        out[c],
                        base_date=base_date,
                        allow_time_only=True,
                    )
                    break

        if "datetime" not in out.columns:
            out["datetime"] = pd.NaT

    except Exception:
        out["datetime"] = pd.NaT

    out["datetime"] = coerce_datetime_series(out["datetime"])
    return out


# ============================================================
# misc helpers
# ============================================================

def latest_timestamp(df: Any):
    """
    DataFrame 内の代表 datetime 列から最新時刻を返す。
    """
    out = ensure_dataframe(df)
    if out.empty:
        return None

    for col in (
        "datetime",
        "end_time",
        "start_time",
        "snapshot_time",
        "received_at",
        "CurrentPriceTime",
        "current_price_time",
    ):
        if col in out.columns:
            try:
                s = coerce_datetime_series(out[col]).dropna()
                if not s.empty:
                    return s.max()
            except Exception:
                pass
    return None


def symbol_count(df: Any) -> int:
    out = ensure_dataframe(df)
    if out.empty or "symbol" not in out.columns:
        return 0

    try:
        s = out["symbol"].astype(str).str.strip()
        s = s.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
        return int(s.dropna().nunique())
    except Exception:
        return 0


def log_df_profile(label: str, df: Any) -> None:
    """
    デバッグ用の軽い DataFrame プロファイルログ。
    """
    try:
        out = ensure_dataframe(df)
        logger.info(
            "[summary.engine.common] %s rows=%s cols=%s symbols=%s latest_dt=%s",
            label,
            len(out),
            len(out.columns),
            symbol_count(out),
            latest_timestamp(out),
        )
    except Exception:
        logger.exception("[summary.engine.common] log_df_profile failed label=%s", label)


__all__ = [
    "interval_label",
    "normalize_interval_value",
    "ensure_dataframe",
    "empty_df",
    "empty_result",
    "make_empty_result",
    "empty_summary_result",
    "safe_get_series",
    "normalize_symbol_column",
    "coerce_datetime_series",
    "normalize_datetime_columns",
    "ensure_datetime",
    "latest_timestamp",
    "symbol_count",
    "log_df_profile",
]