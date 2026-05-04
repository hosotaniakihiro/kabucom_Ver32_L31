from __future__ import annotations
import logging, time
from typing import Any
import pandas as pd
from .constants import YAHOO_SUMMARY_INTERVALS
logger = logging.getLogger(__name__)

def elapsed(start_ts: float) -> float:
    return max(time.time() - start_ts, 0.0)

def log_step(label: str, start_ts: float, **kwargs: Any) -> None:
    meta = " ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.info("[YAHOO COMPLEMENT] %s elapsed=%.3fs%s%s", label, elapsed(start_ts), " " if meta else "", meta)

def log_df_profile(label: str, df: pd.DataFrame | None) -> None:
    if df is None:
        logger.info("[YAHOO COMPLEMENT] %s df=None", label); return
    if not isinstance(df, pd.DataFrame):
        logger.info("[YAHOO COMPLEMENT] %s not_dataframe type=%s", label, type(df).__name__); return
    if df.empty:
        logger.info("[YAHOO COMPLEMENT] %s rows=0 cols=%s", label, len(df.columns)); return
    cols = list(df.columns)
    unique = df["symbol"].astype(str).nunique() if "symbol" in df.columns else -1
    dt_min = pd.to_datetime(df["datetime"], errors="coerce").min() if "datetime" in df.columns else None
    dt_max = pd.to_datetime(df["datetime"], errors="coerce").max() if "datetime" in df.columns else None
    logger.info("[YAHOO COMPLEMENT] %s rows=%s cols=%s unique_symbols=%s dt_min=%s dt_max=%s sample_cols=%s", label, len(df), len(cols), unique, dt_min, dt_max, cols[:20])

def log_pipeline_result(result_map: dict, *, label: str) -> None:
    if not isinstance(result_map, dict) or not result_map:
        logger.warning("%s: summary pipeline empty", label); return
    nonempty = {k: v for k, v in result_map.items() if isinstance(v, pd.DataFrame) and not v.empty}
    if not nonempty:
        logger.warning("%s: MTF summary pipeline empty", label); return
    try:
        latest = sorted(nonempty.keys())[-1]
        df = nonempty[latest]
        logger.info("%s complete intervals=%s nonempty=%s latest_interval=%s rows=%d symbols=%d range=%s→%s", label, YAHOO_SUMMARY_INTERVALS, list(nonempty.keys()), latest, len(df), df["symbol"].nunique() if "symbol" in df.columns else 0, df["datetime"].min() if "datetime" in df.columns else None, df["datetime"].max() if "datetime" in df.columns else None)
    except Exception:
        logger.info("%s complete intervals=%s nonempty=%s", label, YAHOO_SUMMARY_INTERVALS, list(nonempty.keys()))
__all__ = ["elapsed", "log_step", "log_df_profile", "log_pipeline_result"]
