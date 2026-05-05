# ============================================================
# File   : trading/summary/mtf/daily_ma_mtf.py
# Version: PRODUCTION-STABLE-DAILY-MA-MTF-REV1.1
# ------------------------------------------------------------
# Purpose:
#   - 日足 5日線 / 25日線 / 75日線 を summary_df に付与する
#   - 日足MAベースの MTF スコアを計算する
#   - 既存の短期 MTF を壊さず、score_mtf_daily として加算する
#
# Input daily_df columns:
#   symbol
#   daily_date
#   daily_close
#   daily_ma5
#   daily_ma25
#   daily_ma75
#   daily_ma5_slope       optional
#   daily_ma25_slope      optional
#   daily_ma75_slope      optional
#
# Output columns:
#   daily_close
#   daily_ma5
#   daily_ma25
#   daily_ma75
#   daily_ma5_slope
#   daily_ma25_slope
#   daily_ma75_slope
#   daily_mtf_buy
#   daily_mtf_sell
#   daily_trend
#   score_mtf_short
#   score_mtf_daily
#   score_mtf
#   mtf
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# column helpers
# ------------------------------------------------------------

def _find_col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _to_symbol_str(s: pd.Series) -> pd.Series:
    return (
        s.astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
        .str.replace(r"\.T$", "", regex=True)
    )


def _safe_num(value, default: float = 0.0):
    if isinstance(value, pd.Series):
        return pd.to_numeric(value, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(default)
    return pd.to_numeric(value, errors="coerce")


# ------------------------------------------------------------
# daily MTF scoring
# ------------------------------------------------------------

def add_daily_mtf_score(
    df: pd.DataFrame,
    *,
    close_col: str = "daily_close",
    ma5_col: str = "daily_ma5",
    ma25_col: str = "daily_ma25",
    ma75_col: str = "daily_ma75",
    ma5_slope_col: str = "daily_ma5_slope",
    ma25_slope_col: str = "daily_ma25_slope",
    ma75_slope_col: str = "daily_ma75_slope",
    use_slope_bonus: bool = True,
) -> pd.DataFrame:
    """
    日足MAから daily_mtf_buy / daily_mtf_sell を計算する。

    BUY 基本最大5点:
      +1 daily_close > daily_ma5
      +1 daily_close > daily_ma25
      +1 daily_close > daily_ma75
      +1 daily_ma5 > daily_ma25
      +1 daily_ma25 > daily_ma75

    SELL 基本最大5点:
      +1 daily_close < daily_ma5
      +1 daily_close < daily_ma25
      +1 daily_close < daily_ma75
      +1 daily_ma5 < daily_ma25
      +1 daily_ma25 < daily_ma75

    use_slope_bonus=True の場合:
      +0.5 MA_5_Slope / MA_25_Slope / MA_75_Slope が同方向
      最大 +1.5
    """
    if df is None or df.empty:
        return df

    out = df.copy()

    for c in [close_col, ma5_col, ma25_col, ma75_col]:
        if c not in out.columns:
            out[c] = np.nan
        out[c] = pd.to_numeric(out[c], errors="coerce")

    for c in [ma5_slope_col, ma25_slope_col, ma75_slope_col]:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)

    valid = (
        out[close_col].notna()
        & out[ma5_col].notna()
        & out[ma25_col].notna()
        & out[ma75_col].notna()
        & (out[close_col] > 0)
        & (out[ma5_col] > 0)
        & (out[ma25_col] > 0)
        & (out[ma75_col] > 0)
    )

    buy = pd.Series(0.0, index=out.index)
    sell = pd.Series(0.0, index=out.index)

    buy += np.where(valid & (out[close_col] > out[ma5_col]), 1.0, 0.0)
    buy += np.where(valid & (out[close_col] > out[ma25_col]), 1.0, 0.0)
    buy += np.where(valid & (out[close_col] > out[ma75_col]), 1.0, 0.0)
    buy += np.where(valid & (out[ma5_col] > out[ma25_col]), 1.0, 0.0)
    buy += np.where(valid & (out[ma25_col] > out[ma75_col]), 1.0, 0.0)

    sell += np.where(valid & (out[close_col] < out[ma5_col]), 1.0, 0.0)
    sell += np.where(valid & (out[close_col] < out[ma25_col]), 1.0, 0.0)
    sell += np.where(valid & (out[close_col] < out[ma75_col]), 1.0, 0.0)
    sell += np.where(valid & (out[ma5_col] < out[ma25_col]), 1.0, 0.0)
    sell += np.where(valid & (out[ma25_col] < out[ma75_col]), 1.0, 0.0)

    if use_slope_bonus:
        buy += np.where(valid & (out[ma5_slope_col] > 0), 0.5, 0.0)
        buy += np.where(valid & (out[ma25_slope_col] > 0), 0.5, 0.0)
        buy += np.where(valid & (out[ma75_slope_col] > 0), 0.5, 0.0)

        sell += np.where(valid & (out[ma5_slope_col] < 0), 0.5, 0.0)
        sell += np.where(valid & (out[ma25_slope_col] < 0), 0.5, 0.0)
        sell += np.where(valid & (out[ma75_slope_col] < 0), 0.5, 0.0)

    out["daily_mtf_buy"] = buy.fillna(0.0)
    out["daily_mtf_sell"] = sell.fillna(0.0)

    out["daily_trend"] = np.select(
        [
            out["daily_mtf_buy"] >= 5,
            out["daily_mtf_sell"] >= 5,
            out["daily_mtf_buy"] > out["daily_mtf_sell"],
            out["daily_mtf_sell"] > out["daily_mtf_buy"],
        ],
        [
            "strong_buy",
            "strong_sell",
            "buy",
            "sell",
        ],
        default="neutral",
    )

    return out


# ------------------------------------------------------------
# merge into summary df
# ------------------------------------------------------------

def attach_daily_ma_mtf_to_summary(
    summary_df: pd.DataFrame,
    daily_df: pd.DataFrame,
    *,
    side: str = "auto",
    overwrite_score_mtf: bool = True,
    use_slope_bonus: bool = True,
) -> pd.DataFrame:
    """
    summary_df に日足MAと日足MTFを付与するメイン関数。

    Parameters
    ----------
    summary_df:
        1min / 3min / 5min / ranking summary など。

    daily_df:
        load_daily_mtf_latest_df() が返す日足MAデータ。

    side:
        "buy" or "sell" or "auto"
        buy  の場合 score_mtf_daily = daily_mtf_buy
        sell の場合 score_mtf_daily = daily_mtf_sell
        auto の場合 score_buy/score_sell または signal を見て自動選択

    overwrite_score_mtf:
        True:
          score_mtf = score_mtf_short + score_mtf_daily に更新する
        False:
          daily列だけ追加して score_mtf は触らない
    """
    if summary_df is None or summary_df.empty:
        return summary_df

    out = summary_df.copy()

    symbol_col = _find_col(out, ["symbol", "code", "Symbol", "stock_code"])
    if symbol_col is None:
        logger.warning("[DAILY MTF] summary_df has no symbol column cols=%s", list(out.columns))
        return out

    out["symbol"] = _to_symbol_str(out[symbol_col])

    if daily_df is None or daily_df.empty:
        logger.warning("[DAILY MTF] daily_df empty; skip attach")
        return _ensure_daily_columns(out, overwrite_score_mtf=overwrite_score_mtf)

    daily = daily_df.copy()
    daily_symbol_col = _find_col(daily, ["symbol", "stock_code", "code", "Symbol"])
    if daily_symbol_col is None:
        logger.warning("[DAILY MTF] daily_df has no symbol column cols=%s", list(daily.columns))
        return _ensure_daily_columns(out, overwrite_score_mtf=overwrite_score_mtf)

    daily["symbol"] = _to_symbol_str(daily[daily_symbol_col])

    rename_map = {
        "close": "daily_close",
        "MA_5": "daily_ma5",
        "MA_25": "daily_ma25",
        "MA_75": "daily_ma75",
        "MA_5_Slope": "daily_ma5_slope",
        "MA_25_Slope": "daily_ma25_slope",
        "MA_75_Slope": "daily_ma75_slope",
        "date": "daily_date",
    }
    for src, dst in rename_map.items():
        if src in daily.columns and dst not in daily.columns:
            daily = daily.rename(columns={src: dst})

    daily = add_daily_mtf_score(daily, use_slope_bonus=use_slope_bonus)

    keep_cols = [
        "symbol",
        "daily_date",
        "daily_close",
        "daily_ma5",
        "daily_ma25",
        "daily_ma75",
        "daily_ma5_slope",
        "daily_ma25_slope",
        "daily_ma75_slope",
        "daily_mtf_buy",
        "daily_mtf_sell",
        "daily_trend",
    ]
    keep_cols = [c for c in keep_cols if c in daily.columns]

    daily = (
        daily[keep_cols]
        .sort_values(["symbol"])
        .drop_duplicates(subset=["symbol"], keep="last")
    )

    out = out.merge(daily, on="symbol", how="left", suffixes=("", "_daily_src"))
    out = _ensure_daily_columns(out, overwrite_score_mtf=False)

    if "score_mtf_short" not in out.columns:
        if "score_mtf" in out.columns:
            out["score_mtf_short"] = _safe_num(out["score_mtf"], default=0.0)
        elif "mtf" in out.columns:
            out["score_mtf_short"] = _safe_num(out["mtf"], default=0.0)
        else:
            out["score_mtf_short"] = 0.0
    else:
        out["score_mtf_short"] = _safe_num(out["score_mtf_short"], default=0.0)

    side_l = str(side or "auto").lower().strip()
    if side_l == "buy":
        out["score_mtf_daily"] = out["daily_mtf_buy"]
    elif side_l == "sell":
        out["score_mtf_daily"] = out["daily_mtf_sell"]
    else:
        out["score_mtf_daily"] = _choose_daily_mtf_by_row(out)

    out["score_mtf_daily"] = _safe_num(out["score_mtf_daily"], default=0.0)

    if overwrite_score_mtf:
        out["score_mtf"] = out["score_mtf_short"] + out["score_mtf_daily"]
        out["mtf"] = out["score_mtf"]

    logger.info(
        "[DAILY MTF] attached rows=%s daily_hit=%s score_mtf_nonzero=%s daily_buy_nonzero=%s daily_sell_nonzero=%s",
        len(out),
        int((out["daily_close"] > 0).sum()) if "daily_close" in out.columns else 0,
        int((_safe_num(out["score_mtf"], default=0.0) != 0).sum()) if "score_mtf" in out.columns else 0,
        int((out["daily_mtf_buy"] > 0).sum()) if "daily_mtf_buy" in out.columns else 0,
        int((out["daily_mtf_sell"] > 0).sum()) if "daily_mtf_sell" in out.columns else 0,
    )

    return out


def _choose_daily_mtf_by_row(out: pd.DataFrame) -> pd.Series:
    if "score_buy" in out.columns and "score_sell" in out.columns:
        score_buy = _safe_num(out["score_buy"], default=0.0)
        score_sell = _safe_num(out["score_sell"], default=0.0)
        return pd.Series(
            np.where(score_sell > score_buy, out["daily_mtf_sell"], out["daily_mtf_buy"]),
            index=out.index,
        )

    if "signal" in out.columns:
        sig = out["signal"].astype(str).str.upper()
        return pd.Series(
            np.where(sig.str.contains("SELL"), out["daily_mtf_sell"], out["daily_mtf_buy"]),
            index=out.index,
        )

    return out["daily_mtf_buy"]


def _ensure_daily_columns(out: pd.DataFrame, *, overwrite_score_mtf: bool) -> pd.DataFrame:
    text_defaults = {
        "daily_date": None,
        "daily_trend": "neutral",
    }
    numeric_defaults = [
        "daily_close",
        "daily_ma5",
        "daily_ma25",
        "daily_ma75",
        "daily_ma5_slope",
        "daily_ma25_slope",
        "daily_ma75_slope",
        "daily_mtf_buy",
        "daily_mtf_sell",
        "score_mtf_short",
        "score_mtf_daily",
    ]

    for c, default in text_defaults.items():
        if c not in out.columns:
            out[c] = default
        out[c] = out[c].fillna(default if default is not None else "")

    for c in numeric_defaults:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = _safe_num(out[c], default=0.0)

    if "score_mtf" not in out.columns:
        out["score_mtf"] = 0.0
    if "mtf" not in out.columns:
        out["mtf"] = out["score_mtf"]

    if overwrite_score_mtf:
        out["score_mtf"] = _safe_num(out["score_mtf"], default=0.0)
        out["mtf"] = _safe_num(out["mtf"], default=0.0)

    return out


# ------------------------------------------------------------
# DB schema helper columns
# ------------------------------------------------------------

DAILY_MTF_SUMMARY_COLUMNS_SQLITE: dict[str, str] = {
    "daily_date": "TEXT",
    "daily_close": "REAL DEFAULT 0",
    "daily_ma5": "REAL DEFAULT 0",
    "daily_ma25": "REAL DEFAULT 0",
    "daily_ma75": "REAL DEFAULT 0",
    "daily_ma5_slope": "REAL DEFAULT 0",
    "daily_ma25_slope": "REAL DEFAULT 0",
    "daily_ma75_slope": "REAL DEFAULT 0",
    "daily_mtf_buy": "REAL DEFAULT 0",
    "daily_mtf_sell": "REAL DEFAULT 0",
    "daily_trend": "TEXT",
    "score_mtf_short": "REAL DEFAULT 0",
    "score_mtf_daily": "REAL DEFAULT 0",
}


def ensure_daily_mtf_columns_sqlite(conn, table_name: str) -> None:
    """
    sqlite3.Connection または SQLAlchemy raw connection に対して、
    summary table に日足MTF列を追加する。
    """
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    existing = {row[1] for row in cur.fetchall()}

    for col, typ in DAILY_MTF_SUMMARY_COLUMNS_SQLITE.items():
        if col in existing:
            continue
        sql = f"ALTER TABLE {table_name} ADD COLUMN {col} {typ}"
        logger.info("[DAILY MTF SCHEMA] %s", sql)
        cur.execute(sql)

    conn.commit()
