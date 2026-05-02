# ============================================================
# File   : trading/ranking/summary/persistence/normalizer.py
# Ver    : PRODUCTION-STABLE-REV3.1
# ------------------------------------------------------------
# 【概要】
#   ranking_summary 保存前 DataFrame 正規化。
#
# 【責務】
#   - alias 補完
#   - OHLCV 補完
#   - signal / score / mtf 補完
#   - datetime 正規化
#   - ranking snapshot 専用特徴量生成
#   - save columns 順に整列
#
# REV3.1:
#   ✔ price_source 空欄を ranking_snapshot で補完
#   ✔ source 空欄を ranking で補完
#   ✔ close/current_price/close_price の補完順を強化
#   ✔ prev_close / prev_rank / prev_volume は保存列に混入させない
#   ✔ symbol の .0 除去を維持
#   ✔ ranking由来OHLCは疑似OHLCとして扱い、本物ATRは作らない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional, Dict, Any

import pandas as pd

from .schema import RANKING_SUMMARY_COLUMNS

logger = logging.getLogger(__name__)


# ============================================================
# column helpers
# ============================================================

def first_existing_col(df: pd.DataFrame, names: list[str]) -> Optional[str]:
    for name in names:
        if name in df.columns:
            return name
    return None


def ensure_col_from_alias(
    df: pd.DataFrame,
    target: str,
    aliases: list[str],
    default=None,
) -> None:
    if target in df.columns:
        return

    src = first_existing_col(df, aliases)
    if src is not None:
        df[target] = df[src]
    else:
        df[target] = default


def fill_missing_from_alias(
    df: pd.DataFrame,
    target: str,
    aliases: list[str],
) -> None:
    if target not in df.columns:
        df[target] = None

    for src in aliases:
        if src not in df.columns or src == target:
            continue
        try:
            df[target] = df[target].where(df[target].notna(), df[src])
        except Exception:
            pass


# ============================================================
# type helpers
# ============================================================

def coerce_datetime_series(s: pd.Series) -> pd.Series:
    out = pd.to_datetime(s, errors="coerce")

    try:
        out = out.dt.tz_localize(None)
    except Exception:
        try:
            out = out.dt.tz_convert(None)
        except Exception:
            pass

    return out


def safe_numeric(df: pd.DataFrame, cols: list[str]) -> None:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")


def safe_text(df: pd.DataFrame, cols: list[str]) -> None:
    for c in cols:
        if c in df.columns:
            df[c] = df[c].fillna("").astype(str)


def safe_int_flags(df: pd.DataFrame, cols: list[str]) -> None:
    for c in cols:
        if c not in df.columns:
            df[c] = 0
        try:
            df[c] = df[c].fillna(False).astype(bool).astype(int)
        except Exception:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)


def add_date_time_columns(df: pd.DataFrame) -> None:
    if "datetime" not in df.columns:
        return

    dt_s = pd.to_datetime(df["datetime"], errors="coerce")

    if "date" not in df.columns:
        df["date"] = dt_s.dt.strftime("%Y-%m-%d")

    if "time" not in df.columns:
        df["time"] = dt_s.dt.strftime("%H:%M:%S")

    if "time_range" not in df.columns:
        df["time_range"] = df["time"]


# ============================================================
# normalize main
# ============================================================

def normalize_for_save(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if "symbol" not in out.columns:
        logger.warning("[RANKING SUMMARY SAVE] no symbol column interval=%s", interval)
        return pd.DataFrame()

    if "datetime" not in out.columns:
        logger.warning("[RANKING SUMMARY SAVE] no datetime column interval=%s", interval)
        return pd.DataFrame()

    # ========================================================
    # alias 補完
    # ========================================================
    ensure_col_from_alias(out, "symbolname", ["name", "symbol_name", "SymbolName"], "")

    ensure_col_from_alias(
        out,
        "current_price",
        ["close", "close_price", "price", "last_price", "CurrentPrice"],
        None,
    )
    ensure_col_from_alias(
        out,
        "close",
        ["close_price", "current_price", "price", "last_price", "CurrentPrice"],
        None,
    )

    ensure_col_from_alias(out, "open", ["open_price", "close", "current_price"], None)
    ensure_col_from_alias(out, "high", ["high_price", "close", "current_price"], None)
    ensure_col_from_alias(out, "low", ["low_price", "close", "current_price"], None)

    ensure_col_from_alias(out, "open_price", ["open", "close", "current_price"], None)
    ensure_col_from_alias(out, "high_price", ["high", "close", "current_price"], None)
    ensure_col_from_alias(out, "low_price", ["low", "close", "current_price"], None)
    ensure_col_from_alias(out, "close_price", ["close", "current_price"], None)

    fill_missing_from_alias(out, "close", ["close_price", "current_price", "price", "last_price"])
    fill_missing_from_alias(out, "current_price", ["close", "close_price", "price", "last_price"])
    fill_missing_from_alias(out, "close_price", ["close", "current_price", "price", "last_price"])

    for price_col in ["open", "high", "low", "open_price", "high_price", "low_price"]:
        fill_missing_from_alias(out, price_col, ["close", "close_price", "current_price"])

    ensure_col_from_alias(
        out,
        "volume",
        ["vol", "Volume", "volume_1m", "trading_volume"],
        0,
    )

    ensure_col_from_alias(out, "signal", ["macd_signal"], None)
    ensure_col_from_alias(out, "macd_signal", ["signal"], None)
    fill_missing_from_alias(out, "signal", ["macd_signal"])
    fill_missing_from_alias(out, "macd_signal", ["signal"])

    ensure_col_from_alias(out, "score_mtf", ["mtf_score"], None)
    ensure_col_from_alias(out, "mtf_score", ["score_mtf"], None)
    fill_missing_from_alias(out, "score_mtf", ["mtf_score"])
    fill_missing_from_alias(out, "mtf_score", ["score_mtf"])

    ensure_col_from_alias(
        out,
        "display_score",
        ["disp_score", "final_score", "score_total", "score"],
        None,
    )
    ensure_col_from_alias(
        out,
        "disp_score",
        ["display_score", "final_score", "score_total", "score"],
        None,
    )
    ensure_col_from_alias(
        out,
        "final_score",
        ["score_total", "display_score", "disp_score", "score"],
        None,
    )
    ensure_col_from_alias(
        out,
        "score_total",
        ["final_score", "display_score", "disp_score", "score"],
        None,
    )

    ensure_col_from_alias(out, "score_buy", ["buy_score", "disp_buy_score"], None)
    ensure_col_from_alias(out, "score_sell", ["sell_score", "disp_sell_score"], None)
    ensure_col_from_alias(out, "score_slope", ["slope_score"], None)

    ensure_col_from_alias(out, "best_rank", ["rank"], None)
    ensure_col_from_alias(out, "hit_count", ["hist"], None)
    ensure_col_from_alias(out, "hist", ["hit_count"], None)

    # ========================================================
    # datetime / symbol 正規化
    # ========================================================
    out["datetime"] = coerce_datetime_series(out["datetime"])
    out = out.dropna(subset=["symbol", "datetime"])

    if out.empty:
        return pd.DataFrame()

    out["symbol"] = (
        out["symbol"]
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )
    out = out[out["symbol"] != ""]

    if out.empty:
        return pd.DataFrame()

    # ========================================================
    # ranking snapshot 専用特徴量
    # 注意:
    #   ranking由来のOHLCは擬似OHLCなので、本物ATRは作らない
    # ========================================================
    try:
        out = out.sort_values(["symbol", "datetime"])

        out["close"] = pd.to_numeric(out["close"], errors="coerce")
        out["prev_close"] = out.groupby("symbol")["close"].shift(1)

        out["price_delta"] = out["close"] - out["prev_close"]
        out["price_delta_pct"] = out["price_delta"] / out["prev_close"].replace(0, pd.NA)

        # 本物ATRではない。ランキング専用の値動き proxy
        out["ranking_atr_proxy"] = (
            out.groupby("symbol")["price_delta"]
            .transform(lambda s: s.abs().rolling(14, min_periods=3).mean())
        )

        out["ranking_momentum"] = out["price_delta_pct"].fillna(0) * 100.0

        if "rank" in out.columns:
            out["rank"] = pd.to_numeric(out["rank"], errors="coerce")
            out["prev_rank"] = out.groupby("symbol")["rank"].shift(1)
            out["rank_improve"] = out["prev_rank"] - out["rank"]
        else:
            out["rank_improve"] = 0.0

        if "volume" in out.columns:
            out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0)
            out["prev_volume"] = out.groupby("symbol")["volume"].shift(1)
            out["volume_delta"] = (out["volume"] - out["prev_volume"]).fillna(0)
        else:
            out["volume_delta"] = 0.0

        out["ranking_score"] = (
            out["ranking_momentum"].fillna(0) * 1.0
            + out["rank_improve"].fillna(0) * 0.2
            + (out["volume_delta"].fillna(0) > 0).astype(float) * 1.0
        )

    except Exception:
        logger.warning(
            "[RANKING SUMMARY SAVE] ranking feature calculation skipped interval=%s",
            interval,
            exc_info=True,
        )

    # 作業列は保存対象ではないため、後で save_cols に含まれなければ自然に落ちる。
    add_date_time_columns(out)
    out["datetime"] = out["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")

    # ========================================================
    # default columns
    # ========================================================
    defaults: Dict[str, Any] = {
        "symbolname": "",
        "date": "",
        "time": "",
        "time_range": "",

        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "volume": 0,

        "open_price": None,
        "high_price": None,
        "low_price": None,
        "close_price": None,
        "current_price": None,

        "ranking_type": "",
        "rank": None,
        "best_rank": None,
        "hit_count": None,
        "hist": None,
        "change_percentage": None,
        "trading_volume": None,
        "trading_value": None,
        "turnover": None,
        "tick_count": None,

        "ma5": None,
        "ma25": None,
        "ma75": None,
        "rsi": None,
        "rsi_slope": None,
        "macd": None,
        "signal": None,
        "macd_signal": None,
        "macd_hist": None,
        "macd_hist_slope": None,
        "slope": None,
        "slope_atr_scaled": None,

        "price_delta": None,
        "price_delta_pct": None,
        "ranking_atr_proxy": None,
        "ranking_momentum": None,
        "rank_improve": None,
        "volume_delta": None,
        "ranking_score": None,

        "mtf": None,
        "score_mtf": None,
        "mtf_score": None,

        "flag_macd_cross": 0,
        "flag_macd_hist_expand": 0,
        "flag_rsi_rebound": 0,
        "flag_rsi_midline_cross": 0,
        "flag_macd_dc": 0,
        "flag_macd_hist_contract": 0,
        "flag_rsi_falling": 0,
        "flag_rsi_overbought_70": 0,

        "score": None,
        "score_buy": None,
        "score_sell": None,
        "score_total": None,
        "final_score": None,
        "display_score": None,
        "disp_score": None,
        "score_slope": None,

        "base": None,
        "trend": None,
        "mom": None,
        "vel": None,
        "pen": None,

        "interval": int(interval),
        "source": "ranking",
        "price_source": "ranking_snapshot",
        "mode": "",
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    for c, default in defaults.items():
        if c not in out.columns:
            out[c] = default

    # ========================================================
    # 型変換
    # ========================================================
    numeric_cols = [
        "open", "high", "low", "close", "volume",
        "open_price", "high_price", "low_price", "close_price", "current_price",
        "rank", "best_rank", "hit_count", "hist", "change_percentage",
        "trading_volume", "trading_value", "turnover", "tick_count",
        "ma5", "ma25", "ma75", "rsi", "rsi_slope",
        "macd", "signal", "macd_signal", "macd_hist", "macd_hist_slope",
        "slope", "slope_atr_scaled",
        "price_delta", "price_delta_pct", "ranking_atr_proxy",
        "ranking_momentum", "rank_improve", "volume_delta", "ranking_score",
        "mtf", "score_mtf", "mtf_score",
        "score", "score_buy", "score_sell", "score_total",
        "final_score", "display_score", "disp_score", "score_slope",
        "base", "trend", "mom", "vel", "pen",
    ]
    safe_numeric(out, numeric_cols)

    text_cols = [
        "symbolname", "date", "time", "time_range",
        "ranking_type", "source", "price_source", "mode", "updated_at",
    ]
    safe_text(out, text_cols)

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
    safe_int_flags(out, flag_cols)

    # ========================================================
    # 最終補完
    # ========================================================
    out["interval"] = int(interval)

    if "source" not in out.columns:
        out["source"] = "ranking"
    else:
        out["source"] = out["source"].replace("", "ranking").fillna("ranking")

    if "price_source" not in out.columns:
        out["price_source"] = "ranking_snapshot"
    else:
        out["price_source"] = (
            out["price_source"]
            .replace("", "ranking_snapshot")
            .fillna("ranking_snapshot")
        )

    out["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for c in ["close", "close_price", "current_price"]:
        fill_missing_from_alias(out, c, ["close", "close_price", "current_price", "price", "last_price"])

    for c in ["open", "high", "low", "open_price", "high_price", "low_price"]:
        fill_missing_from_alias(out, c, ["close", "close_price", "current_price"])

    fill_missing_from_alias(out, "signal", ["macd_signal"])
    fill_missing_from_alias(out, "macd_signal", ["signal"])

    fill_missing_from_alias(out, "display_score", ["disp_score", "final_score", "score_total", "score"])
    fill_missing_from_alias(out, "disp_score", ["display_score"])
    fill_missing_from_alias(out, "final_score", ["score_total", "display_score", "score"])
    fill_missing_from_alias(out, "score_total", ["final_score", "display_score", "score"])

    # ========================================================
    # 保存列に整列
    # ========================================================
    save_cols = [name for name, _typ in RANKING_SUMMARY_COLUMNS]

    for c in save_cols:
        if c not in out.columns:
            out[c] = defaults.get(c, None)

    out = out[save_cols]

    before = len(out)
    out = out.drop_duplicates(["symbol", "datetime"], keep="last")
    after = len(out)

    if before != after:
        logger.warning(
            "[RANKING SUMMARY SAVE] input dedupe interval=%s before=%s after=%s deleted=%s",
            interval,
            before,
            after,
            before - after,
        )

    price_missing = out["close"].isna().sum() if "close" in out.columns else len(out)
    if price_missing > 0:
        logger.warning(
            "[RANKING SUMMARY SAVE] close missing rows interval=%s missing=%s total=%s",
            interval,
            int(price_missing),
            len(out),
        )

    return out.reset_index(drop=True)


__all__ = [
    "first_existing_col",
    "ensure_col_from_alias",
    "fill_missing_from_alias",
    "coerce_datetime_series",
    "safe_numeric",
    "safe_text",
    "safe_int_flags",
    "add_date_time_columns",
    "normalize_for_save",
]