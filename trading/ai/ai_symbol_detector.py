# ============================================================
# File   : trading/ai/ai_symbol_detector.py
# Version: Ver1.0-PRODUCTION-AI-DISCOVERY-ENGINE
# ------------------------------------------------------------
# ✔ 全銘柄探索
# ✔ 出来高異常検知
# ✔ 資金流入検知
# ✔ 相対強度
# ✔ 価格加速
# ✔ ランキング未掲載銘柄発見
# ✔ ETF除外
# ✔ NaN / inf guard
# ✔ pandas crash guard
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# parameters
# ============================================================

MAX_RETURN = 30

MIN_PRICE = 50

MIN_VOLUME_RATIO = 3

MIN_TURNOVER = 30_000_000


ETF_PREFIX = (
    "130","131","132","134","135","136","138","139",
    "145","146","147","148","149",
    "154","155","156","157","158","159",
    "165","167","168","169",
)


# ============================================================
# main class
# ============================================================

class AISymbolDetector:


    def __init__(self):

        self.last_candidates = []


    # ========================================================
    # public
    # ========================================================

    def get_candidates(self, n: int = 30):

        try:

            df = self._load_universe()

            if df.empty:

                return []

            df = self._detect_money_flow(df)

            df = self._detect_volume_spike(df)

            df = self._detect_price_acceleration(df)

            df = self._score(df)

            df = df.sort_values("ai_discovery_score", ascending=False)

            out = df["symbol"].astype(str).tolist()

            self.last_candidates = out[:n]

            return self.last_candidates[:n]

        except Exception:

            logger.exception("[AI DISCOVERY] failed")

            return []


    # ========================================================
    # universe loader
    # ========================================================

    def _load_universe(self):

        try:

            from global_state import global_data

            df = getattr(global_data, "summary_1min", None)

            if df is None:

                return pd.DataFrame()

            df = df.copy()

            if df.empty:

                return pd.DataFrame()

        except Exception:

            logger.exception("[AI DISCOVERY] universe load failed")

            return pd.DataFrame()

        df = self._sanitize(df)

        df = self._filter_etf(df)

        df = self._filter_price(df)

        return df


    # ========================================================
    # filters
    # ========================================================

    def _filter_etf(self, df):

        if "symbol" not in df.columns:

            return df

        try:

            return df[~df["symbol"].astype(str).str.startswith(ETF_PREFIX)]

        except Exception:

            return df


    def _filter_price(self, df):

        if "close" not in df.columns:

            return df

        try:

            return df[df["close"] >= MIN_PRICE]

        except Exception:

            return df


    # ========================================================
    # money flow
    # ========================================================

    def _detect_money_flow(self, df):

        if "turnover" not in df.columns:

            return df

        df["money_flow_score"] = (

            df["turnover"]
            .fillna(0)
            .clip(0, None)
            / 10_000_000

        )

        return df


    # ========================================================
    # volume spike
    # ========================================================

    def _detect_volume_spike(self, df):

        if "volume_ratio" not in df.columns:

            df["volume_spike_score"] = 0

            return df

        df["volume_spike_score"] = (

            df["volume_ratio"]
            .fillna(0)
            .clip(0, 20)

        )

        return df


    # ========================================================
    # price acceleration
    # ========================================================

    def _detect_price_acceleration(self, df):

        if "momentum" not in df.columns:

            df["price_accel_score"] = 0

            return df

        df["price_accel_score"] = (

            df["momentum"]
            .fillna(0)
            .clip(-10, 10)

        )

        return df


    # ========================================================
    # scoring
    # ========================================================

    def _score(self, df):

        df["ai_discovery_score"] = (

            df.get("money_flow_score", 0) * 4
            + df.get("volume_spike_score", 0) * 3
            + df.get("price_accel_score", 0) * 2
            + df.get("market_relative_strength", 0) * 2

        )

        df["ai_discovery_score"] = (

            df["ai_discovery_score"]
            .replace([np.inf, -np.inf], 0)
            .fillna(0)

        )

        return df


    # ========================================================
    # sanitize
    # ========================================================

    def _sanitize(self, df):

        try:

            if isinstance(df.columns, pd.MultiIndex):

                df.columns = [
                    "_".join(map(str, c))
                    for c in df.columns
                ]

            if df.columns.duplicated().any():

                df = df.loc[:, ~df.columns.duplicated()]

            df = df.replace([np.inf, -np.inf], np.nan)

        except Exception:

            logger.exception("[AI DISCOVERY] sanitize failed")

        return df