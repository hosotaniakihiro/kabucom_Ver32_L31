# ============================================================
# File   : trading/summary/recovery/persistence_pkg/cache_builder.py
# Ver    : PRODUCTION-STABLE-REV9.1-CACHE-BUILDER
#          -HISTORY-AWARE-TECHNICAL-FFILL
#          -LATEST-CACHE-TECHNICAL-GUARD
# ------------------------------------------------------------
# 【概要】
#   completed-ish cache DataFrame builder
#
# 【REV9.1 修正】
#   ✔ latest 1行/銘柄へ圧縮する前に technical columns を symbol別に forward-fill
#   ✔ 0 が未計算値として入りやすい slope / mtf 系は NaN 扱いにして補完
#   ✔ score 系は勝手に補完しない
#   ✔ rsi / macd / signal / slope / mtf が latest cache で消える問題を軽減
#   ✔ db_seed 後に realtime/cache_updater が merged summary を上書きしても
#      technical が落ちにくいようにする
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from .column_utils import (
    safe_df,
    normalize_symbol_value,
    pick_first_existing,
    pick_numeric_series_nan,
    coalesce_duplicate_columns,
)
from .datetime_utils import normalize_datetime_like
from .symbol_utils import resolve_symbolname_series
from .score_utils import ensure_score_columns, is_completed_summary_df, repair_mtf_consistency
from .db_normalizer import normalize_numeric_like

logger = logging.getLogger(__name__)


TECHNICAL_FFILL_COLS = [
    "rsi",
    "macd",
    "signal",
    "hist",
    "slope",
    "slope_raw",
    "slope_atr_scaled",
    "score_slope",
    "mtf",
    "score_mtf",
    "mtf_score",
    "mtf_alignment",
    "ma5",
    "ma25",
    "ma75",
    "ma75_slope",
    "ema12",
    "ema26",
    "atr",
    "vwap",
    "vwap_slope",
    "volume_slope",
]

ZERO_AS_MISSING_TECH_COLS = [
    "slope",
    "slope_raw",
    "slope_atr_scaled",
    "score_slope",
    "mtf",
    "score_mtf",
    "mtf_score",
    "mtf_alignment",
]


def _count_nonnull_numeric(df: pd.DataFrame, col: str) -> int:
    try:
        if df is None or df.empty or col not in df.columns:
            return 0
        return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    except Exception:
        return 0


def _count_nonzero_numeric(df: pd.DataFrame, col: str) -> int:
    try:
        if df is None or df.empty or col not in df.columns:
            return 0
        s = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return int((s != 0).sum())
    except Exception:
        return 0


def _prepare_technical_ffill(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    """
    latest cache 作成前に、symbol ごとに technical columns を forward-fill する。

    理由:
      realtime/update_cache 経由では最新barだけが渡ることがあり、
      その最新barの rsi/macd/slope/mtf が未計算のまま latest cache に採用される。
      full history が渡っている場合は、過去の有効 technical を latest 行へ補完する。
    """
    out = safe_df(df)
    if out.empty:
        return out

    if "symbol" not in out.columns:
        return out

    if "datetime" not in out.columns:
        return out

    try:
        out["symbol"] = out["symbol"].map(normalize_symbol_value)
        out = out[out["symbol"] != ""].copy()
        if out.empty:
            return out

        out = normalize_datetime_like(out)
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.dropna(subset=["datetime"]).copy()

        if out.empty:
            return out

        out = out.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

        existing = [c for c in TECHNICAL_FFILL_COLS if c in out.columns]

        for c in existing:
            try:
                out[c] = pd.to_numeric(out[c], errors="coerce")
            except Exception:
                pass

        for c in ZERO_AS_MISSING_TECH_COLS:
            if c in out.columns:
                try:
                    s = pd.to_numeric(out[c], errors="coerce")
                    out.loc[s.eq(0), c] = pd.NA
                except Exception:
                    logger.debug(
                        "[summary.recovery.persistence] technical zero->NA failed interval=%s col=%s",
                        interval,
                        c,
                        exc_info=True,
                    )

        if existing:
            out[existing] = out.groupby("symbol", group_keys=False)[existing].ffill()

        # mtf / score_mtf / mtf_score alias 補完
        try:
            if "score_mtf" in out.columns and "mtf_score" in out.columns:
                sm = pd.to_numeric(out["score_mtf"], errors="coerce")
                ms = pd.to_numeric(out["mtf_score"], errors="coerce")
                out["score_mtf"] = sm.combine_first(ms)
                out["mtf_score"] = ms.combine_first(sm)
            elif "score_mtf" in out.columns and "mtf_score" not in out.columns:
                out["mtf_score"] = out["score_mtf"]
            elif "mtf_score" in out.columns and "score_mtf" not in out.columns:
                out["score_mtf"] = out["mtf_score"]
        except Exception:
            logger.debug(
                "[summary.recovery.persistence] mtf alias repair failed interval=%s",
                interval,
                exc_info=True,
            )

        logger.info(
            "[summary.recovery.persistence] technical ffill prepared interval=%s rows=%s symbols=%s "
            "rsi=%s macd=%s signal=%s slope=%s mtf=%s score_mtf=%s mtf_score=%s",
            interval,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            _count_nonnull_numeric(out, "rsi"),
            _count_nonnull_numeric(out, "macd"),
            _count_nonnull_numeric(out, "signal"),
            _count_nonnull_numeric(out, "slope"),
            _count_nonnull_numeric(out, "mtf"),
            _count_nonnull_numeric(out, "score_mtf"),
            _count_nonnull_numeric(out, "mtf_score"),
        )

        return out

    except Exception:
        logger.debug(
            "[summary.recovery.persistence] technical ffill prepare failed interval=%s",
            interval,
            exc_info=True,
        )
        return df


def make_completedish_cache_df(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    out = coalesce_duplicate_columns(out)
    out = normalize_datetime_like(out)
    out = normalize_numeric_like(out)

    if "symbol" not in out.columns:
        return pd.DataFrame()

    out["symbol"] = out["symbol"].map(normalize_symbol_value)
    out = out[out["symbol"] != ""].copy()
    if out.empty:
        return out

    if "symbolname" not in out.columns:
        out["symbolname"] = ""
    out["symbolname"] = resolve_symbolname_series(out)

    out = ensure_score_columns(out)

    for dst, candidates in {
        "open": ["open", "open_price"],
        "high": ["high", "high_price"],
        "low": ["low", "low_price"],
        "close": [
            "close",
            "close_price",
            "price",
            "current_price",
            "currentprice",
            "CurrentPrice",
            "last_price",
            "lastprice",
            "LastPrice",
        ],
    }.items():
        if dst not in out.columns:
            alt = pick_first_existing(out, candidates)
            if alt:
                out[dst] = pd.to_numeric(out[alt], errors="coerce")

    # latest 1行化の前に technical を補完
    out = _prepare_technical_ffill(out, interval=int(interval))

    out = repair_mtf_consistency(out)

    complete_score = pd.Series(0, index=out.index, dtype="int64")
    complete_score += out["symbolname"].fillna("").astype(str).str.strip().ne("").astype(int) * 8

    score_s = pick_numeric_series_nan(out, ["score"])
    buy_s = pick_numeric_series_nan(out, ["score_buy"])
    sell_s = pick_numeric_series_nan(out, ["score_sell"])
    final_s = pick_numeric_series_nan(out, ["final_score"])
    slope_s = pick_numeric_series_nan(out, ["score_slope", "slope", "slope_atr_scaled"])
    mtf_s = pick_numeric_series_nan(out, ["score_mtf", "mtf_score", "mtf"])
    rsi_s = pick_numeric_series_nan(out, ["rsi"])
    macd_s = pick_numeric_series_nan(out, ["macd"])
    signal_s = pick_numeric_series_nan(out, ["signal"])

    complete_score += score_s.notna().astype(int) * 8
    complete_score += buy_s.notna().astype(int) * 7
    complete_score += sell_s.notna().astype(int) * 7
    complete_score += final_s.notna().astype(int) * 6
    complete_score += slope_s.notna().astype(int) * 4
    complete_score += mtf_s.notna().astype(int) * 4
    complete_score += rsi_s.notna().astype(int) * 3
    complete_score += macd_s.notna().astype(int) * 3
    complete_score += signal_s.notna().astype(int) * 3

    all_score_missing = score_s.isna() & buy_s.isna() & sell_s.isna()
    complete_score -= all_score_missing.astype(int) * 20

    zeroish_tech = (
        rsi_s.fillna(0).eq(0)
        & macd_s.fillna(0).eq(0)
        & signal_s.fillna(0).eq(0)
    )
    complete_score -= zeroish_tech.astype(int) * 3

    out["_complete_score"] = complete_score

    sort_cols = ["symbol", "_complete_score"]
    ascending = [True, False]
    if "datetime" in out.columns:
        sort_cols.append("datetime")
        ascending.append(False)

    out = out.sort_values(sort_cols, ascending=ascending, kind="stable")
    out = out.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)
    out = out.drop(columns=["_complete_score"], errors="ignore")

    out = ensure_score_columns(out)
    out = repair_mtf_consistency(out)

    if not is_completed_summary_df(out):
        logger.warning(
            "[summary.recovery.persistence] completedish cache rejected interval=%s rows=%s symbols=%s score_nonnull=%s buy_nonnull=%s sell_nonnull=%s",
            interval,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            int(pick_numeric_series_nan(out, ["score", "score_total", "display_score", "final_score"]).notna().sum()),
            int(pick_numeric_series_nan(out, ["score_buy", "buy_score", "buy"]).notna().sum()),
            int(pick_numeric_series_nan(out, ["score_sell", "sell_score", "sell"]).notna().sum()),
        )
        return pd.DataFrame()

    front = [
        "symbol",
        "symbolname",
        "datetime",
        "date",
        "open",
        "high",
        "low",
        "close",
        "score",
        "score_total",
        "display_score",
        "final_score",
        "score_buy",
        "score_sell",
        "buy_score",
        "sell_score",
        "slope",
        "slope_atr_scaled",
        "score_slope",
        "mtf",
        "score_mtf",
        "mtf_score",
        "rsi",
        "macd",
        "signal",
        "display_ready",
    ]
    ordered = [c for c in front if c in out.columns] + [c for c in out.columns if c not in front]
    out = out[ordered].copy()

    logger.info(
        "[summary.recovery.persistence] completedish cache built interval=%s rows=%s symbols=%s "
        "score_nonnull=%s buy_nonnull=%s sell_nonnull=%s rsi=%s macd=%s signal=%s slope=%s mtf=%s score_mtf=%s",
        interval,
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        int(pick_numeric_series_nan(out, ["score", "score_total", "display_score", "final_score"]).notna().sum()),
        int(pick_numeric_series_nan(out, ["score_buy", "buy_score", "buy"]).notna().sum()),
        int(pick_numeric_series_nan(out, ["score_sell", "score_sell", "sell_score", "sell"]).notna().sum()),
        _count_nonnull_numeric(out, "rsi"),
        _count_nonnull_numeric(out, "macd"),
        _count_nonnull_numeric(out, "signal"),
        _count_nonnull_numeric(out, "slope"),
        _count_nonnull_numeric(out, "mtf"),
        _count_nonnull_numeric(out, "score_mtf"),
    )
    return out


__all__ = [
    "make_completedish_cache_df",
]