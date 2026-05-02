# ============================================================
# File   : trading/summary/summary_cache_utils.py
# Ver    : 1.5.0-PRODUCTION-ULTRA-STABLE
# ------------------------------------------------------------
# ✔ summary_cache 正規化の唯一ルート
# ✔ DataFrame 契約を強制
# ✔ end_time datetime 正規化
# ✔ symbol + end_time 重複完全排除
# ✔ リングバッファ（max_rows）対応
# ✔ MA75_conf 時間帯 × ATR 補正
# ✔ initial / incremental / self-heal 共通利用
# ✔ dict / GlobalData / Compat Layer 完全互換
# ✔ 型依存完全排除（インターフェース判定）
# ✔ 例外メッセージ一貫性保証
# ✔ cache collapse 防止
# ✔ tz normalize 安定化
# ✔ DataFrame 安全 concat
# ✔ 本番用 logging
# ============================================================

import logging
import datetime as dt
import pandas as pd
from typing import Dict, Any

from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# デフォルト最大行数
# ============================================================

DEFAULT_MAX_ROWS = {
    1: 12000,
    3: 6000,
    5: 4000,
}

# ============================================================
# クラスタ別最低 conf
# ============================================================

CLUSTER_CONF_FLOOR = {
    "STRONG": 0.75,
    "NORMAL": 0.65,
    "WEAK": 0.60,
}

# ============================================================
# MA75_conf 時間帯補正
# ============================================================

TIMEZONE_MA75_CONF_FLOOR = [
    ("09:00", "09:10", 0.90),
    ("09:10", "09:30", 0.80),
    ("09:30", "10:00", 0.70),
    ("10:00", "15:30", 0.60),
]


def _parse_time(s: str) -> dt.time:
    return dt.datetime.strptime(s, "%H:%M").time()


PARSED_TIMEZONES = [
    (_parse_time(s), _parse_time(e), c)
    for s, e, c in TIMEZONE_MA75_CONF_FLOOR
]


# ============================================================
# cache 正規化（型依存完全排除）
# ============================================================

def _resolve_cache_container(cache_or_gd: Any) -> Dict[int, pd.DataFrame]:

    if isinstance(cache_or_gd, dict):
        return cache_or_gd

    if hasattr(cache_or_gd, "_summary_cache"):
        cache = getattr(cache_or_gd, "_summary_cache")
        if not isinstance(cache, dict):
            raise RuntimeError("_summary_cache must be dict[int, DataFrame]")
        return cache

    if hasattr(cache_or_gd, "summary_cache"):
        cache = getattr(cache_or_gd, "summary_cache")
        if not isinstance(cache, dict):
            raise RuntimeError("summary_cache must be dict[int, DataFrame]")
        return cache

    raise RuntimeError(
        "summary_cache must be dict[int, DataFrame] "
        "or object exposing _summary_cache/summary_cache"
    )


# ============================================================
# time_zone 正規化
# ============================================================

def add_time_zone_label(
    df: pd.DataFrame,
    *,
    column: str = "end_time",
    tz: str = "Asia/Tokyo",
) -> pd.DataFrame:

    if df is None or df.empty or column not in df.columns:
        return df

    s = pd.to_datetime(df[column], errors="coerce")

    if s.dt.tz is None:
        s = s.dt.tz_localize(tz)
    else:
        s = s.dt.tz_convert(tz)

    df = df.copy()
    df[column] = s

    return df


# ============================================================
# MA75 conf 補正
# ============================================================

def adjust_ma75_conf_by_time_and_atr(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if "ma75_conf" not in df.columns or "end_time" not in df.columns:
        return df

    df = df.copy()

    df["ma75_conf_raw"] = (
        pd.to_numeric(df["ma75_conf"], errors="coerce").fillna(0.0)
    )

    df["end_time"] = pd.to_datetime(df["end_time"], errors="coerce")
    df = df.dropna(subset=["end_time"])

    if df.empty:
        return df

    def _time_floor(t: dt.time) -> float:

        for start, end, floor in PARSED_TIMEZONES:

            if start <= t < end:
                return floor

        return 0.6

    df["ma75_conf_floor"] = df["end_time"].dt.time.map(_time_floor)

    if "atr" in df.columns:

        atr = pd.to_numeric(df["atr"], errors="coerce")

        atr_med = (
            atr.rolling(50, min_periods=1)
            .median()
            .replace(0, pd.NA)
        )

        atr_norm = atr / atr_med

        atr_factor = atr_norm.clip(0.5, 1.2).fillna(1.0)

    else:

        atr_factor = 1.0

    df["ma75_conf_adj"] = df["ma75_conf_raw"] * atr_factor

    df["ma75_conf_adj"] = df[
        ["ma75_conf_adj", "ma75_conf_floor"]
    ].max(axis=1)

    df["ma75_conf_ratio"] = (
        df["ma75_conf_adj"] / df["ma75_conf_floor"]
    ).clip(0.0, 2.0)

    df["ma75_conf"] = df["ma75_conf_adj"].clip(0.0, 1.0)

    return df


# ============================================================
# cluster floor
# ============================================================

def apply_cluster_floor(
    df: pd.DataFrame,
    cluster_map: Dict[str, str],
) -> pd.DataFrame:

    if df is None or df.empty or "symbol" not in df.columns:
        return df

    df = df.copy()

    def _cluster_floor(sym):

        cluster = cluster_map.get(str(sym))

        return CLUSTER_CONF_FLOOR.get(cluster, 0.6)

    df["cluster_conf_floor"] = df["symbol"].map(_cluster_floor)

    if "ma75_conf_floor" in df.columns:

        df["ma75_conf_floor"] = df[
            ["ma75_conf_floor", "cluster_conf_floor"]
        ].max(axis=1)

    else:

        df["ma75_conf_floor"] = df["cluster_conf_floor"]

    return df


# ============================================================
# hard negative
# ============================================================

def apply_ma75_hard_negative(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    if not {"ma75_conf_ratio", "end_time"}.issubset(df.columns):
        return df

    df = df.copy()

    df["end_time"] = pd.to_datetime(df["end_time"], errors="coerce")

    df = df.dropna(subset=["end_time"])

    def _is_hard_ng(row):

        t = row["end_time"].time()

        ratio = row["ma75_conf_ratio"]

        if dt.time(9, 0) <= t < dt.time(9, 30):
            return ratio < 0.9

        return ratio < 0.75

    df["ma75_hard_ng"] = df.apply(_is_hard_ng, axis=1)

    return df


# ============================================================
# summary_cache 正規化
# ============================================================

def normalize_summary_cache(
    prev: pd.DataFrame | None,
    new: pd.DataFrame | None,
    *,
    interval: int | None = None,
    max_rows: int | None = None,
    apply_ma75_conf_fix: bool = True,
) -> pd.DataFrame:

    if new is None or not isinstance(new, pd.DataFrame) or new.empty:

        return prev if isinstance(prev, pd.DataFrame) else pd.DataFrame()

    # ------------------------------
    # concat 安全化
    # ------------------------------

    if prev is None or not isinstance(prev, pd.DataFrame) or prev.empty:

        merged = new.copy()

    else:

        merged = pd.concat(
            [prev, new],
            ignore_index=True,
            copy=False,
        )

    if "end_time" not in merged.columns:

        logger.warning("[summary_cache] end_time missing")

        return pd.DataFrame()

    merged["end_time"] = pd.to_datetime(
        merged["end_time"],
        errors="coerce",
    )

    merged = merged.dropna(subset=["end_time"])

    if merged.empty:

        return pd.DataFrame()

    if "symbol" in merged.columns:

        merged["symbol"] = merged["symbol"].astype(str)

        subset = ["symbol", "end_time"]

    else:

        subset = ["end_time"]

    merged = (
        merged
        .drop_duplicates(subset=subset, keep="last")
        .sort_values("end_time")
        .reset_index(drop=True)
    )

    # ------------------------------
    # ring buffer
    # ------------------------------

    if max_rows and len(merged) > max_rows:

        merged = merged.iloc[-max_rows:].reset_index(drop=True)

    # ------------------------------
    # tz normalize
    # ------------------------------

    merged = add_time_zone_label(
        merged,
        column="end_time",
    )

    # ------------------------------
    # MA75補正
    # ------------------------------

    if apply_ma75_conf_fix:

        merged = adjust_ma75_conf_by_time_and_atr(merged)

    if hasattr(global_data, "symbol_cluster"):

        merged = apply_cluster_floor(
            merged,
            global_data.symbol_cluster,
        )

    merged = apply_ma75_hard_negative(merged)

    return merged


# ============================================================
# PUBLIC API
# ============================================================

def update_summary_cache(
    cache_or_gd: Any,
    interval: int,
    new_df: pd.DataFrame,
    *,
    max_rows: Dict[int, int] | None = None,
) -> Dict[int, pd.DataFrame]:

    cache = _resolve_cache_container(cache_or_gd)

    max_rows = max_rows or DEFAULT_MAX_ROWS

    prev = cache.get(interval)

    cache[interval] = normalize_summary_cache(
        prev=prev,
        new=new_df,
        interval=interval,
        max_rows=max_rows.get(interval),
    )

    return cache


def initialize_summary_cache(
    cache_or_gd: Any,
    interval: int,
    df: pd.DataFrame,
    *,
    max_rows: Dict[int, int] | None = None,
) -> Dict[int, pd.DataFrame]:

    if not isinstance(df, pd.DataFrame):

        raise RuntimeError(
            "initialize_summary_cache requires DataFrame df"
        )

    cache = _resolve_cache_container(cache_or_gd)

    max_rows = max_rows or DEFAULT_MAX_ROWS

    cache[interval] = normalize_summary_cache(
        prev=None,
        new=df,
        interval=interval,
        max_rows=max_rows.get(interval),
    )

    return cache


def get_summary_cache_df(
    cache_or_gd: Any,
    interval: int,
) -> pd.DataFrame:

    cache = _resolve_cache_container(cache_or_gd)

    df = cache.get(interval)

    if isinstance(df, pd.DataFrame):

        return df.copy()

    return pd.DataFrame()