"""
============================================================
htf_logger.py
Higher Timeframe Summary Logger
------------------------------------------------------------
✔ 3分 / 5分足確定ログ
✔ 重複ログ防止
✔ summary表示
✔ symbolname表示対応
✔ NaN / inf防御
✔ Discord通知拡張可能
✔ 本番安定版
============================================================
"""

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# Logger Class
# ============================================================

class HTFSummaryLogger:

    def __init__(self):

        # 最後に出したログ
        self.last_logged = {
            3: None,
            5: None
        }

    # ========================================================
    # Utility
    # ========================================================

    def _safe_float(self, v):

        try:

            x = float(v)

            if np.isnan(x) or np.isinf(x):
                return 0.0

            return x

        except Exception:

            return 0.0

    # ========================================================
    # Main logger
    # ========================================================

    def log_if_new(self, df: pd.DataFrame, interval: int):

        """
        新しいバーだけログ出す
        """

        try:

            if df is None or df.empty:
                return

            last = df.iloc[-1]

            ts = last.get("datetime")

            if ts is None:
                return

            # -----------------------------------------
            # 重複防止
            # -----------------------------------------

            if ts == self.last_logged.get(interval):
                return

            self.last_logged[interval] = ts

            symbol = last.get("symbol")
            symbolname = last.get("symbolname", symbol)

            o = self._safe_float(last.get("open_price"))
            h = self._safe_float(last.get("high_price"))
            l = self._safe_float(last.get("low_price"))
            c = self._safe_float(last.get("close_price"))
            v = self._safe_float(last.get("volume"))

            logger.info(
                "[SUMMARY %sm CONFIRMED] %s (%s) %s O=%.2f H=%.2f L=%.2f C=%.2f V=%.0f",
                interval,
                symbol,
                symbolname,
                ts,
                o,
                h,
                l,
                c,
                v,
            )

        except Exception:

            logger.exception("[HTF logger failed]")


# ============================================================
# Singleton
# ============================================================

htf_logger = HTFSummaryLogger()