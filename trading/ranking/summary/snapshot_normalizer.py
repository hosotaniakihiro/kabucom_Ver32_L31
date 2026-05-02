# ============================================================
# File   : trading/ranking/summary/snapshot_normalizer.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-SNAPSHOT-NORMALIZER
# ------------------------------------------------------------
# ranking summary 用 snapshot 正規化
# ranking_summary_engine.py から安全に切り出すためのモジュール
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import pandas as pd

from trading.ranking.summary.symbol_metadata import (
    _ensure_symbolname,
    _normalize_symbol,
    _normalize_symbolname,
)

logger = logging.getLogger(__name__)

NUMERIC_COLUMNS_SNAPSHOT = [
    "rank_position",
    "price",
    "volume",
    "volume_speed",
    "change_rate",
    "score",
    "ranking_score",
]

DEFAULT_KEEP_COLUMNS_SNAPSHOT = [
    "symbol",
    "symbolname",
    "snapshot_time",
    "market",
    "rank_type",
    "rank_position",
    "price",
    "volume",
    "volume_speed",
    "change_rate",
    "source",
    "ranking_score",
]


def _to_dataframe(snapshot_rows: Any) -> pd.DataFrame:
    if snapshot_rows is None:
        return pd.DataFrame()

    try:
        if isinstance(snapshot_rows, pd.DataFrame):
            return snapshot_rows.copy()

        if isinstance(snapshot_rows, dict):
            return pd.DataFrame([snapshot_rows])

        if isinstance(snapshot_rows, (list, tuple)):
            if not snapshot_rows:
                return pd.DataFrame()
            return pd.DataFrame(list(snapshot_rows))

    except Exception:
        logger.exception("[RANKING SUMMARY] to_dataframe failed")

    return pd.DataFrame()


def _drop_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    out = df.copy()

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in tup if str(x) != ""]).strip("_")
                for tup in out.columns
            ]
    except Exception:
        logger.exception("[RANKING SUMMARY] multiindex flatten failed")

    try:
        out.columns = [str(c).strip() for c in out.columns]
        if out.columns.duplicated().any():
            dup = out.columns[out.columns.duplicated()].tolist()
            logger.warning("[RANKING SUMMARY] duplicate columns removed: %s", dup)
            out = out.loc[:, ~out.columns.duplicated()]
    except Exception:
        logger.exception("[RANKING SUMMARY] duplicate column guard failed")

    return out


def _safe_numeric(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    out = df.copy()
    for c in cols:
        if c in out.columns:
            try:
                out[c] = pd.to_numeric(out[c], errors="coerce")
            except Exception:
                logger.exception("[RANKING SUMMARY] numeric cast failed col=%s", c)
    return out


def _sort_if_possible(df: pd.DataFrame, by: list[str], ascending=True) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    cols = [c for c in by if c in df.columns]
    if not cols:
        return df.copy()

    try:
        return df.sort_values(cols, ascending=ascending, kind="stable").reset_index(drop=True)
    except Exception:
        logger.exception("[RANKING SUMMARY] sort failed by=%s", cols)
        return df.copy()


def _coerce_datetime_series(s: pd.Series) -> pd.Series:
    try:
        return pd.to_datetime(s, errors="coerce")
    except Exception:
        logger.exception("[RANKING SUMMARY] datetime coercion failed")
        return pd.to_datetime(pd.Series([], dtype="object"), errors="coerce")


def _coalesce_first_existing(df: pd.DataFrame, target: str, candidates: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    out = df.copy()
    if target in out.columns:
        return out

    for c in candidates:
        if c in out.columns:
            out[target] = out[c]
            logger.warning("[RANKING SUMMARY] alias used: %s -> %s", c, target)
            return out

    return out


def _ensure_snapshot_compatible_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out = _drop_duplicate_columns(out)

    if "snapshot_time" not in out.columns:
        for c in ("datetime", "end_time", "time", "timestamp", "snapshot_dt", "snapshot_datetime"):
            if c in out.columns:
                out["snapshot_time"] = out[c]
                logger.warning("[RANKING SUMMARY] alias used: %s -> snapshot_time", c)
                break

    if "symbolname" not in out.columns:
        for c in ("IssueName", "name", "stock_name", "issue_name", "symbol_name"):
            if c in out.columns:
                out["symbolname"] = out[c]
                logger.warning("[RANKING SUMMARY] alias used: %s -> symbolname", c)
                break

    if "rank_position" not in out.columns:
        for c in ("rank", "position", "rank_pos", "順位"):
            if c in out.columns:
                out["rank_position"] = out[c]
                logger.warning("[RANKING SUMMARY] alias used: %s -> rank_position", c)
                break

    out = _coalesce_first_existing(
        out,
        "price",
        [
            "close",
            "close_price",
            "current_price",
            "last_price",
            "price_current",
            "now_price",
            "約定価格",
            "値",
        ],
    )

    out = _coalesce_first_existing(
        out,
        "volume",
        [
            "current_volume",
            "cum_volume",
            "volume_total",
            "trading_volume",
            "turnover_volume",
            "出来高",
            "売買高",
            "vol",
        ],
    )

    out = _coalesce_first_existing(
        out,
        "volume_speed",
        [
            "vol_speed",
            "volume_velocity",
            "出来高速度",
            "velocity",
            "volume_rate",
        ],
    )

    out = _coalesce_first_existing(
        out,
        "change_rate",
        [
            "change_percent",
            "pct_change",
            "price_change_rate",
            "騰落率",
            "change_ratio",
            "rate_of_change",
        ],
    )

    if "ranking_score" not in out.columns:
        if "score" in out.columns:
            out["ranking_score"] = out["score"]
            logger.warning("[RANKING SUMMARY] alias used: score -> ranking_score")
        elif "total_score" in out.columns:
            out["ranking_score"] = out["total_score"]
            logger.warning("[RANKING SUMMARY] alias used: total_score -> ranking_score")
        else:
            out["ranking_score"] = 0.0

    for c, default in {
        "volume": 0.0,
        "volume_speed": 0.0,
        "change_rate": 0.0,
        "ranking_score": 0.0,
    }.items():
        if c not in out.columns:
            out[c] = default

    return out


def _normalize_snapshot_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = _ensure_snapshot_compatible_columns(df)

    required_min = {"symbol", "snapshot_time"}
    if not required_min.issubset(df.columns):
        missing = sorted(required_min - set(df.columns))
        logger.warning("[RANKING SUMMARY] required columns missing: %s", missing)
        return pd.DataFrame()

    df["symbol"] = df["symbol"].map(_normalize_symbol)
    df = df[df["symbol"] != ""].copy()

    if "symbolname" not in df.columns:
        df["symbolname"] = ""
    else:
        df["symbolname"] = df["symbolname"].map(_normalize_symbolname)

    if "market" not in df.columns:
        df["market"] = "ALL"
    else:
        df["market"] = df["market"].fillna("ALL").astype(str)

    if "rank_type" not in df.columns:
        df["rank_type"] = "UNKNOWN"
    else:
        df["rank_type"] = df["rank_type"].fillna("UNKNOWN").astype(str)

    if "source" not in df.columns:
        df["source"] = "RANKING"
    else:
        df["source"] = df["source"].fillna("RANKING").astype(str)

    df["snapshot_time"] = _coerce_datetime_series(df["snapshot_time"])
    df = df.dropna(subset=["snapshot_time"]).copy()

    if df.empty:
        return pd.DataFrame()

    df = _safe_numeric(df, NUMERIC_COLUMNS_SNAPSHOT)

    for c in ["price", "volume", "volume_speed", "change_rate", "ranking_score", "rank_position"]:
        if c in df.columns:
            try:
                df[c] = (
                    pd.to_numeric(df[c], errors="coerce")
                    .replace([float("inf"), float("-inf")], pd.NA)
                )
            except Exception:
                logger.exception("[RANKING SUMMARY] sanitize failed col=%s", c)

    if "price" not in df.columns:
        df["price"] = pd.NA
    if "volume" not in df.columns:
        df["volume"] = 0.0
    if "volume_speed" not in df.columns:
        df["volume_speed"] = 0.0
    if "change_rate" not in df.columns:
        df["change_rate"] = 0.0
    if "ranking_score" not in df.columns:
        df["ranking_score"] = 0.0
    if "rank_position" not in df.columns:
        df["rank_position"] = pd.NA

    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0).astype("float64")
    df["volume_speed"] = pd.to_numeric(df["volume_speed"], errors="coerce").fillna(0.0).astype("float64")
    df["change_rate"] = pd.to_numeric(df["change_rate"], errors="coerce").fillna(0.0).astype("float64")
    df["ranking_score"] = pd.to_numeric(df["ranking_score"], errors="coerce").fillna(0.0).astype("float64")
    df["rank_position"] = pd.to_numeric(df["rank_position"], errors="coerce")

    try:
        invalid_rank_count = int((df["rank_position"].fillna(0) <= 0).sum())
        if invalid_rank_count > 0:
            logger.warning("[RANKING SUMMARY] invalid rank_position<=0 -> NaN count=%d", invalid_rank_count)
        df.loc[df["rank_position"] <= 0, "rank_position"] = pd.NA
    except Exception:
        logger.exception("[RANKING SUMMARY] rank_position sanitize failed")

    keep_cols = [c for c in DEFAULT_KEEP_COLUMNS_SNAPSHOT if c in df.columns]
    df = df[keep_cols].copy()

    dedup_cols = [c for c in ["symbol", "snapshot_time", "rank_type", "market"] if c in df.columns]
    if dedup_cols:
        df = df.drop_duplicates(subset=dedup_cols, keep="last")

    df = _ensure_symbolname(df)
    df = _sort_if_possible(df, ["symbol", "snapshot_time"])
    return df


__all__ = [
    "NUMERIC_COLUMNS_SNAPSHOT",
    "DEFAULT_KEEP_COLUMNS_SNAPSHOT",
    "_to_dataframe",
    "_drop_duplicate_columns",
    "_safe_numeric",
    "_sort_if_possible",
    "_coerce_datetime_series",
    "_coalesce_first_existing",
    "_ensure_snapshot_compatible_columns",
    "_normalize_snapshot_df",
]