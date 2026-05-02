# ============================================================
# File   : core/global_context/ranking_state.py
# Version: V39-PRODUCTION-RANKING-STATE-MERGED-COMPAT
# ------------------------------------------------------------
# ✔ snapshotバグ修正保持
# ✔ set_latest_snapshot重複削除保持
# ✔ snapshot_time typo修正保持
# ✔ thread safe
# ✔ backward compatible
# ✔ ranking merged summary 追加
# ✔ interval別 merged DataFrame 保持
# ✔ copy返却で安全化
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from threading import Lock
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class RankingState:

    def __init__(self):

        self._lock = Lock()

        self._ranking: Dict[str, Dict[str, Any]] = {}

        self._latest_snapshot: Dict[str, dict] = {}

        self._latest_snapshot_time = None

        # ----------------------------------------------------
        # merged summary（新設）
        # interval -> DataFrame
        # ----------------------------------------------------
        self._merged: Dict[int, pd.DataFrame] = {
            1: pd.DataFrame(),
            3: pd.DataFrame(),
            5: pd.DataFrame(),
        }

    # ========================================================
    # internal
    # ========================================================

    @staticmethod
    def _normalize_interval(interval: int | str = 1) -> int:
        try:
            s = str(interval).strip().lower().replace(" ", "")
            if s.endswith("min"):
                s = s[:-3]
            n = int(s)
            return n if n > 0 else 1
        except Exception:
            logger.exception("RankingState._normalize_interval failed interval=%r", interval)
            return 1

    # ========================================================
    # SET
    # ========================================================

    def set(self, symbol: str, row: dict):

        try:

            if not isinstance(row, dict):
                return

            with self._lock:

                self._ranking[str(symbol)] = dict(row)

        except Exception:

            logger.exception("RankingState.set failed")

    # ========================================================
    # BULK SET
    # ========================================================

    def set_bulk(self, rows: Dict[str, dict]):

        try:

            if not isinstance(rows, dict):
                return

            with self._lock:

                for symbol, row in rows.items():

                    if isinstance(row, dict):

                        self._ranking[str(symbol)] = dict(row)

        except Exception:

            logger.exception("RankingState.set_bulk failed")

    # ========================================================
    # GET
    # ========================================================

    def get(self, symbol: str) -> Optional[dict]:

        try:

            row = self._ranking.get(str(symbol))

            return dict(row) if isinstance(row, dict) else None

        except Exception:

            return None

    # ========================================================
    # SYMBOL LIST
    # ========================================================

    def get_symbols(self) -> List[str]:

        try:

            return list(self._ranking.keys())

        except Exception:

            return []

    # ========================================================
    # REMOVE
    # ========================================================

    def remove(self, symbol: str):

        try:

            with self._lock:

                s = str(symbol)
                self._ranking.pop(s, None)

        except Exception:

            logger.exception("RankingState.remove failed")

    # ========================================================
    # SNAPSHOT
    # ========================================================

    def snapshot(self) -> Dict[str, dict]:

        try:

            with self._lock:

                return {
                    k: dict(v)
                    for k, v in self._ranking.items()
                }

        except Exception:

            return {}

    # ========================================================
    # SIZE
    # ========================================================

    def size(self) -> int:

        try:

            return len(self._ranking)

        except Exception:

            return 0

    # ========================================================
    # LATEST SNAPSHOT
    # ========================================================

    def set_latest_snapshot(self, snapshot, snapshot_time=None):

        try:

            with self._lock:

                if snapshot is None:
                    self._latest_snapshot = {}

                elif isinstance(snapshot, dict):
                    self._latest_snapshot = dict(snapshot)

                else:
                    try:
                        self._latest_snapshot = dict(snapshot)
                    except Exception:
                        self._latest_snapshot = {}

                self._latest_snapshot_time = snapshot_time

        except Exception:

            logger.exception("RankingState.set_latest_snapshot failed")

    def get_latest_snapshot(self):

        try:
            with self._lock:
                return dict(self._latest_snapshot)
        except Exception:
            return {}

    def get_latest_snapshot_time(self):

        try:

            return self._latest_snapshot_time

        except Exception:

            return None

    # ========================================================
    # MERGED SUMMARY（新設）
    # ========================================================

    @property
    def ranking_summary_cache(self):
        """
        互換用途:
        interval -> DataFrame
        """
        return self._merged

    def set_merged(self, interval: int | str, df: pd.DataFrame):
        try:
            tf = self._normalize_interval(interval)
            if not isinstance(df, pd.DataFrame):
                df = pd.DataFrame()

            with self._lock:
                self._merged[tf] = df.copy()

            logger.info(
                "[RankingState] set_merged tf=%s rows=%s cols=%s",
                tf,
                len(df),
                list(df.columns)[:20],
            )

        except Exception:
            logger.exception("RankingState.set_merged failed interval=%r", interval)

    def get_merged(self, interval: int | str) -> pd.DataFrame:
        try:
            tf = self._normalize_interval(interval)
            with self._lock:
                df = self._merged.get(tf)
                return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
        except Exception:
            logger.exception("RankingState.get_merged failed interval=%r", interval)
            return pd.DataFrame()

    def clear_merged(self, interval: int | str | None = None):
        try:
            with self._lock:
                if interval is None:
                    for k in list(self._merged.keys()):
                        self._merged[k] = pd.DataFrame()
                    logger.info("[RankingState] clear_merged all")
                    return

                tf = self._normalize_interval(interval)
                self._merged[tf] = pd.DataFrame()
                logger.info("[RankingState] clear_merged tf=%s", tf)

        except Exception:
            logger.exception("RankingState.clear_merged failed interval=%r", interval)

    # ========================================================
    # CLEAR
    # ========================================================

    def clear(self):

        try:

            with self._lock:

                self._ranking.clear()

                self._latest_snapshot = {}

                self._latest_snapshot_time = None

                for k in list(self._merged.keys()):
                    self._merged[k] = pd.DataFrame()

        except Exception:

            logger.exception("RankingState.clear failed")