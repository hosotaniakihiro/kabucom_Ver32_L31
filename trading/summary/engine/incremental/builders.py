# ============================================================
# File   : trading/summary/engine/incremental/builders.py
# Version: Ver1.0-INCREMENTAL-BUILDERS
# ------------------------------------------------------------
# ✔ 1分足生成
# ✔ 3分足 / 5分足リサンプル
# ✔ 元 pipeline.py から builder責務を分離
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from trading.summary.calculator.summary_pipeline import calculate_summary
from trading.summary.engine.guards.enhance_guard import enhance_guard
from trading.summary.engine.processors.resample import safe_resample

from .common import log_df_state

logger = logging.getLogger(__name__)


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time"):
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce")
            try:
                out[c] = out[c].dt.tz_localize(None)
            except Exception:
                pass
    if "datetime" not in out.columns:
        for c in ("dt", "timestamp", "end_time", "snapshot_time"):
            if c in out.columns:
                out["datetime"] = pd.to_datetime(out[c], errors="coerce")
                try:
                    out["datetime"] = out["datetime"].dt.tz_localize(None)
                except Exception:
                    pass
                break
    return out


def build_1m_from_push(df_push: pd.DataFrame) -> pd.DataFrame:
    if df_push is None or df_push.empty:
        return pd.DataFrame()

    symbols = df_push["symbol"].astype(str).unique().tolist() if "symbol" in df_push.columns else []

    try:
        df_1m = calculate_summary(
            df_push=df_push,
            symbols=symbols,
            start_time=None,
            end_time=None,
        )
    except TypeError:
        try:
            df_1m = calculate_summary(df_push)
        except Exception:
            logger.exception("[INCREMENTAL SUMMARY] calculate_summary fallback failed")
            df_1m = pd.DataFrame()
    except Exception:
        logger.exception("[INCREMENTAL SUMMARY] calculate_summary failed")
        df_1m = pd.DataFrame()

    df_1m = enhance_guard(df_1m)

    if df_1m.empty:
        df_1m = df_push.copy()
        if "close" in df_1m.columns:
            df_1m["close_price"] = df_1m["close"]
            df_1m["open_price"] = df_1m.get("open", df_1m["close"])
            df_1m["high_price"] = df_1m.get("high", df_1m["close"])
            df_1m["low_price"] = df_1m.get("low", df_1m["close"])

    df_1m = _ensure_datetime(df_1m)
    log_df_state("1m-after-calculate", df_1m)
    return df_1m


def build_target_interval_df(df_push: pd.DataFrame, interval: int) -> pd.DataFrame:
    interval = int(interval)

    df_1m = build_1m_from_push(df_push)
    if df_1m.empty:
        return pd.DataFrame()

    if interval == 1:
        return df_1m

    if interval == 3:
        df = safe_resample(df_1m, 3)
        df = _ensure_datetime(df)
        log_df_state("3m-after-resample", df)
        return df

    if interval == 5:
        df = safe_resample(df_1m, 5)
        df = _ensure_datetime(df)
        log_df_state("5m-after-resample", df)
        return df

    logger.warning("[INCREMENTAL SUMMARY] unsupported interval=%s", interval)
    return pd.DataFrame()