# ============================================================
# File   : trading/ranking/summary/resample.py
# Ver    : PRODUCTION-STABLE-REV2.0-RANKING-SUMMARY-RESAMPLE
#          -SNAPSHOT-TO-PSEUDO-OHLCV
#          -VOLUME-DIFF-FROM-TRADING-VOLUME
#          -PUSH-COMPAT-COLUMNS
#          -3MIN-5MIN-RESAMPLE-SAFE
# ------------------------------------------------------------
# 【概要】
#   ranking 1min データを PUSH summary 互換の
#   ranking_summary base df に変換する。
#
# 【重要】
#   - ranking_snapshot_1min 由来の close/current_price から
#     疑似OHLCを作る
#
#       open = close
#       high = close
#       low  = close
#
#   - trading_volume が累積出来高の場合に備えて
#
#       volume = 今回 trading_volume - 前回 trading_volume
#
#     を作る
#
#   - 3min / 5min では
#
#       open   = first
#       high   = max
#       low    = min
#       close  = last
#       volume = sum
#
#     で集約する
#
#   - trading_volume は累積値なので sum しない
#     3min/5min では last を採用する
#
# 【注意】
#   - このファイルは「DF生成」まで。
#   - indicator計算 / scoring / DB保存は呼び出し元 runner/bootstrap 側で行う。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, Optional, Any

import numpy as np
import pandas as pd

from trading.ranking.summary.constants import SUPPORTED_INTERVALS
from trading.ranking.summary.loader import load_ranking_snapshot_1min
from trading.ranking.summary.utils import (
    default_yahoo_db_path,
    log_df_profile,
    normalize_trade_date,
    path_exists,
)
from trading.ranking.summary.yahoo_fill import apply_yahoo_fill

logger = logging.getLogger(__name__)


# ============================================================
# helpers
# ============================================================

def _to_datetime_naive(s: pd.Series) -> pd.Series:
    out = pd.to_datetime(s, errors="coerce")

    try:
        out = out.dt.tz_localize(None)
    except Exception:
        try:
            out = out.dt.tz_convert(None)
        except Exception:
            pass

    return out


def _normalize_symbol(s: pd.Series) -> pd.Series:
    return (
        s.astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )


def _first_existing(df: pd.DataFrame, names: list[str]) -> Optional[str]:
    for c in names:
        if c in df.columns:
            return c
    return None


def _ensure_col_from_alias(
    df: pd.DataFrame,
    target: str,
    aliases: list[str],
    default=None,
) -> None:
    if target in df.columns:
        return

    src = _first_existing(df, aliases)
    if src is not None:
        df[target] = df[src]
    else:
        df[target] = default


def _fill_missing_from_alias(
    df: pd.DataFrame,
    target: str,
    aliases: list[str],
) -> None:
    if target not in df.columns:
        df[target] = np.nan

    for src in aliases:
        if src not in df.columns or src == target:
            continue
        try:
            df[target] = df[target].where(df[target].notna(), df[src])
        except Exception:
            pass


def _safe_numeric(df: pd.DataFrame, cols: list[str]) -> None:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")


def _add_date_time_cols(df: pd.DataFrame) -> None:
    if "datetime" not in df.columns:
        return

    dts = pd.to_datetime(df["datetime"], errors="coerce")

    df["date"] = dts.dt.strftime("%Y-%m-%d")
    df["time"] = dts.dt.strftime("%H:%M:%S")

    if "time_range" not in df.columns:
        df["time_range"] = df["time"]


def _ensure_push_compatible_price_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking snapshot / yahoo fill 後のDFを PUSH summary 互換の価格列に揃える。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()

    # symbol
    if "symbol" in x.columns:
        x["symbol"] = _normalize_symbol(x["symbol"])

    # datetime
    if "datetime" in x.columns:
        x["datetime"] = _to_datetime_naive(x["datetime"])

    # close/current_price alias
    _ensure_col_from_alias(
        x,
        "current_price",
        ["close", "close_price", "price", "last_price", "CurrentPrice"],
        np.nan,
    )
    _ensure_col_from_alias(
        x,
        "close",
        ["close_price", "current_price", "price", "last_price", "CurrentPrice"],
        np.nan,
    )

    _fill_missing_from_alias(x, "close", ["close_price", "current_price", "price", "last_price"])
    _fill_missing_from_alias(x, "current_price", ["close", "close_price"])
    _ensure_col_from_alias(x, "close_price", ["close", "current_price"], np.nan)
    _fill_missing_from_alias(x, "close_price", ["close", "current_price"])

    # 疑似OHLC
    _ensure_col_from_alias(x, "open", ["open_price", "close", "current_price"], np.nan)
    _ensure_col_from_alias(x, "high", ["high_price", "close", "current_price"], np.nan)
    _ensure_col_from_alias(x, "low", ["low_price", "close", "current_price"], np.nan)

    _fill_missing_from_alias(x, "open", ["close", "close_price", "current_price"])
    _fill_missing_from_alias(x, "high", ["close", "close_price", "current_price"])
    _fill_missing_from_alias(x, "low", ["close", "close_price", "current_price"])

    # price aliases
    _ensure_col_from_alias(x, "open_price", ["open", "close", "current_price"], np.nan)
    _ensure_col_from_alias(x, "high_price", ["high", "close", "current_price"], np.nan)
    _ensure_col_from_alias(x, "low_price", ["low", "close", "current_price"], np.nan)

    _fill_missing_from_alias(x, "open_price", ["open", "close", "current_price"])
    _fill_missing_from_alias(x, "high_price", ["high", "close", "current_price"])
    _fill_missing_from_alias(x, "low_price", ["low", "close", "current_price"])

    _safe_numeric(
        x,
        [
            "open",
            "high",
            "low",
            "close",
            "current_price",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
        ],
    )

    return x


def _ensure_volume_1min(df: pd.DataFrame) -> pd.DataFrame:
    """
    1min DF に volume を作る。

    優先:
      1. 既に volume があり、非NULLが多い場合はそれを使う
      2. trading_volume があれば symbol ごとの差分から作る
      3. なければ 0
    """
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()
    x = x.sort_values(["symbol", "datetime"], kind="mergesort").reset_index(drop=True)

    if "volume" in x.columns:
        vol = pd.to_numeric(x["volume"], errors="coerce")
        non_null = int(vol.notna().sum())
        non_zero = int((vol.fillna(0) > 0).sum())

        # 既にまともな1分出来高があるなら尊重
        if non_null > 0 and non_zero > 0:
            x["volume"] = vol.fillna(0).clip(lower=0)
            return x

    if "trading_volume" in x.columns:
        tv = pd.to_numeric(x["trading_volume"], errors="coerce")
        diff = tv.groupby(x["symbol"]).diff()
        diff = diff.fillna(0)
        diff = diff.where(diff >= 0, 0)
        x["volume"] = diff.astype(float)
    else:
        x["volume"] = 0.0

    return x


def _ensure_summary_base_columns(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    """
    保存・indicator・announce に必要な土台列を作る。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()

    if "datetime" in x.columns:
        x["datetime"] = _to_datetime_naive(x["datetime"])

    _add_date_time_cols(x)

    x["interval"] = int(interval)

    if "source" not in x.columns:
        if int(interval) == 1:
            x["source"] = "ranking_summary_snapshot_1m"
        else:
            x["source"] = f"ranking_summary_resample_{int(interval)}m"

    if "price_source" not in x.columns:
        x["price_source"] = "ranking_snapshot"

    if "mode" not in x.columns:
        x["mode"] = "summary"

    # indicator/scoring の土台列
    default_nan_cols = [
        "ma5",
        "ma25",
        "ma75",
        "rsi",
        "rsi_slope",
        "macd",
        "signal",
        "macd_signal",
        "macd_hist",
        "macd_hist_slope",
        "slope",
        "slope_atr_scaled",
        "mtf",
        "score_mtf",
        "mtf_score",
        "score",
        "score_buy",
        "score_sell",
        "score_total",
        "final_score",
        "display_score",
        "disp_score",
        "score_slope",
        "base",
        "trend",
        "mom",
        "vel",
        "pen",
    ]

    for c in default_nan_cols:
        if c not in x.columns:
            x[c] = np.nan

    # flags
    flag_cols = [
        "flag_macd_cross",
        "flag_macd_hist_expand",
        "flag_rsi_rebound",
        "flag_rsi_midline_cross",
        "flag_macd_dc",
        "flag_macd_hist_contract",
        "flag_rsi_falling",
        "flag_rsi_overbought_70",
    ]
    for c in flag_cols:
        if c not in x.columns:
            x[c] = 0

    # signal alias
    if "signal" not in x.columns and "macd_signal" in x.columns:
        x["signal"] = x["macd_signal"]
    if "macd_signal" not in x.columns and "signal" in x.columns:
        x["macd_signal"] = x["signal"]

    _fill_missing_from_alias(x, "signal", ["macd_signal"])
    _fill_missing_from_alias(x, "macd_signal", ["signal"])

    # score alias
    _fill_missing_from_alias(x, "display_score", ["disp_score", "final_score", "score_total", "score"])
    _fill_missing_from_alias(x, "disp_score", ["display_score", "final_score", "score_total", "score"])
    _fill_missing_from_alias(x, "final_score", ["score_total", "display_score", "score"])
    _fill_missing_from_alias(x, "score_total", ["final_score", "display_score", "score"])

    # hist aliases
    if "best_rank" not in x.columns:
        if "best_rank_position" in x.columns:
            x["best_rank"] = x["best_rank_position"]
        elif "rank" in x.columns:
            x["best_rank"] = x["rank"]
        else:
            x["best_rank"] = np.nan

    if "hist" not in x.columns:
        if "hit_count" in x.columns:
            x["hist"] = x["hit_count"]
        else:
            x["hist"] = 1

    if "hit_count" not in x.columns:
        x["hit_count"] = x["hist"]

    return x


def _dedupe_1min_symbol_datetime(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking snapshot は同一 symbol/datetime に複数ランキング種別がある。
    summary は symbol/datetime で1行にする。

    方針:
      - rank が一番良い行を代表にする
      - best_rank は同一時刻の最小rank
      - hist/hit_count は同一時刻で出現したランキング種別数
    """
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()

    if "rank" not in x.columns:
        x["rank"] = np.nan

    x["rank_for_sort"] = pd.to_numeric(x["rank"], errors="coerce").fillna(999999)

    key = ["symbol", "datetime"]

    agg = (
        x.groupby(key, as_index=False)
        .agg(
            best_rank=("rank_for_sort", "min"),
            hit_count=("rank_for_sort", "count"),
        )
    )

    rep = (
        x.sort_values(["symbol", "datetime", "rank_for_sort"], kind="mergesort")
        .drop_duplicates(key, keep="first")
        .drop(columns=["rank_for_sort"], errors="ignore")
    )

    out = rep.merge(agg, on=key, how="left", suffixes=("", "_agg"))

    out["hist"] = out["hit_count"]

    return out.sort_values(["symbol", "datetime"], kind="mergesort").reset_index(drop=True)


# ============================================================
# resample
# ============================================================

def resample_symbol_frame(
    g: pd.DataFrame,
    *,
    interval: int,
) -> pd.DataFrame:
    if g is None or g.empty:
        return pd.DataFrame()

    interval = int(interval)

    x = g.copy()
    x["datetime"] = _to_datetime_naive(x["datetime"])
    x = x.dropna(subset=["datetime"])
    x = x.sort_values("datetime", kind="mergesort")

    if x.empty:
        return pd.DataFrame()

    x = _ensure_push_compatible_price_columns(x)
    x = _ensure_summary_base_columns(x, interval=1)

    x = x.set_index("datetime")

    agg: dict[str, Any] = {}

    # OHLCV
    agg["open"] = ("open", "first")
    agg["high"] = ("high", "max")
    agg["low"] = ("low", "min")
    agg["close"] = ("close", "last")

    if "volume" in x.columns:
        agg["volume"] = ("volume", "sum")
    else:
        x["volume"] = 0.0
        agg["volume"] = ("volume", "sum")

    # price aliases
    agg["current_price"] = ("close", "last")
    agg["open_price"] = ("open", "first")
    agg["high_price"] = ("high", "max")
    agg["low_price"] = ("low", "min")
    agg["close_price"] = ("close", "last")

    # trading_volume は累積値想定なので last
    if "trading_volume" in x.columns:
        agg["trading_volume"] = ("trading_volume", "last")

    # 加算系
    for c in ["turnover", "trading_value", "tick_count"]:
        if c in x.columns:
            agg[c] = (c, "sum")

    # rank系
    if "rank" in x.columns:
        agg["rank"] = ("rank", "last")
    if "best_rank" in x.columns:
        agg["best_rank"] = ("best_rank", "min")
    if "hit_count" in x.columns:
        agg["hit_count"] = ("hit_count", "sum")
    if "hist" in x.columns:
        agg["hist"] = ("hist", "sum")

    latest_cols = [
        "symbolname",
        "rank_position",
        "best_rank_position",
        "ranking_type",
        "rank_type",
        "category",
        "market",
        "market_type",
        "change_percentage",
        "volume_speed",
        "rank_strength",
        "rank_persistence",
        "rank_delta",
        "price_delta_1m",
        "volume_delta_1m",
        "volume_spike",
        "minute_of_day",
        "source",
        "price_source",
        "mode",
    ]

    for c in latest_cols:
        if c in x.columns:
            agg[c] = (c, "last")

    try:
        out = (
            x.resample(
                f"{interval}min",
                origin="start_day",
                label="right",
                closed="right",
            )
            .agg(**agg)
            .dropna(subset=["close"])
            .reset_index()
        )

        if out.empty:
            return pd.DataFrame()

        out["symbol"] = str(g["symbol"].iloc[0]).strip()
        out["interval"] = interval

        # high/low 補正
        for c in ["open", "high", "low", "close"]:
            out[c] = pd.to_numeric(out[c], errors="coerce")

        out["high"] = out["high"].where(
            out["high"].notna(),
            out[["open", "close"]].max(axis=1),
        )
        out["low"] = out["low"].where(
            out["low"].notna(),
            out[["open", "close"]].min(axis=1),
        )

        out["open_price"] = out["open"]
        out["high_price"] = out["high"]
        out["low_price"] = out["low"]
        out["close_price"] = out["close"]
        out["current_price"] = out["close"]

        out["source"] = f"ranking_summary_resample_{interval}m"
        out["price_source"] = "ranking_snapshot"
        out["mode"] = "summary"

        out = _ensure_summary_base_columns(out, interval=interval)

        return out.reset_index(drop=True)

    except Exception:
        logger.exception(
            "[RANKING SUMMARY RESAMPLE] resample failed symbol=%s interval=%s",
            g["symbol"].iloc[0] if "symbol" in g.columns and not g.empty else None,
            interval,
        )
        return pd.DataFrame()


def build_ranking_summary_base_df(
    *,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    interval: int = 1,
    lookback_minutes: int = 240,
    symbols: Optional[Iterable[str]] = None,
    ranking_db_path: Optional[str] = None,
    yahoo_db_path: Optional[str] = None,
    use_yahoo_fill: bool = True,
) -> pd.DataFrame:
    """
    ranking_snapshot_1min から interval 指定の ranking summary base df を作る。

    interval=1:
      - snapshotから1分疑似OHLCVを作る

    interval=3/5:
      - 1分疑似OHLCVからresampleする
    """
    interval = int(interval)

    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(f"unsupported interval: {interval}")

    d = normalize_trade_date(trade_date)

    if yahoo_db_path is None:
        yp = default_yahoo_db_path(d)
        yahoo_db_path = yp if path_exists(yp) else None

    one = load_ranking_snapshot_1min(
        trade_date=d,
        lookback_minutes=lookback_minutes,
        symbols=symbols,
        ranking_db_path=ranking_db_path,
    )

    if one is None or one.empty:
        logger.warning(
            "[RANKING SUMMARY RESAMPLE] 1min snapshot load empty interval=%s",
            interval,
        )
        return pd.DataFrame()

    one = one.copy()

    if "datetime" not in one.columns:
        logger.error(
            "[RANKING SUMMARY RESAMPLE] snapshot has no datetime columns=%s",
            list(one.columns),
        )
        return pd.DataFrame()

    if "symbol" not in one.columns:
        logger.error(
            "[RANKING SUMMARY RESAMPLE] snapshot has no symbol columns=%s",
            list(one.columns),
        )
        return pd.DataFrame()

    one["datetime"] = _to_datetime_naive(one["datetime"])
    one["symbol"] = _normalize_symbol(one["symbol"])

    one = one.dropna(subset=["datetime"])
    one = one[one["symbol"] != ""]

    if one.empty:
        logger.warning("[RANKING SUMMARY RESAMPLE] snapshot empty after key normalize")
        return pd.DataFrame()

    # Yahoo補完
    one = apply_yahoo_fill(
        one,
        yahoo_db_path=yahoo_db_path,
        symbols=symbols,
        use_yahoo_fill=use_yahoo_fill,
    )

    if one is None or one.empty:
        logger.warning(
            "[RANKING SUMMARY RESAMPLE] empty after yahoo fill interval=%s",
            interval,
        )
        return pd.DataFrame()

    one = _ensure_push_compatible_price_columns(one)

    one = one.dropna(subset=["symbol", "datetime", "close"])
    one = one[one["symbol"] != ""]

    if one.empty:
        logger.warning(
            "[RANKING SUMMARY RESAMPLE] empty after price normalize interval=%s",
            interval,
        )
        return pd.DataFrame()

    # 同一 symbol/datetime を1行に集約
    one = _dedupe_1min_symbol_datetime(one)

    # 1分出来高を作る
    one = _ensure_volume_1min(one)

    # 1分の土台列
    one = _ensure_summary_base_columns(one, interval=1)

    # 1分はここで返す
    if interval == 1:
        x = one.copy()
        x["interval"] = 1
        x["source"] = "ranking_summary_snapshot_1m"
        x["price_source"] = "ranking_snapshot"
        x["mode"] = "summary"

        x = x.sort_values(
            ["symbol", "datetime"],
            ascending=[True, True],
            kind="mergesort",
        ).reset_index(drop=True)

        log_df_profile("base-1min", x)

        logger.info(
            "[RANKING SUMMARY RESAMPLE] built base 1min rows=%s symbols=%s dt_min=%s dt_max=%s volume_nonzero=%s",
            len(x),
            x["symbol"].nunique() if "symbol" in x.columns else 0,
            x["datetime"].min() if "datetime" in x.columns else None,
            x["datetime"].max() if "datetime" in x.columns else None,
            int((pd.to_numeric(x.get("volume", 0), errors="coerce").fillna(0) > 0).sum()),
        )

        return x.reset_index(drop=True)

    frames: list[pd.DataFrame] = []

    for symbol, g in one.groupby("symbol", sort=False):
        r = resample_symbol_frame(g, interval=interval)
        if r is not None and not r.empty:
            frames.append(r)

    if not frames:
        logger.warning(
            "[RANKING SUMMARY RESAMPLE] resample produced no frames interval=%s source_rows=%s symbols=%s",
            interval,
            len(one),
            one["symbol"].nunique() if "symbol" in one.columns else 0,
        )
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)

    out = out.sort_values(
        ["symbol", "datetime"],
        ascending=[True, True],
        kind="mergesort",
    ).reset_index(drop=True)

    out = _ensure_summary_base_columns(out, interval=interval)

    log_df_profile(f"base-{interval}min", out)

    logger.info(
        "[RANKING SUMMARY RESAMPLE] built base interval=%s rows=%s symbols=%s dt_min=%s dt_max=%s volume_nonzero=%s",
        interval,
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        out["datetime"].min() if "datetime" in out.columns else None,
        out["datetime"].max() if "datetime" in out.columns else None,
        int((pd.to_numeric(out.get("volume", 0), errors="coerce").fillna(0) > 0).sum()),
    )

    return out.reset_index(drop=True)