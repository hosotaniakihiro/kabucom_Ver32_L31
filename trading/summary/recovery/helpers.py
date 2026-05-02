# ============================================================
# File   : trading/summary/recovery/helpers.py
# Ver    : PRODUCTION-STABLE-REV7.1-DELTA-FIRST-HELPERS-DATETIME-SAFE
# ------------------------------------------------------------
# 【概要】
#   サマリー復元・差分再構築で共通利用する helper 群
#
# 【主な機能】
#   - DataFrame 安全化
#   - 重複列の coalesce
#   - symbol 正規化
#   - datetime/date/time/time_range 正規化
#   - OHLC alias 列の吸収
#   - 営業日・対象日判定
#   - 日付ガード
#   - 複数 summary frame の優先マージ
#
# 【今回の修正】
#   - pd.to_datetime(..., errors="coerce") の直接呼び出しで出る
#     UserWarning: Could not infer format...
#     を抑止
#   - to_datetime_naive() を安全変換版へ変更
#   - normalize_datetime_columns() 内の datetime/date/time/start_time/end_time 変換を安全化
#   - repair_datetime_from_time_range() / build_time_range_from_datetime() / date guard も安全変換へ統一
#   - pandas format="mixed" 非依存
#
# 【設計方針】
#   - 本モジュールは「共通整形」に責務を限定
#   - DB / rebuild / persistence には依存しない
#   - 既存システム互換の列名揺れをできるだけ吸収
#   - 起動時差分復元に必要な日付ガードを内包
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import warnings
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# Generic dataframe / series helpers
# ============================================================

def ensure_dataframe(df) -> pd.DataFrame:
    """
    入力をできるだけ安全に DataFrame 化する。
    """
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
            logger.exception("[summary.recovery.helpers] dataframe conversion failed")
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        out = out.reset_index(drop=True)
    except Exception:
        pass
    return out


def safe_get_series(df: pd.DataFrame, col: str) -> Optional[pd.Series]:
    """
    重複列や DataFrame 化された列でもできるだけ Series として取り出す。
    """
    try:
        if df is None or df.empty or col not in df.columns:
            return None

        value = df[col]

        if isinstance(value, pd.DataFrame):
            if value.shape[1] <= 0:
                return None

            out = None
            for i in range(value.shape[1]):
                s = value.iloc[:, i]
                if out is None:
                    out = s
                else:
                    try:
                        out = out.combine_first(s)
                    except Exception:
                        try:
                            out = out.where(out.notna(), s)
                        except Exception:
                            pass
            return out

        if isinstance(value, pd.Series):
            return value

        return pd.Series(value, index=df.index)

    except Exception:
        logger.exception("[summary.recovery.helpers] safe_get_series failed col=%s", col)
        return None


# ============================================================
# Datetime safe parser
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
    s,
    *,
    base_date=None,
    allow_time_only: bool = True,
) -> pd.Series:
    """
    warning を出さずに datetime へ変換する。

    対応:
      - 2026-04-20 09:52:00
      - 2026-04-20 09:52
      - 2026/04/20 09:52:00
      - 2026/04/20 09:52
      - 2026-04-20
      - 2026/04/20
      - 09:52:00
      - 09:52

    時刻だけの場合は base_date を付与する。
    base_date が無い場合は今日の日付を使う。
    """
    try:
        if s is None:
            return pd.Series(dtype="datetime64[ns]")

        if not isinstance(s, pd.Series):
            s = pd.Series(s)

        if pd.api.types.is_datetime64_any_dtype(s):
            out = pd.to_datetime(s, errors="coerce")
            try:
                if getattr(out.dt, "tz", None) is not None:
                    out = out.dt.tz_localize(None)
            except Exception:
                pass
            return out

        raw = _clean_datetime_like_series(s)
        out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")

        masks = [
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

        for mask, fmt in masks:
            if mask.any():
                idx = mask[mask].index
                out.loc[idx] = pd.to_datetime(
                    raw.loc[idx],
                    errors="coerce",
                    format=fmt,
                )

        if allow_time_only:
            time_hms = raw.str.match(r"^\d{1,2}:\d{2}:\d{2}$", na=False)
            time_hm = raw.str.match(r"^\d{1,2}:\d{2}$", na=False)

            if time_hms.any() or time_hm.any():
                today_str = pd.Timestamp.now().strftime("%Y-%m-%d")

                if base_date is None:
                    base_date_s = pd.Series(today_str, index=s.index, dtype="object")
                elif isinstance(base_date, pd.Series):
                    try:
                        if pd.api.types.is_datetime64_any_dtype(base_date):
                            base_date_s = pd.to_datetime(base_date, errors="coerce").dt.strftime("%Y-%m-%d")
                        else:
                            # base_date 自体が時刻のみだと困るため、date形式のみ安全に処理
                            base_raw = _clean_datetime_like_series(base_date)
                            base_parsed = pd.Series(pd.NaT, index=base_date.index, dtype="datetime64[ns]")

                            dash_date = base_raw.str.match(r"^\d{4}-\d{1,2}-\d{1,2}$", na=False)
                            slash_date = base_raw.str.match(r"^\d{4}/\d{1,2}/\d{1,2}$", na=False)
                            dash_dt = base_raw.str.match(
                                r"^\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(:\d{2})?$",
                                na=False,
                            )
                            slash_dt = base_raw.str.match(
                                r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}(:\d{2})?$",
                                na=False,
                            )

                            if dash_date.any():
                                idx = dash_date[dash_date].index
                                base_parsed.loc[idx] = pd.to_datetime(
                                    base_raw.loc[idx],
                                    errors="coerce",
                                    format="%Y-%m-%d",
                                )
                            if slash_date.any():
                                idx = slash_date[slash_date].index
                                base_parsed.loc[idx] = pd.to_datetime(
                                    base_raw.loc[idx],
                                    errors="coerce",
                                    format="%Y/%m/%d",
                                )
                            if dash_dt.any():
                                idx = dash_dt[dash_dt].index
                                with warnings.catch_warnings():
                                    warnings.simplefilter("ignore", UserWarning)
                                    base_parsed.loc[idx] = pd.to_datetime(base_raw.loc[idx], errors="coerce")
                            if slash_dt.any():
                                idx = slash_dt[slash_dt].index
                                with warnings.catch_warnings():
                                    warnings.simplefilter("ignore", UserWarning)
                                    base_parsed.loc[idx] = pd.to_datetime(base_raw.loc[idx], errors="coerce")

                            base_date_s = base_parsed.dt.strftime("%Y-%m-%d")

                        base_date_s = base_date_s.fillna(today_str).replace("NaT", today_str)
                    except Exception:
                        base_date_s = pd.Series(today_str, index=s.index, dtype="object")
                else:
                    try:
                        parsed_base = _safe_parse_datetime_series(pd.Series(base_date, index=s.index), allow_time_only=False)
                        if parsed_base.notna().any():
                            base_date_s = parsed_base.dt.strftime("%Y-%m-%d").fillna(today_str)
                        else:
                            base_date_s = pd.Series(str(base_date), index=s.index, dtype="object")
                    except Exception:
                        base_date_s = pd.Series(today_str, index=s.index, dtype="object")

                try:
                    base_date_s = base_date_s.fillna(today_str).replace("NaT", today_str)
                except Exception:
                    base_date_s = pd.Series(today_str, index=s.index, dtype="object")

                combined = base_date_s.astype(str) + " " + raw.astype(str)

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
            if getattr(out.dt, "tz", None) is not None:
                out = out.dt.tz_localize(None)
        except Exception:
            pass

        return out

    except Exception:
        logger.exception("[summary.recovery.helpers] safe datetime parse failed")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                return pd.to_datetime(pd.Series(s), errors="coerce")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")


def to_datetime_naive(s) -> pd.Series:
    """
    timezone aware/naive 混在を避けるため tz を外した datetime series を返す。
    warning が出る pd.to_datetime(s, errors="coerce") 直接呼び出しは使わない。
    """
    return _safe_parse_datetime_series(s, allow_time_only=True)


# ============================================================
# Symbol helpers
# ============================================================

def looks_like_symbol_series(s: pd.Series) -> bool:
    """
    JP株の symbol らしい series かを緩く判定する。
    例: 1332, 285A など
    """
    try:
        if s is None:
            return False

        x = s.astype(str).str.strip()
        x = x.replace(
            {"": pd.NA, "nan": pd.NA, "None": pd.NA, "null": pd.NA, "<NA>": pd.NA}
        ).dropna()

        if x.empty:
            return False

        hit = x.str.match(r"^[0-9]{4}[A-Z]?$", na=False)
        ratio = float(hit.mean()) if len(hit) > 0 else 0.0
        return ratio >= 0.7

    except Exception:
        return False


def cleanup_symbol_series(s: pd.Series) -> pd.Series:
    """
    symbol の .0 や空文字などを除去して揃える。
    """
    try:
        out = s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        out = out.replace(
            {"": pd.NA, "nan": pd.NA, "None": pd.NA, "null": pd.NA, "<NA>": pd.NA}
        )
        return out
    except Exception:
        return s


def normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    """
    symbol 列の名前揺れや index 上の symbol を吸収して symbol 列へ寄せる。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    try:
        if "symbol" not in out.columns and getattr(out.index, "name", None) == "symbol":
            out = out.reset_index()
    except Exception:
        logger.debug("[summary.recovery.helpers] reset_index for symbol failed", exc_info=True)

    try:
        if "symbol" not in out.columns and not isinstance(out.index, pd.RangeIndex):
            out = out.reset_index()
    except Exception:
        logger.debug("[summary.recovery.helpers] generic reset_index for symbol failed", exc_info=True)

    for col in (
        "symbol",
        "Symbol",
        "SYMBOL",
        "code",
        "Code",
        "ticker",
        "Ticker",
        "stock_code",
        "symbol_x",
        "symbol_y",
        "Symbol_x",
        "Symbol_y",
        "銘柄コード",
        "level_0",
        "index",
    ):
        if col in out.columns:
            try:
                s = safe_get_series(out, col)
                if s is not None and looks_like_symbol_series(s):
                    out["symbol"] = cleanup_symbol_series(s)
                    break
            except Exception:
                logger.debug(
                    "[summary.recovery.helpers] symbol normalize failed col=%s",
                    col,
                    exc_info=True,
                )

    if "symbol" not in out.columns:
        try:
            for c in out.columns:
                s = safe_get_series(out, c)
                if s is not None and looks_like_symbol_series(s):
                    out["symbol"] = cleanup_symbol_series(s)
                    logger.info("[summary.recovery.helpers] symbol rescued from column=%s", c)
                    break
        except Exception:
            logger.debug("[summary.recovery.helpers] symbol rescue scan failed", exc_info=True)

    if "symbol" in out.columns:
        try:
            out["symbol"] = cleanup_symbol_series(out["symbol"])
        except Exception:
            logger.debug("[summary.recovery.helpers] symbol cleanup failed", exc_info=True)

    return out


# ============================================================
# Duplicate columns / alias helpers
# ============================================================

def coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    同名列が複数ある場合に左優先で1列へ潰す。
    """
    df = ensure_dataframe(df)
    if df.empty:
        return df

    try:
        cols = list(df.columns)
        if len(cols) == len(set(cols)):
            return df
    except Exception:
        return df

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
            else:
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
        logger.exception("[summary.recovery.helpers] duplicate coalesce failed")
        try:
            return df.loc[:, ~df.columns.duplicated()].copy()
        except Exception:
            return df


def coalesce_first_numeric(df: pd.DataFrame, dest: str, candidates: Sequence[str]) -> pd.DataFrame:
    """
    候補列から最初に取れる数値を dest へ寄せる。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()
    if dest not in out.columns:
        out[dest] = pd.NA

    try:
        base = pd.to_numeric(safe_get_series(out, dest), errors="coerce")
    except Exception:
        base = pd.Series(pd.NA, index=out.index)

    for c in candidates:
        if c not in out.columns:
            continue
        try:
            s = pd.to_numeric(safe_get_series(out, c), errors="coerce")
            try:
                base = base.combine_first(s)
            except Exception:
                base = base.where(base.notna(), s)
        except Exception:
            logger.debug(
                "[summary.recovery.helpers] coalesce numeric failed dest=%s src=%s",
                dest,
                c,
                exc_info=True,
            )

    out[dest] = base
    return out


def repair_ohlc_alias(df: pd.DataFrame) -> pd.DataFrame:
    """
    OHLC / price / volume の列名揺れを吸収し、主要列へ寄せる。
    """
    df = ensure_dataframe(df)
    if df.empty:
        return df

    out = df.copy()

    forward_map = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
        "openPrice": "open",
        "highPrice": "high",
        "lowPrice": "low",
        "closePrice": "close",
        "OpenPrice": "open",
        "HighPrice": "high",
        "LowPrice": "low",
        "ClosePrice": "close",
    }
    reverse_map = {
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "close_price",
    }

    for src, dst in forward_map.items():
        if src in out.columns and dst not in out.columns:
            s = safe_get_series(out, src)
            if s is not None:
                out[dst] = s

    for src, dst in reverse_map.items():
        if src in out.columns and dst not in out.columns:
            s = safe_get_series(out, src)
            if s is not None:
                out[dst] = s

    out = coalesce_first_numeric(out, "close", [
        "close", "close_price", "price", "Price",
        "current_price", "CurrentPrice", "currentPrice",
        "last_price", "LastPrice", "last",
        "closePrice", "ClosePrice",
    ])
    out = coalesce_first_numeric(out, "close_price", [
        "close_price", "close", "price", "Price",
        "current_price", "CurrentPrice", "currentPrice",
        "last_price", "LastPrice", "last",
        "closePrice", "ClosePrice",
    ])

    out = coalesce_first_numeric(out, "open", [
        "open", "open_price", "openPrice", "OpenPrice",
        "close", "close_price",
    ])
    out = coalesce_first_numeric(out, "high", [
        "high", "high_price", "highPrice", "HighPrice",
        "close", "close_price",
    ])
    out = coalesce_first_numeric(out, "low", [
        "low", "low_price", "lowPrice", "LowPrice",
        "close", "close_price",
    ])
    out = coalesce_first_numeric(out, "volume", [
        "volume", "Volume", "trading_volume", "TradingVolume", "qty", "cum_volume",
    ])

    try:
        close_num = pd.to_numeric(safe_get_series(out, "close"), errors="coerce")
        close_num = close_num.replace([np.inf, -np.inf], np.nan).mask(close_num <= 0, np.nan)
        for c in ("open", "high", "low"):
            s = pd.to_numeric(safe_get_series(out, c), errors="coerce")
            s = s.replace([np.inf, -np.inf], np.nan).mask(s <= 0, np.nan)
            try:
                out[c] = s.combine_first(close_num)
            except Exception:
                out[c] = s.where(s.notna(), close_num)
    except Exception:
        logger.debug("[summary.recovery.helpers] ohlc backfill from close failed", exc_info=True)

    alias_map = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
        "price": "close",
        "current_price": "close",
        "CurrentPrice": "close",
        "last_price": "close",
        "LastPrice": "close",
        "trading_volume": "volume",
        "TradingVolume": "volume",
    }

    for alias, src in alias_map.items():
        try:
            if alias not in out.columns:
                out[alias] = pd.to_numeric(safe_get_series(out, src), errors="coerce")
            else:
                base = pd.to_numeric(safe_get_series(out, alias), errors="coerce")
                src_s = pd.to_numeric(safe_get_series(out, src), errors="coerce")
                try:
                    out[alias] = base.combine_first(src_s)
                except Exception:
                    out[alias] = base.where(base.notna(), src_s)
        except Exception:
            logger.debug(
                "[summary.recovery.helpers] alias sync failed alias=%s src=%s",
                alias,
                src,
                exc_info=True,
            )

    return out


# ============================================================
# Datetime / time_range helpers
# ============================================================

def build_time_range_from_datetime(dt_series: pd.Series, interval: int) -> pd.Series:
    """
    datetime から HH:MM-HH:MM 形式の time_range を生成する。
    """
    try:
        base = to_datetime_naive(dt_series)
        start = base.dt.floor(f"{int(interval)}min")
        end = start + pd.to_timedelta(int(interval) - 1, unit="min")
        return start.dt.strftime("%H:%M") + "-" + end.dt.strftime("%H:%M")
    except Exception:
        logger.exception(
            "[summary.recovery.helpers] build time_range failed interval=%s",
            interval,
        )
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)


def _derive_base_date_for_time_only(out: pd.DataFrame) -> pd.Series:
    today_str = pd.Timestamp.now().strftime("%Y-%m-%d")

    for c in ("date", "datetime", "end_time", "start_time", "snapshot_time", "tick_time"):
        if c not in out.columns:
            continue

        try:
            s = safe_get_series(out, c)
            if s is None:
                continue

            parsed = _safe_parse_datetime_series(s, allow_time_only=False)
            if parsed.notna().any():
                return parsed.dt.strftime("%Y-%m-%d").fillna(today_str)
        except Exception:
            logger.debug("[summary.recovery.helpers] derive base date failed col=%s", c, exc_info=True)

    return pd.Series(today_str, index=out.index, dtype="object")


def repair_datetime_from_time_range(df: pd.DataFrame, interval: int = 1) -> pd.DataFrame:
    """
    datetime が欠けている場合、date + time_range から救済する。
    """
    out = ensure_dataframe(df)
    if out.empty or "time_range" not in out.columns:
        return out

    tr = safe_get_series(out, "time_range")
    if tr is None:
        return out

    tr = tr.astype(str).str.strip()

    if "datetime" not in out.columns:
        out["datetime"] = pd.NaT

    dt_existing = to_datetime_naive(safe_get_series(out, "datetime"))
    need_dt = dt_existing.isna()

    hhmm = tr.str.extract(r"^\s*(\d{1,2}:\d{2})(?:\s*-\s*(\d{1,2}:\d{2}))?\s*$")
    start_hhmm = hhmm[0]
    end_hhmm = hhmm[1].fillna(start_hhmm)

    if "date" in out.columns:
        date_s = _safe_parse_datetime_series(safe_get_series(out, "date"), allow_time_only=False).dt.strftime("%Y-%m-%d")
        date_s = date_s.fillna(pd.Timestamp.now().strftime("%Y-%m-%d"))

        parsed_from_date_end = _safe_parse_datetime_series(
            date_s.astype(str) + " " + end_hhmm.astype(str),
            allow_time_only=False,
        )
        parsed_from_date_start = _safe_parse_datetime_series(
            date_s.astype(str) + " " + start_hhmm.astype(str),
            allow_time_only=False,
        )
    else:
        parsed_from_date_end = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns]")
        parsed_from_date_start = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns]")

    try:
        out.loc[need_dt, "datetime"] = parsed_from_date_end.loc[need_dt]
    except Exception:
        logger.debug("[summary.recovery.helpers] datetime repair assign failed", exc_info=True)

    dt_fixed = to_datetime_naive(safe_get_series(out, "datetime"))

    if "time" not in out.columns:
        out["time"] = dt_fixed.dt.strftime("%H:%M:%S")
    if "end_time" not in out.columns:
        out["end_time"] = parsed_from_date_end.dt.strftime("%H:%M:%S")
    if "start_time" not in out.columns:
        out["start_time"] = parsed_from_date_start.dt.strftime("%H:%M:%S")
    if "date" not in out.columns:
        out["date"] = dt_fixed.dt.strftime("%Y-%m-%d")
    if "symbolname" not in out.columns:
        out["symbolname"] = ""

    return out


def normalize_datetime_columns(df: pd.DataFrame, interval: int = 1) -> pd.DataFrame:
    """
    datetime/date/time/start_time/end_time/time_range を整える。
    """
    df = ensure_dataframe(df)
    if df.empty:
        return df

    out = df.copy()
    out = coalesce_duplicate_columns(out)
    out = normalize_symbol(out)
    out = repair_ohlc_alias(out)

    try:
        if "datetime" in out.columns:
            out["datetime"] = to_datetime_naive(safe_get_series(out, "datetime"))
        else:
            date_col = "date" if "date" in out.columns else None
            time_col = None
            for c in ("time", "end_time", "start_time", "snapshot_time", "tick_time"):
                if c in out.columns:
                    time_col = c
                    break

            if date_col and time_col:
                date_s = _safe_parse_datetime_series(
                    safe_get_series(out, date_col),
                    allow_time_only=False,
                ).dt.strftime("%Y-%m-%d")
                date_s = date_s.fillna(pd.Timestamp.now().strftime("%Y-%m-%d"))

                time_s = safe_get_series(out, time_col).astype(str).str.strip()
                out["datetime"] = _safe_parse_datetime_series(
                    date_s.astype(str) + " " + time_s.astype(str),
                    allow_time_only=False,
                )
            elif time_col:
                base_date = _derive_base_date_for_time_only(out)
                out["datetime"] = _safe_parse_datetime_series(
                    safe_get_series(out, time_col),
                    base_date=base_date,
                    allow_time_only=True,
                )
            else:
                out["datetime"] = pd.NaT

        try:
            if getattr(out["datetime"].dt, "tz", None) is not None:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass

    except Exception:
        logger.exception("[summary.recovery.helpers] datetime normalize failed")

    try:
        out = repair_datetime_from_time_range(out, interval=interval)
    except Exception:
        logger.exception(
            "[summary.recovery.helpers] datetime repair from time_range failed interval=%s",
            interval,
        )

    try:
        if "date" not in out.columns and "datetime" in out.columns:
            out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
        elif "date" in out.columns:
            existing_date = _safe_parse_datetime_series(safe_get_series(out, "date"), allow_time_only=False).dt.strftime("%Y-%m-%d")
            derived_date = to_datetime_naive(safe_get_series(out, "datetime")).dt.strftime("%Y-%m-%d")
            try:
                out["date"] = existing_date.combine_first(derived_date)
            except Exception:
                out["date"] = existing_date.where(existing_date.notna(), derived_date)
    except Exception:
        logger.debug("[summary.recovery.helpers] date normalize failed", exc_info=True)

    try:
        dt_s = to_datetime_naive(safe_get_series(out, "datetime"))
        if "time" not in out.columns:
            out["time"] = dt_s.dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[summary.recovery.helpers] time normalize failed", exc_info=True)

    try:
        dt_s = to_datetime_naive(safe_get_series(out, "datetime"))
        if "start_time" not in out.columns:
            out["start_time"] = dt_s.dt.floor(f"{int(interval)}min").dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[summary.recovery.helpers] start_time normalize failed", exc_info=True)

    try:
        dt_s = to_datetime_naive(safe_get_series(out, "datetime"))
        if "end_time" not in out.columns:
            out["end_time"] = dt_s.dt.strftime("%H:%M:%S")
    except Exception:
        logger.debug("[summary.recovery.helpers] end_time normalize failed", exc_info=True)

    try:
        if "time_range" not in out.columns and "datetime" in out.columns:
            out["time_range"] = build_time_range_from_datetime(out["datetime"], interval)
        else:
            tr = safe_get_series(out, "time_range")
            if tr is not None:
                tr_str = tr.astype(str)
                need_fill = (
                    tr.isna()
                    | (tr_str.str.strip() == "")
                    | tr_str.isin(["1min", "3min", "5min", "10min", "15min", "30min", "60min", "unknown"])
                )
                if need_fill.any() and "datetime" in out.columns:
                    built = build_time_range_from_datetime(out["datetime"], interval)
                    out.loc[need_fill, "time_range"] = built.loc[need_fill]
    except Exception:
        logger.debug("[summary.recovery.helpers] time_range normalize failed", exc_info=True)

    try:
        if "symbolname" in out.columns:
            out["symbolname"] = safe_get_series(out, "symbolname").fillna("").astype(str)
    except Exception:
        logger.debug("[summary.recovery.helpers] symbolname normalize failed", exc_info=True)

    try:
        sort_cols = []
        if "symbol" in out.columns:
            sort_cols.append("symbol")
        if "datetime" in out.columns:
            sort_cols.append("datetime")
        if sort_cols:
            out = out.sort_values(sort_cols, kind="stable").reset_index(drop=True)
    except Exception:
        logger.debug("[summary.recovery.helpers] sort failed", exc_info=True)

    return out


# ============================================================
# Merge helpers
# ============================================================

def merge_summary_frames_with_priority(*frames: pd.DataFrame, interval: int = 1) -> pd.DataFrame:
    """
    複数の summary frame を後勝ちでマージする。
    """
    valid = []
    for df in frames:
        ndf = normalize_datetime_columns(df, interval=interval)
        if not ndf.empty:
            valid.append(ndf)

    if not valid:
        return pd.DataFrame()

    try:
        out = pd.concat(valid, ignore_index=True, sort=False)
        out = normalize_datetime_columns(out, interval=interval)

        if {"symbol", "datetime"}.issubset(out.columns):
            out = (
                out.dropna(subset=["symbol", "datetime"])
                .sort_values(["symbol", "datetime"], kind="stable")
                .drop_duplicates(["symbol", "datetime"], keep="last")
                .reset_index(drop=True)
            )
        return out

    except Exception:
        logger.exception(
            "[summary.recovery.helpers] merge with priority failed interval=%s",
            interval,
        )
        return valid[-1].copy()


# ============================================================
# Business-day helpers
# ============================================================

def today() -> dt.date:
    return dt.datetime.now().date()


def get_previous_business_day(base_date: dt.date) -> dt.date:
    """
    utils.business_day_utils があれば優先利用、なければ土日だけを除外。
    """
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
        return today().weekday() < 5


def target_dates(include_previous_business_day: bool = True) -> list[dt.date]:
    """
    起動時に許容する対象日を返す。
    営業日:
      - 前営業日 + 当日
    休場日:
      - 前営業日
    """
    td = today()
    prev_bd = get_previous_business_day(td)
    business_day = is_today_business_day()

    if business_day:
        dates = [prev_bd, td] if include_previous_business_day else [td]
    else:
        dates = [prev_bd]

    logger.info(
        "[summary.recovery.helpers] target_dates business_day=%s include_previous_business_day=%s -> %s",
        business_day,
        include_previous_business_day,
        [str(x) for x in dates],
    )
    return dates


def extract_dates_from_datetime_like(series: pd.Series) -> list[dt.date]:
    """
    datetime-like series から date のユニーク一覧を返す。
    """
    try:
        s = to_datetime_naive(series).dropna()
        if s.empty:
            return []
        return sorted({x.date() for x in s})
    except Exception:
        logger.debug("[summary.recovery.helpers] extract dates failed", exc_info=True)
        return []


# ============================================================
# Date guard helpers
# ============================================================

def drop_rows_outside_allowed_dates(
    df: pd.DataFrame,
    *,
    label: str,
    include_previous_business_day: bool = True,
    interval: int = 1,
) -> pd.DataFrame:
    """
    target_dates の範囲外の行を落とす。
    """
    out = normalize_datetime_columns(df, interval=interval)
    if out.empty or "datetime" not in out.columns:
        return out

    allowed = set(target_dates(include_previous_business_day=include_previous_business_day))
    row_dates = to_datetime_naive(safe_get_series(out, "datetime")).dt.date
    keep_mask = row_dates.isin(allowed)

    before = len(out)
    out = out.loc[keep_mask.fillna(False)].copy().reset_index(drop=True)

    logger.info(
        "[summary.recovery.helpers] %s date guard rows=%d -> %d allowed_dates=%s",
        label,
        before,
        len(out),
        sorted(str(x) for x in allowed),
    )
    return out


def drop_rows_to_explicit_dates(
    df: pd.DataFrame,
    *,
    allowed_dates: Iterable[dt.date],
    label: str,
    interval: int = 1,
) -> pd.DataFrame:
    """
    明示的に与えられた allowed_dates に行を絞る。
    """
    out = normalize_datetime_columns(df, interval=interval)
    if out.empty or "datetime" not in out.columns:
        return out

    allowed = {to_datetime_naive(pd.Series([x])).dropna().iloc[0].date() for x in allowed_dates if x is not None}
    row_dates = to_datetime_naive(safe_get_series(out, "datetime")).dt.date
    keep_mask = row_dates.isin(allowed)

    before = len(out)
    out = out.loc[keep_mask.fillna(False)].copy().reset_index(drop=True)

    logger.info(
        "[summary.recovery.helpers] %s explicit date guard rows=%d -> %d allowed_dates=%s",
        label,
        before,
        len(out),
        sorted(str(x) for x in allowed),
    )
    return out


__all__ = [
    "ensure_dataframe",
    "safe_get_series",
    "to_datetime_naive",
    "looks_like_symbol_series",
    "cleanup_symbol_series",
    "normalize_symbol",
    "coalesce_duplicate_columns",
    "coalesce_first_numeric",
    "repair_ohlc_alias",
    "build_time_range_from_datetime",
    "repair_datetime_from_time_range",
    "normalize_datetime_columns",
    "merge_summary_frames_with_priority",
    "today",
    "get_previous_business_day",
    "is_today_business_day",
    "target_dates",
    "extract_dates_from_datetime_like",
    "drop_rows_outside_allowed_dates",
    "drop_rows_to_explicit_dates",
]