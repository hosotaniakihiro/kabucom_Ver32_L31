# ============================================================
# File   : core/global_context/summary_state.py
# Version: V39-FINAL-SUMMARY-STATE-ABSOLUTE-COMPAT
# ------------------------------------------------------------
# ✔ V38 全機能保持
# ✔ summary_cache 互換
# ✔ _summary_cache 互換
# ✔ _cache 旧直参照互換
# ✔ summary_cache_utils 完全通過
# ✔ thread safe 維持
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from threading import Lock
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class SummaryState:

    def __init__(self, max_rows: Dict[int, int] | None = None):

        self._lock = Lock()

        # interval -> symbol -> row(dict)
        self._summary: Dict[int, Dict[str, Dict[str, Any]]] = {
            1: {},
            3: {},
            5: {},
        }

        # merged summary (interval -> DataFrame)
        self._merged: Dict[int, pd.DataFrame] = {
            1: pd.DataFrame(),
            3: pd.DataFrame(),
            5: pd.DataFrame(),
        }

        # ★ 旧直参照互換
        self._cache = self._merged

        self._max_rows = max_rows or {}

    # --------------------------------------------------------
    # 互換プロパティ群
    # --------------------------------------------------------

    @property
    def summary_cache(self):
        return self._merged

    @property
    def _summary_cache(self):
        return self._merged

    # ========================================================
    # SET
    # ========================================================

    def set(self, interval: int, symbol: str, row: dict):
        try:
            if interval not in self._summary:
                return
            if not isinstance(row, dict):
                return

            with self._lock:
                self._summary[interval][str(symbol)] = dict(row)

        except Exception:
            logger.exception("SummaryState.set failed")

    # ========================================================
    # GET
    # ========================================================

    def get(self, interval: int, symbol: str) -> Optional[dict]:
        try:
            row = self._summary.get(interval, {}).get(str(symbol))
            return dict(row) if isinstance(row, dict) else None
        except Exception:
            return None

    # ========================================================
    # SNAPSHOT
    # ========================================================

    def snapshot_interval(self, interval: int) -> Dict[str, dict]:
        try:
            with self._lock:
                return {
                    k: dict(v)
                    for k, v in self._summary.get(interval, {}).items()
                }
        except Exception:
            return {}

    # ========================================================
    # MERGED
    # ========================================================

    def set_merged(self, interval: int, df: pd.DataFrame):
        try:
            if not isinstance(df, pd.DataFrame):
                return
            with self._lock:
                self._merged[interval] = df.copy()
        except Exception:
            logger.exception("SummaryState.set_merged failed")

    def get_merged(self, interval: int) -> pd.DataFrame:
        try:
            df = self._merged.get(interval)
            return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
        except Exception:
            return pd.DataFrame()

    # ========================================================
    # TO DF
    # ========================================================

    def to_dataframe(self, interval: int) -> pd.DataFrame:
        try:
            with self._lock:
                data = self._summary.get(interval, {})
                if not data:
                    return pd.DataFrame()

                df = pd.DataFrame(data.values())

                limit = self._max_rows.get(interval)
                if limit and len(df) > limit:
                    df = df.tail(limit).reset_index(drop=True)

                return df
        except Exception:
            return pd.DataFrame()

    # ========================================================
    # CLEAR
    # ========================================================

    def clear(self):
        try:
            with self._lock:
                for k in self._summary:
                    self._summary[k].clear()
                for k in self._merged:
                    self._merged[k] = pd.DataFrame()
        except Exception:
            logger.exception("SummaryState.clear failed")

    # ============================================================
    # PUSH DF 管理（旧互換用）
    # ============================================================

    def set_push_df(self, df):
        with self._lock:
            self._push_df = df

    def get_push_df(self):
        with self._lock:
            return getattr(self, "_push_df", None)

    # ========================================================
    # BACKWARD COMPATIBILITY (V38以前互換)
    # ========================================================

    def update(self, interval: int, df: pd.DataFrame):
        """
        旧API互換:
        update(interval, df)

        dfをmergedとして保存し、
        さらに各symbol行を_summaryへ展開する
        """
        try:
            if not isinstance(df, pd.DataFrame):
                return

            # merged保存
            self.set_merged(interval, df)

            # symbol単位展開（旧挙動互換）
            if "symbol" in df.columns:
                for _, row in df.iterrows():
                    symbol = str(row["symbol"])
                    self.set(interval, symbol, row.to_dict())

        except Exception:
            logger.exception("SummaryState.update failed")