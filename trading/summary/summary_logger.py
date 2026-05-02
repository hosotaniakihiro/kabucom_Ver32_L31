# ============================================================
# File   : trading/summary/summary_logger.py
# Ver1.0-PRODUCTION-STABLE
# ------------------------------------------------------------
# ✔ 3分足 / 5分足バー確定ログ
# ✔ 重複ログ防止
# ✔ datetime安全
# ✔ 本番例外耐性
# ============================================================

from __future__ import annotations
import logging
import pandas as pd

logger = logging.getLogger(__name__)


class SummaryLogger:

    def __init__(self):
        self.last_bar_time = {}

    def log_if_new(self, df: pd.DataFrame, interval: int):

        try:

            if df is None or df.empty:
                return

            if "datetime" not in df.columns:
                return

            latest = df["datetime"].max()

            prev = self.last_bar_time.get(interval)

            if prev == latest:
                return

            self.last_bar_time[interval] = latest

            logger.info(
                "[%sMIN CONFIRMED] %s",
                interval,
                latest
            )

        except Exception:
            logger.exception("[SUMMARY LOGGER ERROR]")


summary_logger = SummaryLogger()