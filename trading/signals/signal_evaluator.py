# ============================================================
# File   : trading/signals/signal_evaluator.py
# Version: Ver1.0-PRODUCTION-FULL-COMPAT-BRIDGE
# ------------------------------------------------------------
# ✔ BUY / SHORT 条件評価の統一入口
# ✔ indicator_pipeline 強制通過
# ✔ summary / push / raw OHLC どれでも受ける
# ✔ recent_data を必ず指標付与後DataFrameに統一
# ✔ latest/curr/prev 生成
# ✔ buy_reasons / short_reasons / signal列付与
# ✔ 本番用完全版
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from trading.summary.pipeline.indicator_pipeline import run_indicator_pipeline
from trading.signals.conditions_buy import check_entry_conditions
from trading.signals.conditions_short import check_short_conditions

logger = logging.getLogger(__name__)


# ============================================================
# common
# ============================================================

def _ensure_dataframe(df: Any) -> pd.DataFrame:
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
            out = pd.DataFrame(df)
        except Exception:
            logger.exception("[signal_evaluator] dataframe conversion failed")
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in col if str(x) != ""])
                for col in out.columns.to_flat_index()
            ]
    except Exception:
        pass

    try:
        if out.columns.duplicated().any():
            out = out.loc[:, ~out.columns.duplicated()]
    except Exception:
        pass

    return out.reset_index(drop=True)


def _normalize_price_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    pairs = [
        ("open", "open_price"),
        ("high", "high_price"),
        ("low", "low_price"),
        ("close", "close_price"),
    ]

    for a, b in pairs:
        if a in df.columns and b not in df.columns:
            df[b] = df[a]
        if b in df.columns and a not in df.columns:
            df[a] = df[b]

    return df


def _normalize_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    try:
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        elif "date" in df.columns and "time" in df.columns:
            df["datetime"] = pd.to_datetime(
                df["date"].astype(str) + " " + df["time"].astype(str),
                errors="coerce",
            )
        elif "date" in df.columns:
            df["datetime"] = pd.to_datetime(df["date"], errors="coerce")
    except Exception:
        logger.exception("[signal_evaluator] datetime normalize failed")

    return df


def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = df.copy()

    try:
        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype(str).str.strip()
    except Exception:
        logger.exception("[signal_evaluator] symbol normalize failed")

    return df


def _preprocess(df: Any) -> pd.DataFrame:
    df = _ensure_dataframe(df)

    if df.empty:
        return df

    df = _normalize_price_columns(df)
    df = _normalize_datetime(df)
    df = _normalize_symbol(df)

    try:
        df.replace([np.inf, -np.inf], np.nan, inplace=True)
    except Exception:
        pass

    for c in [
        "open", "high", "low", "close",
        "open_price", "high_price", "low_price", "close_price",
        "volume", "tick_count",
    ]:
        if c in df.columns:
            try:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            except Exception:
                pass

    sort_cols = []
    if "symbol" in df.columns:
        sort_cols.append("symbol")
    if "datetime" in df.columns:
        sort_cols.append("datetime")

    if sort_cols:
        try:
            df = df.sort_values(sort_cols).reset_index(drop=True)
        except Exception:
            pass

    return df


def _row_dict(row: Optional[pd.Series]) -> Dict[str, Any]:
    if row is None:
        return {}
    try:
        return row.to_dict()
    except Exception:
        return {}


# ============================================================
# symbol evaluation
# ============================================================

def evaluate_symbol_signals(
    df: Any,
    *,
    interval: int | str = 1,
    recent_bars: int = 30,
) -> Dict[str, Any]:
    """
    単一symbol DataFrame想定。
    raw OHLC を受けても内部で indicator_pipeline を必ず通す。
    """
    try:
        df = _preprocess(df)

        if df.empty:
            return {
                "recent_data": pd.DataFrame(),
                "curr": {},
                "prev": {},
                "buy_reasons": [],
                "short_reasons": [],
            }

        # ★ 最重要: 条件判定前に必ず indicator 済みへ
        df = run_indicator_pipeline(df, interval=interval)
        df = _preprocess(df)

        if df.empty:
            return {
                "recent_data": pd.DataFrame(),
                "curr": {},
                "prev": {},
                "buy_reasons": [],
                "short_reasons": [],
            }

        recent = df.tail(recent_bars).copy().reset_index(drop=True)
        curr = _row_dict(recent.iloc[-1]) if len(recent) >= 1 else {}
        prev = _row_dict(recent.iloc[-2]) if len(recent) >= 2 else {}

        buy_reasons = check_entry_conditions(
            curr,
            prev,
            recent_data=recent,
            prev_state=None,
        )

        short_reasons = check_short_conditions(
            curr,
            prev,
            recent=recent,
            prev_state=None,
        )

        return {
            "recent_data": recent,
            "curr": curr,
            "prev": prev,
            "buy_reasons": buy_reasons if isinstance(buy_reasons, list) else [],
            "short_reasons": short_reasons if isinstance(short_reasons, list) else [],
        }

    except Exception:
        logger.exception("[signal_evaluator] evaluate_symbol_signals failed")
        return {
            "recent_data": pd.DataFrame(),
            "curr": {},
            "prev": {},
            "buy_reasons": [],
            "short_reasons": [],
        }


# ============================================================
# batch evaluation
# ============================================================

def attach_signal_columns(
    df: Any,
    *,
    interval: int | str = 1,
    recent_bars: int = 30,
    latest_only: bool = True,
) -> pd.DataFrame:
    """
    複数symbol DataFrameに対して signal列を付与。
    """
    try:
        df = _preprocess(df)

        if df.empty:
            return pd.DataFrame()

        # ★ ここでも先にindicator
        df = run_indicator_pipeline(df, interval=interval)
        df = _preprocess(df)

        if df.empty:
            return pd.DataFrame()

        if "symbol" not in df.columns:
            ev = evaluate_symbol_signals(df, interval=interval, recent_bars=recent_bars)
            out = df.tail(1).copy() if latest_only else df.copy()
            out["buy_reasons"] = [ev["buy_reasons"]] * len(out)
            out["short_reasons"] = [ev["short_reasons"]] * len(out)
            out["buy_reason_count"] = len(ev["buy_reasons"])
            out["short_reason_count"] = len(ev["short_reasons"])
            out["buy_signal"] = int(len(ev["buy_reasons"]) > 0)
            out["short_signal"] = int(len(ev["short_reasons"]) > 0)
            return out.reset_index(drop=True)

        signal_rows = []

        for symbol, g in df.groupby("symbol", sort=False):
            ev = evaluate_symbol_signals(g, interval=interval, recent_bars=recent_bars)

            base = g.tail(1).copy() if latest_only else g.copy()
            base["buy_reasons"] = [ev["buy_reasons"]] * len(base)
            base["short_reasons"] = [ev["short_reasons"]] * len(base)
            base["buy_reason_count"] = len(ev["buy_reasons"])
            base["short_reason_count"] = len(ev["short_reasons"])
            base["buy_signal"] = int(len(ev["buy_reasons"]) > 0)
            base["short_signal"] = int(len(ev["short_reasons"]) > 0)

            signal_rows.append(base)

        if not signal_rows:
            return pd.DataFrame()

        out = pd.concat(signal_rows, ignore_index=True, sort=False)

        try:
            out = out.sort_values(
                [c for c in ["symbol", "datetime"] if c in out.columns]
            ).reset_index(drop=True)
        except Exception:
            pass

        return out

    except Exception:
        logger.exception("[signal_evaluator] attach_signal_columns failed")
        return pd.DataFrame()