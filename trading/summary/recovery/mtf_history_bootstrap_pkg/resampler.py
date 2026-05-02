# ============================================================
# File   : trading/summary/recovery/mtf_history_bootstrap_pkg/resampler.py
# Version: PRODUCTION-STABLE-REV1.0-RESAMPLER
# ------------------------------------------------------------
# 【概要】
#   1分足履歴から 3分足 / 5分足を再構築
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from .dataframe_utils import (
    normalize_summary_df,
    attach_date_time_columns,
)
from .datetime_guard import drop_future_datetime_rows

logger = logging.getLogger(__name__)


def rebuild_higher_tf_from_1m_history(df_1m: pd.DataFrame, interval: int) -> pd.DataFrame:
    src = normalize_summary_df(df_1m)
    src = drop_future_datetime_rows(src, interval=1, label="rebuild_source_1m")

    if src.empty:
        return pd.DataFrame()

    interval = int(interval)

    if interval <= 1:
        out = src.copy()
        out["source"] = "mtf_history_bootstrap_1min_history"
        out["interval"] = 1
        out = attach_date_time_columns(out, interval=1)
        out = drop_future_datetime_rows(out, interval=1, label="rebuild_1m")

        try:
            out["symbol_hist_len"] = out.groupby("symbol")["datetime"].transform("nunique").astype(int)
        except Exception:
            out["symbol_hist_len"] = 0

        return out

    frames: list[pd.DataFrame] = []

    try:
        for symbol, g in src.groupby("symbol", sort=False):
            g = g.sort_values("datetime", kind="stable").copy()
            if g.empty:
                continue

            g = g.set_index("datetime")

            agg = {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }

            if "vwap" in g.columns:
                agg["vwap"] = "last"

            if "symbolname" in g.columns:
                agg["symbolname"] = "last"

            rule = f"{interval}min"

            r = g.resample(
                rule,
                label="right",
                closed="right",
                origin="start_day",
            ).agg(agg)

            r = r.dropna(subset=["close"]).copy()
            if r.empty:
                continue

            r["symbol"] = symbol

            if "symbolname" not in r.columns:
                try:
                    r["symbolname"] = g["symbolname"].dropna().iloc[-1]
                except Exception:
                    r["symbolname"] = ""

            r = r.reset_index().rename(columns={"datetime": "datetime"})

            for c in ("open", "high", "low"):
                r[c] = pd.to_numeric(r[c], errors="coerce").combine_first(
                    pd.to_numeric(r["close"], errors="coerce")
                )

            r["source"] = f"summary_recovery_resample_{interval}m_from_1m_history"
            r["interval"] = interval
            frames.append(r)

    except Exception:
        logger.exception("[MTF HISTORY BOOTSTRAP] resample failed interval=%s", interval)
        return pd.DataFrame()

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True, sort=False)
    out = normalize_summary_df(out)
    out = attach_date_time_columns(out, interval=interval)
    out = drop_future_datetime_rows(out, interval=interval, label=f"rebuild_higher_tf_{interval}")

    try:
        out["symbol_hist_len"] = out.groupby("symbol")["datetime"].transform("nunique").astype(int)
    except Exception:
        out["symbol_hist_len"] = 0

    try:
        hist = out.groupby("symbol")["datetime"].nunique()
        logger.info(
            "[MTF HISTORY BOOTSTRAP] rebuild higher tf done interval=%s rows=%s symbols=%s hist_min=%s hist_median=%.1f hist_max=%s dt_min=%s dt_max=%s",
            interval,
            len(out),
            out["symbol"].nunique() if "symbol" in out.columns else 0,
            int(hist.min()) if not hist.empty else 0,
            float(hist.median()) if not hist.empty else 0.0,
            int(hist.max()) if not hist.empty else 0,
            out["datetime"].min() if "datetime" in out.columns and not out.empty else None,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
        )
    except Exception:
        logger.debug("[MTF HISTORY BOOTSTRAP] rebuild profile failed interval=%s", interval, exc_info=True)

    return out


__all__ = [
    "rebuild_higher_tf_from_1m_history",
]