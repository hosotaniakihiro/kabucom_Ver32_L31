# ============================================================
# File   : trading/summary/summary_boundary_logger.py
# Ver    : 1.0-FINAL-BOUNDARY-VIS
# ------------------------------------------------------------
# ✔ realtime / confirmed の境界を可視化
# ✔ 未確定足が混入していないか即判断可能
# ============================================================

import logging
import pandas as pd

logger = logging.getLogger(__name__)


def log_summary_boundary(
    *,
    label: str,
    df_realtime: pd.DataFrame | None,
    df_confirmed: pd.DataFrame | None,
):
    """
    realtime と confirmed の境界をログ出力
    """

    def _max(df, col):
        if df is None or df.empty or col not in df.columns:
            return None
        return df[col].max()

    rt_max = _max(df_realtime, "datetime")
    cf_max = _max(df_confirmed, "end_time")

    logger.info(
        "[SUMMARY_BOUNDARY] %s realtime_max=%s confirmed_max=%s",
        label,
        rt_max,
        cf_max,
    )
