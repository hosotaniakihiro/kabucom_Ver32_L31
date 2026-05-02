# ============================================================
# File   : trading/summary/recovery/bootstrap_transforms.py
# Ver    : PRODUCTION-STABLE-REV1.0-BOOTSTRAP-TRANSFORMS
# ------------------------------------------------------------
# ✔ interval 名変換
# ✔ raw OHLCV へ indicator/scoring を適用
# ✔ higher TF の last_dt 同値バー保持
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from trading.summary.recovery.helpers import normalize_datetime_columns
from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.scoring.core.scoring_core import scoring_main

logger = logging.getLogger(__name__)


def interval_name(interval: int) -> str:
    try:
        interval = int(interval)
    except Exception:
        interval = 1

    if interval == 1:
        return "1min"
    if interval == 3:
        return "3min"
    if interval == 5:
        return "5min"
    return f"{interval}min"


def apply_indicators_and_scoring(
    df: pd.DataFrame,
    *,
    interval: int,
    label: str = "",
) -> pd.DataFrame:
    """
    raw OHLCV に対して indicator + scoring を適用する。
    ここでは finalize_for_upsert は行わない。
    """
    try:
        out = normalize_datetime_columns(df, interval=int(interval))
        if out.empty:
            logger.info(
                "[summary_recovery] indicators/scoring skipped interval=%s label=%s reason=empty",
                interval,
                label,
            )
            return out

        i_name = interval_name(int(interval))

        out = add_all_indicators(out, interval=i_name)
        out = normalize_datetime_columns(out, interval=int(interval))

        if out.empty:
            logger.info(
                "[summary_recovery] indicators produced empty df interval=%s label=%s",
                i_name,
                label,
            )
            return out

        out = scoring_main(out, interval=i_name, force=True)
        out = normalize_datetime_columns(out, interval=int(interval))

        logger.info(
            "[summary_recovery] indicators+scoring done interval=%s label=%s rows=%d symbols=%d latest_dt=%s",
            i_name,
            label,
            len(out),
            int(out["symbol"].nunique()) if "symbol" in out.columns and not out.empty else 0,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
        )
        return out

    except Exception:
        logger.exception(
            "[summary_recovery] indicators/scoring failed interval=%s label=%s",
            interval,
            label,
        )
        return normalize_datetime_columns(df, interval=int(interval))


def keep_newer_or_equal_last_bar(
    df: pd.DataFrame,
    last_dt,
    interval: int,
) -> pd.DataFrame:
    """
    higher TF の最終確定バーが last_dt と同値になるケースを落とさない。
    """
    out = df.copy()
    if out.empty or "datetime" not in out.columns or last_dt is None:
        return out

    try:
        dt_s = pd.to_datetime(out["datetime"], errors="coerce")
        last_dt = pd.to_datetime(last_dt, errors="coerce")
        if pd.isna(last_dt):
            return out

        newer = out[dt_s > last_dt].copy()
        equal_rows = out[dt_s == last_dt].copy()

        if not equal_rows.empty and "symbol" in equal_rows.columns:
            equal_rows = (
                equal_rows.sort_values(["symbol", "datetime"], ascending=[True, False], kind="stable")
                .drop_duplicates(subset=["symbol"], keep="first")
                .reset_index(drop=True)
            )

        out = pd.concat([newer, equal_rows], ignore_index=True)
        if "symbol" in out.columns and "datetime" in out.columns:
            out = out.sort_values(["symbol", "datetime"], ascending=[True, True], kind="stable")
        out = out.reset_index(drop=True)

        logger.info(
            "[summary_recovery] keep_newer_or_equal_last_bar interval=%s last_dt=%s input_rows=%s output_rows=%s",
            interval,
            last_dt,
            len(df),
            len(out),
        )
        return out

    except Exception:
        logger.exception(
            "[summary_recovery] keep_newer_or_equal_last_bar failed interval=%s last_dt=%s",
            interval,
            last_dt,
        )
        return df