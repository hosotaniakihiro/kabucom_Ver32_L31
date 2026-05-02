# ============================================================
# File   : trading/summary/postprocess/filtering.py
# Version: PRODUCTION-STABLE-REV1.1-POSTPROCESS-FILTERING
#          -DATETIME-WARNING-FIX
#          -DATE-GUARD-SAFE
#          -SLOPE-MTF-PRESERVE
# ------------------------------------------------------------
# 【概要】
#   summary postprocess 用 filtering
#
# 【主な機能】
#   ✔ 日付ガード
#   ✔ market / universe filter
#   ✔ ETF/ETN/REIT/FUND系除外
#   ✔ dead row 除去
#   ✔ deduplicate
#
# 【REV1.1 修正】
#   ✔ pd.to_datetime(..., errors="coerce") 直接呼び出しによる
#     UserWarning: Could not infer format...
#     を抑制
#
#   ✔ datetime が壊れている場合でも:
#       - datetime
#       - date + time
#       - date + end_time
#       - date + start_time
#       - end_time単体
#     から復元して date guard を行う
#
#   ✔ date guard で全件NaTになった場合は全削除せず skip
#
#   ✔ slope / slope_atr_scaled / score_slope を相互補完
#   ✔ mtf / score_mtf / mtf_alignment 系を相互補完
#   ✔ dead row判定で slope/mtf の非ゼロ候補を潰さない
#
# 【重要】
#   - date guard は安全側:
#       datetimeが復元できない場合、全削除ではなくskipする
#   - market filter は既存通り allowed universe / market_type を優先
#   - deduplicate は symbol単位で最良score行を残す
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import warnings
from typing import Optional, Iterable

import numpy as np
import pandas as pd

from .normalize import (
    allowed_post_dates_fallback,
    extract_actual_dates_from_df,
    pick_series,
    safe_series,
)

logger = logging.getLogger(__name__)


_ALLOWED_MARKETS = {"プライム", "スタンダード", "グロース"}

_EXCLUDE_NAME_KEYWORDS = (
    "ETF",
    "ETN",
    "REIT",
    "指数",
    "連動",
    "レバ",
    "ダブル",
    "ベア",
    "インデックス",
    "上場投信",
    "投資口",
    "J-REIT",
    "REIT ETF",
    "FUND",
    "ファンド",
    "投信",
)


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
                    "[POST.FILTER] datetime pattern parse failed col=%s fmt=%s",
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
        logger.exception("[POST.FILTER] safe datetime parse failed col=%s", col_name)
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
            logger.debug("[POST.FILTER] base date candidate failed col=%s", col, exc_info=True)

    return pd.Series(today, index=df.index, dtype="object")


def _recover_datetime_from_date_time(df: pd.DataFrame) -> pd.Series:
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
                logger.debug("[POST.FILTER] datetime recovery failed tcol=%s", tcol, exc_info=True)

        if best.notna().any():
            return best

        return _safe_to_datetime_series(
            date_s,
            base_df=df,
            col_name="date",
            allow_time_only=False,
        )

    except Exception:
        logger.exception("[POST.FILTER] recover datetime failed")
        return pd.Series(pd.NaT, index=idx, dtype="datetime64[ns]")


def normalize_datetime_for_filter(df: pd.DataFrame) -> pd.DataFrame:
    """
    filtering用のdatetime正規化。
    datetimeが無い/NaTの場合は date + time/end_time/start_time から復元する。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

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
                    "[POST.FILTER] datetime recovered rows=%s recovered=%s",
                    len(out),
                    int(recovered.notna().sum()),
                )

        elif out["datetime"].isna().any():
            recovered = _recover_datetime_from_date_time(out)
            mask = out["datetime"].isna() & recovered.notna()
            if mask.any():
                out.loc[mask, "datetime"] = recovered.loc[mask]
                logger.info(
                    "[POST.FILTER] datetime partially recovered rows=%s recovered=%s",
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
        if valid.any():
            out.loc[valid, "datetime"] = out.loc[valid, "datetime"].dt.floor("min")
            out.loc[valid, "date"] = out.loc[valid, "datetime"].dt.strftime("%Y-%m-%d")
            out.loc[valid, "time"] = out.loc[valid, "datetime"].dt.strftime("%H:%M:%S")

        invalid = int(out["datetime"].isna().sum())
        if invalid > 0:
            logger.warning(
                "[POST.FILTER] datetime invalid rows remain invalid=%s total=%s",
                invalid,
                len(out),
            )

        return out

    except Exception:
        logger.exception("[POST.FILTER] normalize datetime failed")
        return df.copy()


# ============================================================
# basic text / symbol helpers
# ============================================================

def normalize_market_text(s: object) -> str:
    txt = str(s or "").strip().upper()
    mapping = {
        "PRIME": "プライム",
        "STANDARD": "スタンダード",
        "GROWTH": "グロース",
        "P": "プライム",
        "S": "スタンダード",
        "G": "グロース",
        "TP": "プライム",
        "TS": "スタンダード",
        "TG": "グロース",
    }
    return mapping.get(txt, str(s or "").strip())


def safe_symbol_series(df: pd.DataFrame) -> pd.Series | None:
    if df is None or df.empty:
        return None

    for col in ("symbol", "code", "ticker", "stock_code"):
        if col in df.columns:
            try:
                s = (
                    df[col]
                    .astype(str)
                    .str.strip()
                    .str.replace(r"\.0$", "", regex=True)
                )
                s = s.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA}).dropna()
                return s
            except Exception:
                logger.debug("[POST.FILTER] safe_symbol_series failed col=%s", col, exc_info=True)
                return None

    return None


# ============================================================
# allowed universe
# ============================================================

def load_allowed_symbol_universe() -> set[str]:
    try:
        from utils.market_filter import get_allowed_symbols
        syms = get_allowed_symbols()
        if syms:
            out = {str(x).strip() for x in syms if str(x).strip()}
            if out:
                logger.info("[POST.FILTER] allowed universe via utils.market_filter count=%d", len(out))
                return out
    except Exception:
        logger.debug("[POST.FILTER] market_filter universe load failed", exc_info=True)

    db_candidates = [
        r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db",
        r"\\192.168.0.22\AutoStockBuyAndSell\basic\symbol_flags.db",
    ]

    sql = """
        SELECT symbol, market_type, symbolname
        FROM symbol_flags
        WHERE market_type IN ('プライム','スタンダード','グロース')
    """

    for db_path in db_candidates:
        try:
            import sqlite3
            with sqlite3.connect(db_path, timeout=15) as conn:
                flags = pd.read_sql(sql, conn)

            if flags.empty or "symbol" not in flags.columns:
                continue

            if "market_type" in flags.columns:
                flags["market_type"] = flags["market_type"].astype(str).map(normalize_market_text)
                flags = flags[flags["market_type"].isin(_ALLOWED_MARKETS)]

            if "symbolname" in flags.columns:
                pat = "|".join(_EXCLUDE_NAME_KEYWORDS)
                mask = ~flags["symbolname"].astype(str).str.contains(
                    pat,
                    case=False,
                    na=False,
                    regex=True,
                )
                flags = flags[mask]

            out = set(flags["symbol"].astype(str).str.strip().tolist())
            if out:
                logger.info("[POST.FILTER] allowed universe via symbol_flags.db count=%d", len(out))
                return out

        except Exception:
            logger.debug("[POST.FILTER] symbol_flags load failed path=%s", db_path, exc_info=True)

    logger.warning("[POST.FILTER] allowed universe unresolved")
    return set()


# ============================================================
# slope / mtf preservation
# ============================================================

def _safe_numeric(s, index, default=np.nan, fillna: bool = False) -> pd.Series:
    try:
        if isinstance(s, pd.Series):
            out = pd.to_numeric(s, errors="coerce")
            out = out.replace([np.inf, -np.inf], np.nan)
            if len(out) != len(index):
                out = out.reindex(index)
            if fillna:
                out = out.fillna(default)
            return out
    except Exception:
        pass

    if fillna:
        return pd.Series(default, index=index, dtype="float64")
    return pd.Series(np.nan, index=index, dtype="float64")


def pick_best_existing(df: pd.DataFrame, candidates, default=0.0) -> pd.Series:
    """
    候補列から最良の数値列を拾う。
    0よりも非0を優先する。
    """
    idx = df.index
    best = _safe_numeric(None, idx, default=np.nan, fillna=False)
    found = False

    for c in candidates:
        if c not in df.columns:
            continue

        cur = _safe_numeric(df[c], idx, default=np.nan, fillna=False)

        if not found:
            best = cur
            found = True
            continue

        try:
            fill_mask = best.isna() | best.fillna(0).eq(0)
            candidate_mask = cur.notna() & cur.fillna(0).ne(0)
            best = best.where(~(fill_mask & candidate_mask), cur)
            best = best.where(best.notna(), cur)
        except Exception:
            try:
                best = best.combine_first(cur)
            except Exception:
                pass

    if not found:
        return pd.Series(default, index=idx, dtype="float64")

    return best.fillna(default)


def pick_best_raw_slope(df: pd.DataFrame, default=0.0) -> pd.Series:
    return pick_best_existing(
        df,
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
        default=default,
    )


def pick_best_raw_mtf(df: pd.DataFrame, default=0.0) -> pd.Series:
    return pick_best_existing(
        df,
        [
            "mtf",
            "score_mtf",
            "mtf_alignment",
            "mtf_alignment_bonus",
            "mtf_bonus",
            "mtf_raw",
        ],
        default=default,
    )


def preserve_slope_mtf_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    slope / mtf 系列を相互補完する。
    既存の非0値を潰さない。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    try:
        slope_best = pick_best_raw_slope(out, default=np.nan)
        mtf_best = pick_best_raw_mtf(out, default=np.nan)

        for col in ("slope", "score_slope", "slope_atr_scaled"):
            if col not in out.columns:
                out[col] = slope_best
            else:
                cur = _safe_numeric(out[col], out.index, default=np.nan, fillna=False)
                fill_mask = cur.isna() | cur.fillna(0).eq(0)
                candidate_mask = slope_best.notna() & slope_best.fillna(0).ne(0)
                out[col] = cur.where(~(fill_mask & candidate_mask), slope_best)

        for col in ("mtf", "score_mtf"):
            if col not in out.columns:
                out[col] = mtf_best
            else:
                cur = _safe_numeric(out[col], out.index, default=np.nan, fillna=False)
                fill_mask = cur.isna() | cur.fillna(0).eq(0)
                candidate_mask = mtf_best.notna() & mtf_best.fillna(0).ne(0)
                out[col] = cur.where(~(fill_mask & candidate_mask), mtf_best)

    except Exception:
        logger.exception("[POST.FILTER] preserve slope/mtf failed")

    return out


# ============================================================
# date guard
# ============================================================

def drop_outside_allowed_dates(df: pd.DataFrame, stage: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    if "datetime" not in out.columns and "date" not in out.columns and "end_time" not in out.columns:
        logger.warning("[POST.FILTER] %s date guard skipped: datetime/date/end_time missing", stage)
        return out

    out = normalize_datetime_for_filter(out)

    if "datetime" not in out.columns:
        logger.warning("[POST.FILTER] %s date guard skipped: datetime unresolved", stage)
        return out

    dt_s = _safe_to_datetime_series(
        out["datetime"],
        base_df=out,
        col_name="datetime",
        allow_time_only=True,
    )

    valid_dt = dt_s.notna()
    if not valid_dt.any():
        logger.warning(
            "[POST.FILTER] %s date guard skipped: all datetime invalid rows=%s",
            stage,
            len(out),
        )
        return out

    actual_dates = extract_actual_dates_from_df(out)
    allowed_dates = actual_dates if actual_dates else allowed_post_dates_fallback()

    keep = dt_s.dt.date.isin(allowed_dates)
    keep = keep.fillna(False)

    before = len(out)
    removed = int((~keep).sum())

    if removed > 0:
        logger.warning(
            "[POST.FILTER] %s date guard removed=%d before=%d allowed=%s actual_dates=%s invalid_dt=%s",
            stage,
            removed,
            before,
            sorted(str(x) for x in allowed_dates),
            sorted(str(x) for x in actual_dates),
            int((~valid_dt).sum()),
        )

    out = out.loc[keep].copy().reset_index(drop=True)

    logger.info(
        "[POST.FILTER] %s date guard rows=%d -> %d allowed=%s actual_dates=%s",
        stage,
        before,
        len(out),
        sorted(str(x) for x in allowed_dates),
        sorted(str(x) for x in actual_dates),
    )

    return out


# ============================================================
# market / universe filter
# ============================================================

def apply_market_filter_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    try:
        df = df.copy()
        before = len(df)

        s = safe_symbol_series(df)
        if s is None:
            logger.warning("[POST.FILTER] market filter skipped: symbol missing")
            return df

        df["symbol"] = s.reindex(df.index)

        allowed = load_allowed_symbol_universe()
        if allowed:
            df = df[df["symbol"].astype(str).isin(allowed)]

        for market_col in ("market_type", "market"):
            if market_col in df.columns:
                try:
                    norm = df[market_col].map(normalize_market_text)
                    kept = df[norm.isin(_ALLOWED_MARKETS)]
                    if not kept.empty:
                        df = kept
                    break
                except Exception:
                    logger.debug("[POST.FILTER] market normalize failed col=%s", market_col, exc_info=True)

        if "symbolname" in df.columns:
            try:
                pat = "|".join(_EXCLUDE_NAME_KEYWORDS)
                mask = ~df["symbolname"].astype(str).str.contains(
                    pat,
                    case=False,
                    na=False,
                    regex=True,
                )
                df = df[mask]
            except Exception:
                logger.debug("[POST.FILTER] symbolname ETF filter failed", exc_info=True)

        after = len(df)
        if after != before:
            logger.info("[POST.FILTER] market/universe filter rows: %d -> %d", before, after)

        return df.reset_index(drop=True)

    except Exception:
        logger.exception("[POST.FILTER] market filter apply failed")
        return df


# ============================================================
# dead row filter
# ============================================================

def drop_dead_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    try:
        out = df.copy()
        out = preserve_slope_mtf_columns(out)

        open_s = pick_series(out, ["open"], default=np.nan, fillna=False)
        high_s = pick_series(out, ["high"], default=np.nan, fillna=False)
        low_s = pick_series(out, ["low"], default=np.nan, fillna=False)
        close_s = pick_series(out, ["close"], default=np.nan, fillna=False)

        slope_s = pick_best_raw_slope(out, default=0.0)
        mtf_s = pick_best_raw_mtf(out, default=0.0)
        macd_s = pick_series(out, ["macd"], default=0.0)
        signal_s = pick_series(out, ["signal"], default=0.0)
        volume_s = pick_series(out, ["trading_volume", "volume"], default=0.0)

        same_ohlc = (
            open_s.notna()
            & high_s.notna()
            & low_s.notna()
            & close_s.notna()
            & open_s.eq(high_s)
            & high_s.eq(low_s)
            & low_s.eq(close_s)
        )

        dead_ind = (
            slope_s.fillna(0).eq(0)
            & mtf_s.fillna(0).eq(0)
            & macd_s.fillna(0).eq(0)
            & signal_s.fillna(0).eq(0)
        )

        ultra_dead = same_ohlc & dead_ind & volume_s.fillna(0).le(0)

        before = len(out)
        out = out.loc[~ultra_dead].copy()
        after = len(out)

        if after != before:
            logger.info(
                "[POST.FILTER] dead rows removed: %d -> %d (removed=%d)",
                before,
                after,
                before - after,
            )

        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[POST.FILTER] dead row filter failed")
        return df


# ============================================================
# deduplicate
# ============================================================

def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    out = df.copy()
    out = preserve_slope_mtf_columns(out)

    if "datetime" not in out.columns:
        out["datetime"] = pd.Timestamp.now().floor("min")
    else:
        out = normalize_datetime_for_filter(out)

    sort_candidates = [
        "display_score",
        "final_score",
        "score",
        "score_total",
        "combined_score",
        "score_buy",
        "buy_score",
        "score_sell",
        "sell_score",
    ]

    sort_score = None
    for c in sort_candidates:
        if c in out.columns:
            sort_score = c
            break

    if sort_score is None:
        out["_dedup_score"] = 0.0
        sort_score = "_dedup_score"

    try:
        ret = (
            out.sort_values([sort_score, "datetime"], ascending=[False, False], kind="stable")
               .drop_duplicates(subset=["symbol"], keep="first")
               .drop(columns=["_dedup_score"], errors="ignore")
               .reset_index(drop=True)
        )
        return ret

    except Exception:
        logger.exception("[POST.FILTER] deduplicate failed")
        return out.drop(columns=["_dedup_score"], errors="ignore").reset_index(drop=True)


# ============================================================
# optional combined helper
# ============================================================

def apply_post_filters(
    df: pd.DataFrame,
    *,
    stage: str = "postprocess",
    apply_date_guard: bool = True,
    apply_market_filter: bool = True,
    drop_dead: bool = True,
    do_deduplicate: bool = False,
) -> pd.DataFrame:
    """
    必要に応じてまとめてfilterする補助関数。
    既存コードが個別関数を呼ぶ場合も壊さない。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    out = preserve_slope_mtf_columns(out)

    if apply_date_guard:
        out = drop_outside_allowed_dates(out, stage=stage)

    if out.empty:
        return out

    if apply_market_filter:
        out = apply_market_filter_df(out)

    if out.empty:
        return out

    if drop_dead:
        out = drop_dead_rows(out)

    if out.empty:
        return out

    if do_deduplicate:
        out = deduplicate(out)

    return out


__all__ = [
    "normalize_market_text",
    "safe_symbol_series",
    "load_allowed_symbol_universe",
    "pick_best_existing",
    "pick_best_raw_slope",
    "pick_best_raw_mtf",
    "preserve_slope_mtf_columns",
    "drop_outside_allowed_dates",
    "apply_market_filter_df",
    "drop_dead_rows",
    "deduplicate",
    "normalize_datetime_for_filter",
    "apply_post_filters",
]