# ============================================================
# trading/summary/resample.py
# Ver31-PRODUCTION-ULTRA-STABLE-RESAMPLE-ENGINE-ENDTIME-SAFE
# ------------------------------------------------------------
# ✔ Ver30 完全互換ベース
# ✔ end_time datetime parse warning 修正
# ✔ end_time が時刻のみの場合 date / datetime から日付補完
# ✔ pandas format="mixed" 非依存
# ✔ 価格0/負値を無効値として除外
# ✔ 壊れたOHLCバー除外
# ✔ symbolname / time_range 完全互換
# ✔ duplicate column guard
# ✔ duplicate row guard
# ✔ OHLC alias guard（双方向）
# ✔ datetime alias guard（拡張）
# ✔ dtype stabilization
# ✔ numeric sanitize
# ✔ volume NaN protection
# ✔ pandas crash protection
# ✔ large dataset stability
# ✔ groupby crash isolation
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# DUPLICATE COLUMN GUARD
# ============================================================

def _remove_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    if df.columns.duplicated().any():
        dup = df.columns[df.columns.duplicated()].tolist()
        logger.warning("[RESAMPLE] duplicate columns removed: %s", dup)
        df = df.loc[:, ~df.columns.duplicated()].copy()

    return df


# ============================================================
# OHLC ALIAS REPAIR
# ============================================================

def _repair_ohlc_alias(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    df = df.copy()

    alias_forward = {
        "open": "open_price",
        "high": "high_price",
        "low": "low_price",
        "close": "close_price",
    }

    alias_reverse = {
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
    }

    for src, dst in alias_forward.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]

    for src, dst in alias_reverse.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]

    return df


# ============================================================
# DATETIME ALIAS REPAIR
# ============================================================

def _repair_datetime_alias(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    if "end_time" in df.columns:
        return df

    alias = [
        "datetime",
        "timestamp",
        "time",
        "snapshot_time",
        "tick_time",
        "CurrentPriceTime",
    ]

    for c in alias:
        if c in df.columns:
            df = df.copy()
            df["end_time"] = df[c]
            logger.warning("[RESAMPLE] datetime alias used: %s -> end_time", c)
            return df

    return df


# ============================================================
# END_TIME SAFE PARSER
# ============================================================

def _coerce_end_time_safely(df: pd.DataFrame) -> pd.DataFrame:
    """
    end_time を warning なしで datetime 化する。

    対応:
      - 2026-04-20 09:45:00
      - 2026-04-20 09:45
      - 2026/04/20 09:45:00
      - 2026/04/20 09:45
      - 09:45
      - 09:45:00
      - pandas Timestamp / datetime

    end_time が時刻だけの場合:
      - date 列があれば date + end_time
      - datetime 列があれば datetime の日付 + end_time
      - start_time 列が日付付きなら start_time の日付 + end_time
      - どれも無ければ今日の日付 + end_time

    注意:
      pandas の format="mixed" は古い環境で使えない場合があるため使わない。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    if "end_time" not in df.columns:
        return df

    out = df.copy()

    raw = out["end_time"]

    # すでに datetime 型の場合はそのまま安全変換
    if pd.api.types.is_datetime64_any_dtype(raw):
        out["end_time"] = pd.to_datetime(raw, errors="coerce")
        try:
            out["end_time"] = out["end_time"].dt.tz_localize(None)
        except Exception:
            pass
        return out

    s = raw.astype(str).str.strip()
    s = s.replace(
        {
            "": None,
            "nan": None,
            "NaN": None,
            "None": None,
            "NaT": None,
            "<NA>": None,
        }
    )

    result = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns]")

    # --------------------------------------------------------
    # 1. 日付付き yyyy-mm-dd HH:MM(:SS)
    # --------------------------------------------------------
    date_dash_hms = s.str.match(
        r"^\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}:\d{2}$",
        na=False,
    )
    date_dash_hm = s.str.match(
        r"^\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}$",
        na=False,
    )

    if date_dash_hms.any():
        idx = date_dash_hms[date_dash_hms].index
        result.loc[idx] = pd.to_datetime(
            s.loc[idx],
            errors="coerce",
            format="%Y-%m-%d %H:%M:%S",
        )

    if date_dash_hm.any():
        idx = date_dash_hm[date_dash_hm].index
        result.loc[idx] = pd.to_datetime(
            s.loc[idx],
            errors="coerce",
            format="%Y-%m-%d %H:%M",
        )

    # --------------------------------------------------------
    # 2. 日付付き yyyy/mm/dd HH:MM(:SS)
    # --------------------------------------------------------
    date_slash_hms = s.str.match(
        r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2}$",
        na=False,
    )
    date_slash_hm = s.str.match(
        r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}$",
        na=False,
    )

    if date_slash_hms.any():
        idx = date_slash_hms[date_slash_hms].index
        result.loc[idx] = pd.to_datetime(
            s.loc[idx],
            errors="coerce",
            format="%Y/%m/%d %H:%M:%S",
        )

    if date_slash_hm.any():
        idx = date_slash_hm[date_slash_hm].index
        result.loc[idx] = pd.to_datetime(
            s.loc[idx],
            errors="coerce",
            format="%Y/%m/%d %H:%M",
        )

    # --------------------------------------------------------
    # 3. 時刻のみ HH:MM(:SS)
    # --------------------------------------------------------
    time_hms = s.str.match(
        r"^\d{1,2}:\d{2}:\d{2}$",
        na=False,
    )
    time_hm = s.str.match(
        r"^\d{1,2}:\d{2}$",
        na=False,
    )
    time_only = time_hms | time_hm

    if time_only.any():
        base_date = None

        if "date" in out.columns:
            try:
                base_date = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            except Exception:
                base_date = None

        if base_date is None and "datetime" in out.columns:
            try:
                base_date = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
            except Exception:
                base_date = None

        if base_date is None and "start_time" in out.columns:
            try:
                parsed_start = pd.to_datetime(out["start_time"], errors="coerce")
                if parsed_start.notna().any():
                    base_date = parsed_start.dt.strftime("%Y-%m-%d")
            except Exception:
                base_date = None

        if base_date is None:
            today = pd.Timestamp.now().strftime("%Y-%m-%d")
            base_date = pd.Series(today, index=out.index)

        # base_date が NaN の行は今日で補完
        try:
            today = pd.Timestamp.now().strftime("%Y-%m-%d")
            base_date = base_date.fillna(today).replace("NaT", today)
        except Exception:
            today = pd.Timestamp.now().strftime("%Y-%m-%d")
            base_date = pd.Series(today, index=out.index)

        combined = base_date.astype(str) + " " + s.astype(str)

        idx_hms = time_hms[time_hms].index
        if len(idx_hms) > 0:
            result.loc[idx_hms] = pd.to_datetime(
                combined.loc[idx_hms],
                errors="coerce",
                format="%Y-%m-%d %H:%M:%S",
            )

        idx_hm = time_hm[time_hm].index
        if len(idx_hm) > 0:
            result.loc[idx_hm] = pd.to_datetime(
                combined.loc[idx_hm],
                errors="coerce",
                format="%Y-%m-%d %H:%M",
            )

    # --------------------------------------------------------
    # 4. 最後の救済
    #    想定外フォーマットが少数混じった場合でも落とさない。
    #    warning はここで明示的に抑制する。
    # --------------------------------------------------------
    remaining = result.isna() & s.notna()
    if remaining.any():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            result.loc[remaining] = pd.to_datetime(
                s.loc[remaining],
                errors="coerce",
            )

    out["end_time"] = result

    try:
        out["end_time"] = out["end_time"].dt.tz_localize(None)
    except Exception:
        pass

    before = len(out)
    out = out.dropna(subset=["end_time"]).copy()
    removed = before - len(out)

    if removed > 0:
        logger.warning("[RESAMPLE] rows removed by invalid end_time: %d", removed)

    return out


# ============================================================
# DUPLICATE ROW GUARD
# ============================================================

def _drop_duplicate_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    if {"symbol", "end_time"}.issubset(df.columns):
        before = len(df)
        df = df.drop_duplicates(subset=["symbol", "end_time"], keep="last")
        removed = before - len(df)
        if removed > 0:
            logger.warning("[RESAMPLE] duplicate rows removed: %d", removed)

    return df


# ============================================================
# DTYPE STABILIZATION
# ============================================================

def _stabilize_dtype(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    df = df.copy()

    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str).str.strip()

    if "symbolname" in df.columns:
        df["symbolname"] = df["symbolname"].astype(str)

    return df


# ============================================================
# NUMERIC SANITIZE
# ============================================================

def _sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    df = df.copy()

    numeric_cols = [
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
    ]

    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").replace([np.inf, -np.inf], np.nan)

    if "volume" in df.columns:
        df["volume"] = df["volume"].fillna(0)

    return df


# ============================================================
# PRICE VALIDATION
# ============================================================

def _sanitize_price_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    価格 0 / 負値 は欠損扱いにする。
    volume は 0 を許容。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    df = df.copy()

    for c in ("open_price", "high_price", "low_price", "close_price"):
        if c in df.columns:
            s = pd.to_numeric(df[c], errors="coerce")
            s = s.replace([np.inf, -np.inf], np.nan)
            s = s.mask(s <= 0, np.nan)
            df[c] = s

    return df


def _coalesce_valid_ohlc_from_close(df: pd.DataFrame) -> pd.DataFrame:
    """
    close が有効で open/high/low の一部が欠けている場合だけ close で最小限補完。
    ただし close 自体が無効なら補完しない。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    df = df.copy()

    if "close_price" not in df.columns:
        return df

    close_s = pd.to_numeric(df["close_price"], errors="coerce")
    close_s = close_s.mask(close_s <= 0, np.nan)

    for c in ("open_price", "high_price", "low_price"):
        if c in df.columns:
            s = pd.to_numeric(df[c], errors="coerce")
            s = s.mask(s <= 0, np.nan)
            df[c] = s.combine_first(close_s)

    df["close_price"] = close_s
    return df


def _drop_price_dead_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    close が無効な行を除外。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    if "close_price" not in out.columns:
        return out

    before = len(out)
    close_s = pd.to_numeric(out["close_price"], errors="coerce")
    close_s = close_s.mask(close_s <= 0, np.nan)
    out["close_price"] = close_s
    out = out[out["close_price"].notna()].copy()
    removed = before - len(out)

    if removed > 0:
        logger.warning("[RESAMPLE] dead 1min rows removed by close_price invalid: %d", removed)

    return out


def _drop_invalid_resampled_bars(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    resample後の壊れたOHLCを除外。
    """
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    needed = {"open_price", "high_price", "low_price", "close_price"}
    if not needed.issubset(out.columns):
        return out

    for c in needed:
        out[c] = pd.to_numeric(out[c], errors="coerce").replace([np.inf, -np.inf], np.nan)

    valid = (
        out["open_price"].notna()
        & out["high_price"].notna()
        & out["low_price"].notna()
        & out["close_price"].notna()
        & (out["open_price"] > 0)
        & (out["high_price"] > 0)
        & (out["low_price"] > 0)
        & (out["close_price"] > 0)
        & (out["high_price"] >= out["low_price"])
        & (out["high_price"] >= out["open_price"])
        & (out["high_price"] >= out["close_price"])
        & (out["low_price"] <= out["open_price"])
        & (out["low_price"] <= out["close_price"])
    )

    before = len(out)
    bad = out.loc[~valid].copy()

    if not bad.empty:
        sample_cols = [
            c for c in [
                "symbol",
                "end_time",
                "open_price",
                "high_price",
                "low_price",
                "close_price",
                "volume",
            ]
            if c in bad.columns
        ]
        logger.warning(
            "[RESAMPLE] invalid %dmin OHLC bars removed=%d sample=\n%s",
            interval,
            len(bad),
            bad[sample_cols].head(20).to_string(index=False),
        )

    out = out.loc[valid].copy()
    removed = before - len(out)

    if removed > 0:
        logger.warning("[RESAMPLE] invalid %dmin bars removed: %d", interval, removed)

    return out


# ============================================================
# MAIN RESAMPLE ENGINE
# ============================================================

def resample_1min_to(df_1min: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df_1min is None or df_1min.empty:
        return pd.DataFrame()

    interval = int(interval)
    if interval not in (3, 5):
        raise ValueError(f"invalid interval={interval}")

    df = df_1min.copy()

    # --------------------------------------------------------
    # COLUMN SAFETY
    # --------------------------------------------------------
    df = _remove_duplicate_columns(df)
    df = _repair_ohlc_alias(df)
    df = _repair_datetime_alias(df)

    # --------------------------------------------------------
    # TIME COLUMN
    # --------------------------------------------------------
    if "end_time" not in df.columns:
        raise KeyError("resample missing end_time/datetime")

    # Ver31:
    # warning が出る pd.to_datetime(df["end_time"], errors="coerce") は使わない
    df = _coerce_end_time_safely(df)

    if df.empty:
        return pd.DataFrame()

    # --------------------------------------------------------
    # REQUIRED COLUMNS
    # --------------------------------------------------------
    required = {
        "symbol",
        "symbolname",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
    }

    if not required.issubset(df.columns):
        missing = required - set(df.columns)
        raise KeyError(f"resample missing columns: {missing}")

    # --------------------------------------------------------
    # TYPE NORMALIZATION
    # --------------------------------------------------------
    df = _stabilize_dtype(df)
    df = _sanitize_numeric(df)
    df = _sanitize_price_columns(df)
    df = _coalesce_valid_ohlc_from_close(df)
    df = _drop_price_dead_rows(df)

    if df.empty:
        logger.warning("[RESAMPLE] all rows dropped before resample interval=%s", interval)
        return pd.DataFrame()

    # --------------------------------------------------------
    # FLOOR TIME
    # --------------------------------------------------------
    df["t_floor"] = df["end_time"].dt.floor(f"{interval}min")

    # --------------------------------------------------------
    # GROUP RESAMPLE
    # --------------------------------------------------------
    out_frames: list[pd.DataFrame] = []

    agg = {
        "open_price": "first",
        "high_price": "max",
        "low_price": "min",
        "close_price": "last",
        "volume": "sum",
        "symbolname": "first",
    }

    for symbol, g in df.groupby("symbol", sort=False):
        if g.empty:
            continue

        g = g.sort_values("end_time").reset_index(drop=True).copy()

        try:
            r = g.groupby("t_floor", as_index=False).agg(agg)
        except Exception:
            logger.exception("[RESAMPLE] groupby failed symbol=%s", symbol)
            continue

        if r.empty:
            continue

        r["symbol"] = symbol
        r["start_time"] = r["t_floor"]
        r["end_time"] = r["t_floor"] + pd.Timedelta(minutes=interval)
        r["datetime"] = r["end_time"]
        r["interval"] = interval
        r["interval_name"] = f"{interval}min"
        r["date"] = r["end_time"].dt.normalize().dt.date
        r["time"] = r["end_time"].dt.time
        r["time_range"] = (
            r["start_time"].dt.strftime("%H:%M")
            + " - "
            + r["end_time"].dt.strftime("%H:%M")
        )

        out_frames.append(r)

    if not out_frames:
        return pd.DataFrame()

    out = (
        pd.concat(out_frames, ignore_index=True)
        .sort_values(["symbol", "end_time"], kind="stable")
        .reset_index(drop=True)
    )

    out = _drop_duplicate_rows(out)
    out = _drop_invalid_resampled_bars(out, interval)

    if out.empty:
        logger.warning("[RESAMPLE] all bars dropped after validation interval=%s", interval)
        return out

    # --------------------------------------------------------
    # backward-compatible aliases
    # --------------------------------------------------------
    out["open"] = out["open_price"]
    out["high"] = out["high_price"]
    out["low"] = out["low_price"]
    out["close"] = out["close_price"]

    logger.info(
        "[RESAMPLE] %dmin rows=%d symbols=%d",
        interval,
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
    )

    return out


def resample_summary_df(df: pd.DataFrame, interval: int = 3, **kwargs) -> pd.DataFrame:
    """
    backward-compatible wrapper
    rebuilders.py など旧呼び出し側との互換用
    """
    return resample_1min_to(df_1min=df, interval=int(interval))


__all__ = [
    "resample_1min_to",
    "resample_summary_df",
]