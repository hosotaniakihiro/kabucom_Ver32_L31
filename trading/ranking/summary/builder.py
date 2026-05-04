# ============================================================
# File   : trading/ranking/summary/builder.py
# Version: PRODUCTION-STABLE-REV3.0-RANKING-SUMMARY-BUILDER-WITH-HISTORY
# ------------------------------------------------------------
# Purpose:
#   ranking_snapshot_1min から ranking_summary_1min/3min/5min 用
#   DataFrame を生成する。
#
# REV3.0:
#   ✔ ranking_snapshot 単体ではなく、summary_1min 履歴を結合してから
#     MA5/25/75, RSI, MACD を計算
#   ✔ 前日/当日の stock_summary_1min 履歴を history_loader.py から読む
#   ✔ 履歴が読めない場合は従来どおり ranking_snapshot 単体で計算
#   ✔ 既存関数名を維持し、呼び出し元の互換性を確保
#
# Important:
#   - ランキング由来は擬似OHLC
#   - ranking current_price から open = high = low = close を作る
#   - テクニカル計算は「履歴 + ランキング疑似足」で行う
#   - 保存/表示対象は最後に ranking 行だけへ戻す
#   - 本物ATRは作らない
#   - ranking専用特徴量は features.py で付与する
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


try:
    from trading.ranking.summary.features import add_ranking_only_features
except Exception:  # pragma: no cover
    add_ranking_only_features = None


try:
    from trading.ranking.summary.history_loader import (
        DEFAULT_HISTORY_MINUTES,
        load_summary_1min_history,
        merge_history_and_ranking_pseudo,
    )
except Exception:  # pragma: no cover
    DEFAULT_HISTORY_MINUTES = 420
    load_summary_1min_history = None
    merge_history_and_ranking_pseudo = None


# ============================================================
# basic helpers
# ============================================================

def _to_numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _first_existing_col(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _ensure_col(df: pd.DataFrame, col: str, default: Any = np.nan) -> None:
    if col not in df.columns:
        df[col] = default


def _normalize_symbol(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    elif "time" in out.columns and "date" in out.columns:
        out["datetime"] = pd.to_datetime(
            out["date"].astype(str) + " " + out["time"].astype(str),
            errors="coerce",
        )
    elif "timestamp" in out.columns:
        out["datetime"] = pd.to_datetime(out["timestamp"], errors="coerce")
    elif "snapshot_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["snapshot_time"], errors="coerce")
    elif "inserted_at" in out.columns:
        out["datetime"] = pd.to_datetime(out["inserted_at"], errors="coerce")
    else:
        out["datetime"] = pd.NaT

    out = out.dropna(subset=["datetime"])

    if out.empty:
        return out

    try:
        out["datetime"] = out["datetime"].dt.floor("min")
    except Exception:
        pass

    out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    out["time"] = out["datetime"].dt.strftime("%H:%M:%S")

    return out


def _extract_symbols(df: pd.DataFrame) -> list[str]:
    if df is None or df.empty or "symbol" not in df.columns:
        return []

    return sorted(
        {
            _normalize_symbol(x)
            for x in df["symbol"].dropna().tolist()
            if _normalize_symbol(x)
        }
    )


def _is_ranking_row(df: pd.DataFrame) -> pd.Series:
    """
    ranking_snapshot / ranking_summary 由来の行を判定する。
    """
    if df is None or df.empty:
        return pd.Series([], dtype=bool)

    mask = pd.Series(False, index=df.index)

    if "source" in df.columns:
        mask = mask | (
            df["source"]
            .fillna("")
            .astype(str)
            .str.contains("ranking", case=False, na=False)
        )

    if "price_source" in df.columns:
        mask = mask | (
            df["price_source"]
            .fillna("")
            .astype(str)
            .str.contains("ranking", case=False, na=False)
        )

    # ranking_type / rank がある行も ranking 行として扱う
    if "ranking_type" in df.columns:
        mask = mask | (df["ranking_type"].fillna("").astype(str).str.strip() != "")

    if "rank" in df.columns:
        rank_s = pd.to_numeric(df["rank"], errors="coerce")
        mask = mask | rank_s.notna()

    return mask


def _safe_dt_max(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    if df is None or df.empty or "datetime" not in df.columns:
        return None

    s = pd.to_datetime(df["datetime"], errors="coerce")
    s = s.dropna()

    if s.empty:
        return None

    return s.max()


def _safe_trade_date_from_df(df: pd.DataFrame) -> Optional[str]:
    mx = _safe_dt_max(df)

    if mx is None or pd.isna(mx):
        return None

    return pd.Timestamp(mx).strftime("%Y%m%d")


# ============================================================
# snapshot normalize
# ============================================================

def normalize_ranking_snapshot_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking_snapshot_1min / ranking_raw 系の列名差を吸収する。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out = _normalize_datetime(out)

    if out.empty:
        return pd.DataFrame()

    symbol_col = _first_existing_col(
        out,
        [
            "symbol",
            "Symbol",
            "code",
            "Code",
            "銘柄コード",
        ],
    )
    name_col = _first_existing_col(
        out,
        [
            "symbolname",
            "symbol_name",
            "name",
            "Name",
            "銘柄名",
        ],
    )
    price_col = _first_existing_col(
        out,
        [
            "current_price",
            "price",
            "close",
            "close_price",
            "last_price",
            "CurrentPrice",
            "現在値",
        ],
    )
    rank_col = _first_existing_col(
        out,
        [
            "rank",
            "ranking",
            "Rank",
            "順位",
        ],
    )
    volume_col = _first_existing_col(
        out,
        [
            "volume",
            "Volume",
            "出来高",
            "trading_volume",
        ],
    )
    trading_value_col = _first_existing_col(
        out,
        [
            "trading_value",
            "売買代金",
            "turnover",
        ],
    )
    tick_col = _first_existing_col(
        out,
        [
            "tick_count",
            "TICK回数",
            "ticks",
        ],
    )
    change_pct_col = _first_existing_col(
        out,
        [
            "change_percentage",
            "change_ratio",
            "騰落率",
            "前日比率",
        ],
    )
    ranking_type_col = _first_existing_col(
        out,
        [
            "ranking_type",
            "type",
            "ranking_name",
            "category",
            "ランキング種別",
        ],
    )
    market_col = _first_existing_col(
        out,
        [
            "market",
            "exchange",
            "division",
            "市場",
        ],
    )

    if symbol_col is None:
        logger.warning("[RANKING SUMMARY BUILDER] symbol column not found")
        return pd.DataFrame()

    if price_col is None:
        logger.warning("[RANKING SUMMARY BUILDER] price column not found")
        return pd.DataFrame()

    out["symbol"] = out[symbol_col].map(_normalize_symbol)
    out["symbolname"] = out[name_col].astype(str) if name_col else ""
    out["close"] = _to_numeric(out[price_col])

    if rank_col:
        out["rank"] = _to_numeric(out[rank_col])
    else:
        out["rank"] = np.nan

    if volume_col:
        out["volume"] = _to_numeric(out[volume_col]).fillna(0.0)
    else:
        out["volume"] = 0.0

    if trading_value_col:
        out["trading_value"] = _to_numeric(out[trading_value_col])
    else:
        out["trading_value"] = np.nan

    if tick_col:
        out["tick_count"] = _to_numeric(out[tick_col])
    else:
        out["tick_count"] = np.nan

    if change_pct_col:
        out["change_percentage"] = _to_numeric(out[change_pct_col])
    else:
        out["change_percentage"] = np.nan

    if ranking_type_col:
        out["ranking_type"] = out[ranking_type_col].fillna("").astype(str)
    else:
        out["ranking_type"] = ""

    if market_col:
        out["market"] = out[market_col].fillna("").astype(str)
    else:
        out["market"] = ""

    out = out.dropna(subset=["close"])
    out = out[out["symbol"].astype(str) != ""]
    out = out[out["close"] > 0]

    if out.empty:
        return pd.DataFrame()

    out = out.sort_values(["symbol", "datetime"])
    out = out.drop_duplicates(
        ["symbol", "datetime", "ranking_type", "market"],
        keep="last",
    )

    return out.reset_index(drop=True)


# ============================================================
# pseudo OHLC
# ============================================================

def build_pseudo_ohlc_1min(snapshot_df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking_snapshot から 1分擬似OHLCを作る。

    ランキングは1点価格しかないため:
      open = high = low = close
    """
    base = normalize_ranking_snapshot_df(snapshot_df)

    if base.empty:
        return base

    out = base.copy()

    out["open"] = out["close"]
    out["high"] = out["close"]
    out["low"] = out["close"]

    out["open_price"] = out["open"]
    out["high_price"] = out["high"]
    out["low_price"] = out["low"]
    out["close_price"] = out["close"]
    out["current_price"] = out["close"]

    out["source"] = "ranking_snapshot"
    out["price_source"] = "ranking_snapshot"

    cols = [
        "symbol",
        "symbolname",
        "datetime",
        "date",
        "time",
        "ranking_type",
        "market",
        "rank",
        "open",
        "high",
        "low",
        "close",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "current_price",
        "volume",
        "trading_value",
        "tick_count",
        "change_percentage",
        "source",
        "price_source",
    ]

    for c in cols:
        _ensure_col(out, c)

    out = out[cols].copy()
    out = out.sort_values(["symbol", "datetime"])

    logger.info(
        "[RANKING SUMMARY BUILDER] pseudo 1min built rows=%s symbols=%s dt_min=%s dt_max=%s",
        len(out),
        out["symbol"].nunique(),
        out["datetime"].min() if "datetime" in out.columns and not out.empty else None,
        out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
    )

    return out.reset_index(drop=True)


# ============================================================
# history merge
# ============================================================

def build_calc_base_with_history(
    ranking_pseudo_df: pd.DataFrame,
    *,
    use_summary_history: bool = True,
    history_df: Optional[pd.DataFrame] = None,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    summary_db_path: Optional[str] = None,
    summary_dir: Optional[str] = None,
    include_previous: bool = True,
    history_minutes: int = DEFAULT_HISTORY_MINUTES,
) -> pd.DataFrame:
    """
    テクニカル計算用の DataFrame を作る。

    ranking_pseudo_df:
        ranking_snapshot 由来の疑似OHLC

    history_df:
        外部ですでに読んだ stock_summary_1min 履歴。
        指定された場合は DB 読み込みより優先。

    use_summary_history:
        True なら history_loader.py から summary_1min 履歴を読む。
        履歴読み込み失敗時は ranking_pseudo_df 単体へ fallback。
    """
    if ranking_pseudo_df is None or ranking_pseudo_df.empty:
        return pd.DataFrame()

    pseudo = ranking_pseudo_df.copy()
    pseudo["datetime"] = pd.to_datetime(pseudo["datetime"], errors="coerce")
    pseudo = pseudo.dropna(subset=["symbol", "datetime", "close"])

    if pseudo.empty:
        return pd.DataFrame()

    if "source" not in pseudo.columns:
        pseudo["source"] = "ranking_snapshot"
    if "price_source" not in pseudo.columns:
        pseudo["price_source"] = "ranking_snapshot"

    loaded_history = pd.DataFrame()

    if history_df is not None and not history_df.empty:
        loaded_history = history_df.copy()

    elif use_summary_history and load_summary_1min_history is not None:
        symbols = _extract_symbols(pseudo)
        end_dt = _safe_dt_max(pseudo)
        d = trade_date or _safe_trade_date_from_df(pseudo)

        kwargs: dict[str, Any] = dict(
            trade_date=d,
            symbols=symbols,
            summary_db_path=summary_db_path,
            include_previous=include_previous,
            history_minutes=history_minutes,
            end_datetime=end_dt,
        )

        if summary_dir:
            kwargs["summary_dir"] = summary_dir

        try:
            loaded_history = load_summary_1min_history(**kwargs)
        except Exception:
            logger.warning(
                "[RANKING SUMMARY BUILDER] load summary history failed -> fallback ranking only",
                exc_info=True,
            )
            loaded_history = pd.DataFrame()

    if loaded_history is not None and not loaded_history.empty:
        if merge_history_and_ranking_pseudo is not None:
            try:
                base = merge_history_and_ranking_pseudo(
                    history_df=loaded_history,
                    ranking_pseudo_df=pseudo,
                )
            except Exception:
                logger.warning(
                    "[RANKING SUMMARY BUILDER] merge history failed -> fallback concat",
                    exc_info=True,
                )
                base = pd.concat([loaded_history, pseudo], ignore_index=True, sort=False)
        else:
            base = pd.concat([loaded_history, pseudo], ignore_index=True, sort=False)

        base["datetime"] = pd.to_datetime(base["datetime"], errors="coerce")
        base = base.dropna(subset=["symbol", "datetime", "close"])
        base["symbol"] = base["symbol"].map(_normalize_symbol)
        base = base[base["symbol"] != ""]

        if "source" in base.columns:
            base["_ranking_priority"] = (
                base["source"]
                .fillna("")
                .astype(str)
                .str.contains("ranking", case=False, na=False)
                .astype(int)
            )
            base = base.sort_values(["symbol", "datetime", "_ranking_priority"])
            base = base.drop_duplicates(["symbol", "datetime"], keep="last")
            base = base.drop(columns=["_ranking_priority"], errors="ignore")
        else:
            base = base.sort_values(["symbol", "datetime"])
            base = base.drop_duplicates(["symbol", "datetime"], keep="last")

        base = base.sort_values(["symbol", "datetime"])

        logger.info(
            "[RANKING SUMMARY BUILDER] calc base built with history rows=%s symbols=%s "
            "history_rows=%s ranking_rows=%s dt_min=%s dt_max=%s",
            len(base),
            base["symbol"].nunique() if "symbol" in base.columns else 0,
            len(loaded_history),
            len(pseudo),
            base["datetime"].min() if not base.empty else None,
            base["datetime"].max() if not base.empty else None,
        )

        return base.reset_index(drop=True)

    logger.warning(
        "[RANKING SUMMARY BUILDER] summary history empty -> ranking only indicators rows=%s symbols=%s",
        len(pseudo),
        pseudo["symbol"].nunique() if "symbol" in pseudo.columns else 0,
    )

    return pseudo.reset_index(drop=True)


def extract_ranking_rows_after_calc(
    calc_df: pd.DataFrame,
    *,
    ranking_pseudo_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    履歴 + ranking 疑似足でテクニカル計算した後、
    保存/表示対象の ranking 行だけを取り出す。
    """
    if calc_df is None or calc_df.empty:
        return pd.DataFrame()

    out = calc_df.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["symbol", "datetime"])

    if out.empty:
        return pd.DataFrame()

    mask = _is_ranking_row(out)

    # 念のため、ranking_pseudo_df の symbol/datetime と一致する行も ranking 行として扱う
    if ranking_pseudo_df is not None and not ranking_pseudo_df.empty:
        r = ranking_pseudo_df.copy()
        r["datetime"] = pd.to_datetime(r["datetime"], errors="coerce")
        r = r.dropna(subset=["symbol", "datetime"])

        if not r.empty:
            keys = set(
                zip(
                    r["symbol"].map(_normalize_symbol).astype(str),
                    r["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S"),
                )
            )

            out_keys = list(
                zip(
                    out["symbol"].map(_normalize_symbol).astype(str),
                    out["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S"),
                )
            )

            mask = mask | pd.Series([k in keys for k in out_keys], index=out.index)

    out = out[mask].copy()

    if out.empty:
        return pd.DataFrame()

    out["source"] = "ranking_summary_1min"
    out["price_source"] = "ranking_snapshot"

    out = out.sort_values(["symbol", "datetime"])

    # ranking_type / market 別の重複は残す。
    # ただし完全重複は削除。
    subset = ["symbol", "datetime"]
    if "ranking_type" in out.columns:
        subset.append("ranking_type")
    if "market" in out.columns:
        subset.append("market")

    out = out.drop_duplicates(subset, keep="last")

    return out.reset_index(drop=True)


# ============================================================
# indicators
# ============================================================

def add_light_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    軽量 MA/RSI/MACD を付与する。

    REV3.0:
      ranking 行だけではなく「summary履歴 + ranking疑似足」に対して使う。
      そのためランキング行の MA75 / MACD が履歴で成熟しやすくなる。
    """
    if df is None or df.empty:
        return df

    out = df.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"])

    if out.empty:
        return out

    if "symbol" not in out.columns or "close" not in out.columns:
        return out

    out["symbol"] = out["symbol"].map(_normalize_symbol)
    out = out[out["symbol"] != ""]
    out["close"] = _to_numeric(out["close"])

    out = out.dropna(subset=["close"])
    out = out[out["close"] > 0]

    if out.empty:
        return out

    out = out.sort_values(["symbol", "datetime"])

    g = out.groupby("symbol", group_keys=False)

    out["ma5"] = g["close"].transform(lambda s: s.rolling(5, min_periods=1).mean())
    out["ma25"] = g["close"].transform(lambda s: s.rolling(25, min_periods=1).mean())
    out["ma75"] = g["close"].transform(lambda s: s.rolling(75, min_periods=1).mean())

    delta = g["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)

    avg_gain = gain.groupby(out["symbol"]).transform(
        lambda s: s.rolling(14, min_periods=3).mean()
    )
    avg_loss = loss.groupby(out["symbol"]).transform(
        lambda s: s.rolling(14, min_periods=3).mean()
    )

    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi"] = 100 - (100 / (1 + rs))
    out["rsi"] = out["rsi"].replace([np.inf, -np.inf], np.nan).fillna(50.0)

    ema12 = g["close"].transform(lambda s: s.ewm(span=12, adjust=False).mean())
    ema26 = g["close"].transform(lambda s: s.ewm(span=26, adjust=False).mean())

    out["macd"] = ema12 - ema26
    out["signal"] = out.groupby("symbol")["macd"].transform(
        lambda s: s.ewm(span=9, adjust=False).mean()
    )
    out["hist"] = out["macd"] - out["signal"]

    # slope は close の短期変化率として最低限入れる。
    # 本物ATRではないため slope_atr_scaled はランキング用途では 0 を基本にする。
    out["slope"] = g["close"].transform(lambda s: s.diff(3) / s.shift(3).replace(0, np.nan))
    out["slope"] = out["slope"].replace([np.inf, -np.inf], np.nan).fillna(0.0)

    out["atr"] = 0.0
    out["slope_atr_scaled"] = 0.0

    return out


# ============================================================
# ranking score compatibility
# ============================================================

def add_compat_scores(df: pd.DataFrame) -> pd.DataFrame:
    """
    既存表示・AI候補処理が score 系カラムを期待する場合の互換カラム。
    """
    if df is None or df.empty:
        return df

    out = df.copy()

    if "ranking_score" not in out.columns:
        out["ranking_score"] = 0.0

    out["ranking_score"] = pd.to_numeric(out["ranking_score"], errors="coerce").fillna(0.0)

    out["score_buy"] = out["ranking_score"].clip(lower=0)
    out["score_sell"] = (-out["ranking_score"]).clip(lower=0)
    out["score_total"] = out["score_buy"] - out["score_sell"]
    out["final_score"] = out["score_total"]
    out["display_score"] = out["final_score"]
    out["disp_score"] = out["display_score"]
    out["score"] = out["final_score"]

    if "slope" not in out.columns:
        out["slope"] = 0.0
    if "mtf" not in out.columns:
        out["mtf"] = 0.0
    if "score_mtf" not in out.columns:
        out["score_mtf"] = 0.0
    if "mtf_score" not in out.columns:
        out["mtf_score"] = out["score_mtf"]

    if "score_slope" not in out.columns:
        out["score_slope"] = pd.to_numeric(out.get("slope", 0.0), errors="coerce").fillna(0.0)

    return out


# ============================================================
# resample
# ============================================================

def resample_ranking_summary(
    df_1min: pd.DataFrame,
    *,
    interval: int,
) -> pd.DataFrame:
    """
    ranking_summary_1min から 3min/5min を作る。
    """
    if df_1min is None or df_1min.empty:
        return pd.DataFrame()

    if interval == 1:
        return df_1min.copy()

    if interval not in (3, 5):
        raise ValueError(f"unsupported interval: {interval}")

    df = df_1min.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    df = df.sort_values(["symbol", "datetime"])

    if df.empty:
        return pd.DataFrame()

    freq = f"{interval}min"
    rows: list[pd.DataFrame] = []

    group_cols = ["symbol"]
    if "ranking_type" in df.columns:
        group_cols.append("ranking_type")
    if "market" in df.columns:
        group_cols.append("market")

    for _, g in df.groupby(group_cols, dropna=False):
        g = g.set_index("datetime").sort_index()

        agg = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "last",
            "rank": "last",
            "symbolname": "last",
            "source": "last",
        }

        for c in [
            "ranking_type",
            "market",
            "current_price",
            "price_source",
            "trading_value",
            "tick_count",
            "change_percentage",
            "price_delta",
            "price_delta_pct",
            "ranking_atr_proxy",
            "ranking_momentum",
            "rank_improve",
            "volume_delta",
            "ranking_score",
            "ma5",
            "ma25",
            "ma75",
            "rsi",
            "macd",
            "signal",
            "macd_signal",
            "hist",
            "macd_hist",
            "slope",
            "slope_atr_scaled",
            "mtf",
            "score_mtf",
            "mtf_score",
            "score_slope",
            "score_buy",
            "score_sell",
            "score_total",
            "final_score",
            "display_score",
            "disp_score",
            "score",
            "base",
            "trend",
            "mom",
            "vel",
            "pen",
        ]:
            if c in g.columns and c not in agg:
                agg[c] = "last"

        res = g.resample(freq, label="right", closed="right").agg(agg)
        res = res.dropna(subset=["close"])

        if res.empty:
            continue

        res["symbol"] = g["symbol"].iloc[0]

        rows.append(res.reset_index())

    if not rows:
        return pd.DataFrame()

    out = pd.concat(rows, ignore_index=True)

    out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")
    out["time"] = out["datetime"].dt.strftime("%H:%M:%S")

    out["open_price"] = out["open"]
    out["high_price"] = out["high"]
    out["low_price"] = out["low"]
    out["close_price"] = out["close"]
    out["current_price"] = out["close"]

    out["source"] = f"ranking_summary_resample_{interval}min"
    out["price_source"] = "ranking_snapshot"

    out = out.sort_values(["symbol", "datetime"])

    subset = ["symbol", "datetime"]
    if "ranking_type" in out.columns:
        subset.append("ranking_type")
    if "market" in out.columns:
        subset.append("market")

    out = out.drop_duplicates(subset, keep="last")

    logger.info(
        "[RANKING SUMMARY BUILDER] resampled interval=%s rows=%s symbols=%s",
        interval,
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
    )

    return out.reset_index(drop=True)


# ============================================================
# main build functions
# ============================================================

def build_ranking_summary_1min(
    snapshot_df: pd.DataFrame,
    *,
    add_indicators: bool = True,
    add_features: bool = True,
    use_summary_history: bool = True,
    history_df: Optional[pd.DataFrame] = None,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    summary_db_path: Optional[str] = None,
    summary_dir: Optional[str] = None,
    include_previous: bool = True,
    history_minutes: int = DEFAULT_HISTORY_MINUTES,
) -> pd.DataFrame:
    """
    ranking_snapshot_1min から ranking_summary_1min を作る。

    REV3.0:
      1. ranking_snapshot から疑似OHLCを作る
      2. summary_1min 履歴を読む
      3. 履歴 + ranking疑似足 でテクニカル計算
      4. ranking 行だけを返す
    """
    ranking_pseudo = build_pseudo_ohlc_1min(snapshot_df)

    if ranking_pseudo.empty:
        return ranking_pseudo

    calc_df = build_calc_base_with_history(
        ranking_pseudo,
        use_summary_history=use_summary_history,
        history_df=history_df,
        trade_date=trade_date,
        summary_db_path=summary_db_path,
        summary_dir=summary_dir,
        include_previous=include_previous,
        history_minutes=history_minutes,
    )

    if calc_df.empty:
        return pd.DataFrame()

    if add_indicators:
        calc_df = add_light_indicators(calc_df)

    df = extract_ranking_rows_after_calc(
        calc_df,
        ranking_pseudo_df=ranking_pseudo,
    )

    if df.empty:
        logger.warning(
            "[RANKING SUMMARY BUILDER] ranking rows empty after calc -> fallback pseudo rows"
        )
        df = ranking_pseudo.copy()
        if add_indicators:
            df = add_light_indicators(df)

    if add_features and add_ranking_only_features is not None:
        try:
            df = add_ranking_only_features(df)
        except Exception:
            logger.warning(
                "[RANKING SUMMARY BUILDER] add_ranking_only_features failed",
                exc_info=True,
            )

    df = add_compat_scores(df)

    df["source"] = "ranking_summary_1min"
    df["price_source"] = "ranking_snapshot"

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["symbol", "datetime", "close"])
    df["date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    df["time"] = df["datetime"].dt.strftime("%H:%M:%S")

    for c in ["open", "high", "low", "close"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df["open_price"] = df["open"]
    df["high_price"] = df["high"]
    df["low_price"] = df["low"]
    df["close_price"] = df["close"]
    df["current_price"] = df["close"]

    df = df.sort_values(["symbol", "datetime"])

    subset = ["symbol", "datetime"]
    if "ranking_type" in df.columns:
        subset.append("ranking_type")
    if "market" in df.columns:
        subset.append("market")

    df = df.drop_duplicates(subset, keep="last")

    logger.info(
        "[RANKING SUMMARY BUILDER] build 1min done rows=%s symbols=%s "
        "score_nonzero=%s ma75_nonnull=%s rsi_nonnull=%s macd_nonnull=%s use_history=%s",
        len(df),
        df["symbol"].nunique() if "symbol" in df.columns else 0,
        int((pd.to_numeric(df.get("ranking_score", 0), errors="coerce").fillna(0) != 0).sum())
        if "ranking_score" in df.columns else 0,
        int(df["ma75"].notna().sum()) if "ma75" in df.columns else 0,
        int(df["rsi"].notna().sum()) if "rsi" in df.columns else 0,
        int(df["macd"].notna().sum()) if "macd" in df.columns else 0,
        bool(use_summary_history),
    )

    return df.reset_index(drop=True)


def build_ranking_summary(
    snapshot_df: pd.DataFrame,
    *,
    interval: int = 1,
    add_indicators: bool = True,
    add_features: bool = True,
    use_summary_history: bool = True,
    history_df: Optional[pd.DataFrame] = None,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    summary_db_path: Optional[str] = None,
    summary_dir: Optional[str] = None,
    include_previous: bool = True,
    history_minutes: int = DEFAULT_HISTORY_MINUTES,
) -> pd.DataFrame:
    """
    interval=1/3/5 の ranking_summary を作る。
    """
    df1 = build_ranking_summary_1min(
        snapshot_df,
        add_indicators=add_indicators,
        add_features=add_features,
        use_summary_history=use_summary_history,
        history_df=history_df,
        trade_date=trade_date,
        summary_db_path=summary_db_path,
        summary_dir=summary_dir,
        include_previous=include_previous,
        history_minutes=history_minutes,
    )

    if interval == 1:
        return df1

    return resample_ranking_summary(df1, interval=interval)


def build_all_ranking_summaries(
    snapshot_df: pd.DataFrame,
    *,
    add_indicators: bool = True,
    add_features: bool = True,
    use_summary_history: bool = True,
    history_df: Optional[pd.DataFrame] = None,
    trade_date: Optional[str | int | dt.date | dt.datetime] = None,
    summary_db_path: Optional[str] = None,
    summary_dir: Optional[str] = None,
    include_previous: bool = True,
    history_minutes: int = DEFAULT_HISTORY_MINUTES,
) -> dict[int, pd.DataFrame]:
    """
    1min / 3min / 5min をまとめて作る。
    """
    df1 = build_ranking_summary_1min(
        snapshot_df,
        add_indicators=add_indicators,
        add_features=add_features,
        use_summary_history=use_summary_history,
        history_df=history_df,
        trade_date=trade_date,
        summary_db_path=summary_db_path,
        summary_dir=summary_dir,
        include_previous=include_previous,
        history_minutes=history_minutes,
    )

    df3 = resample_ranking_summary(df1, interval=3) if not df1.empty else pd.DataFrame()
    df5 = resample_ranking_summary(df1, interval=5) if not df1.empty else pd.DataFrame()

    return {
        1: df1,
        3: df3,
        5: df5,
    }


# ============================================================
# compatibility aliases
# ============================================================

def build_from_snapshot(
    snapshot_df: pd.DataFrame,
    *,
    interval: int = 1,
    **kwargs: Any,
) -> pd.DataFrame:
    return build_ranking_summary(snapshot_df, interval=interval, **kwargs)


def build_summary_from_snapshot(
    snapshot_df: pd.DataFrame,
    *,
    interval: int = 1,
    **kwargs: Any,
) -> pd.DataFrame:
    return build_ranking_summary(snapshot_df, interval=interval, **kwargs)


def build_ranking_summary_df(
    snapshot_df: pd.DataFrame,
    *,
    interval: int = 1,
    **kwargs: Any,
) -> pd.DataFrame:
    return build_ranking_summary(snapshot_df, interval=interval, **kwargs)


__all__ = [
    "normalize_ranking_snapshot_df",
    "build_pseudo_ohlc_1min",
    "build_calc_base_with_history",
    "extract_ranking_rows_after_calc",
    "add_light_indicators",
    "add_compat_scores",
    "resample_ranking_summary",
    "build_ranking_summary_1min",
    "build_ranking_summary",
    "build_all_ranking_summaries",
    "build_from_snapshot",
    "build_summary_from_snapshot",
    "build_ranking_summary_df",
]