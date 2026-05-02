# ============================================================
# File   : AI/control/market_regime_model.py
# Version: V33-PRODUCTION-ULTRA-STABLE-REGIME-MODEL
# ------------------------------------------------------------
# ✔ V32 完全互換（削除ゼロ）
# ✔ ヒステリシス追加（regime安定化）
# ✔ confidence追加
# ✔ スムージング（EMA）
# ✔ スコア化対応（将来AI用）
# ✔ 完全例外安全
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from threading import Lock
from typing import Optional

import numpy as np

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from config.paths import get_path, ensure_dirs
from database.regime_models import BaseRegime, RegimeHistory

logger = logging.getLogger(__name__)


# ============================================================
# SQLite Engine
# ============================================================

def _create_engine_sqlite():

    db_path = get_path("regime_db")

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.close()

    return engine


# ============================================================
# helpers
# ============================================================

def _safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def _ema(prev: float, new: float, alpha: float = 0.2) -> float:
    return prev * (1 - alpha) + new * alpha


# ============================================================
# MarketRegimeModel
# ============================================================

class MarketRegimeModel:

    """
    Regime:

        0 = TREND_UP
        1 = TREND_DOWN
        2 = RANGE
        3 = VOLATILE
        4 = CRASH
    """

    def __init__(
        self,
        engine=None,
        cache_seconds: int = 30,
        hysteresis: float = 0.2,   # 🔥 安定化
    ):

        ensure_dirs()

        self.engine = engine or _create_engine_sqlite()

        BaseRegime.metadata.create_all(self.engine)

        self._cache_seconds = cache_seconds
        self._lock = Lock()

        self._cached_regime: Optional[int] = None
        self._last_update: Optional[dt.datetime] = None
        self._last_persisted_regime: Optional[int] = None

        # 🔥 追加
        self._last_score = None
        self._hysteresis = hysteresis
        self._ema_state = {}

        self._confidence: float = 0.0
        self._ai_model = None

    # ========================================================
    # Public API
    # ========================================================

    def predict(self, market_state: dict) -> int:

        try:
            now = dt.datetime.utcnow()

            with self._lock:

                if (
                    self._cached_regime is not None
                    and self._last_update
                    and (now - self._last_update).total_seconds()
                    < self._cache_seconds
                ):
                    return self._cached_regime

                # 🔥 スムージング
                s = self._smooth_state(market_state)

                # 🔥 スコア化
                score_map = self._score_regime(s)

                regime = self._select_regime(score_map)

                self._confidence = max(score_map.values())

                self._cached_regime = regime
                self._last_update = now

            self._persist(regime, s)

            return regime

        except Exception:
            logger.exception("MarketRegimeModel.predict failed")
            return 2

    # ========================================================
    # smoothing
    # ========================================================

    def _smooth_state(self, s: dict) -> dict:

        out = {}

        for k, v in s.items():
            v = _safe_float(v)

            if k not in self._ema_state:
                self._ema_state[k] = v

            self._ema_state[k] = _ema(self._ema_state[k], v)

            out[k] = self._ema_state[k]

        return out

    # ========================================================
    # score-based regime
    # ========================================================

    def _score_regime(self, s: dict) -> dict:

        nikkei_slope = _safe_float(s.get("nikkei_slope"))
        breadth = _safe_float(s.get("breadth_ratio"), 0.5)
        volatility = _safe_float(s.get("volatility"), 1.0)
        index_atr = max(_safe_float(s.get("index_atr"), 1.0), 1e-6)

        # normalize
        slope_norm = nikkei_slope / index_atr

        scores = {}

        # TREND_UP
        scores[0] = (
            max(0, slope_norm)
            + max(0, breadth - 0.5) * 2
        )

        # TREND_DOWN
        scores[1] = (
            max(0, -slope_norm)
            + max(0, 0.5 - breadth) * 2
        )

        # RANGE
        scores[2] = (
            1.0 - abs(slope_norm) * 0.5
            + (1.0 - abs(breadth - 0.5) * 2)
        )

        # VOLATILE
        scores[3] = volatility

        # CRASH
        scores[4] = max(0, -slope_norm * 2)

        return scores

    # ========================================================
    # regime selection（ヒステリシス）
    # ========================================================

    def _select_regime(self, score_map: dict) -> int:

        best_regime = max(score_map, key=score_map.get)
        best_score = score_map[best_regime]

        # 🔥 ヒステリシス
        if self._last_score is not None and self._cached_regime is not None:

            prev_score = score_map.get(self._cached_regime, 0)

            if best_score < prev_score + self._hysteresis:
                return self._cached_regime

        self._last_score = best_score

        return best_regime

    # ========================================================
    # persist
    # ========================================================

    def _persist(self, regime: int, s: dict):

        try:

            if regime == self._last_persisted_regime:
                return

            with Session(self.engine) as session:

                session.add(
                    RegimeHistory(
                        timestamp=dt.datetime.utcnow(),
                        regime=regime,
                        nikkei_slope=_safe_float(s.get("nikkei_slope")),
                        breadth_ratio=_safe_float(s.get("breadth_ratio"), 0.5),
                        volatility=_safe_float(s.get("volatility"), 1.0),
                    )
                )

                session.commit()

                self._last_persisted_regime = regime

        except Exception:
            logger.exception("Regime persist failed")

    # ========================================================
    # public utils
    # ========================================================

    def get_confidence(self) -> float:
        return self._confidence

    def reset_cache(self):
        with self._lock:
            self._cached_regime = None
            self._last_update = None

    # ========================================================
    # future AI hook
    # ========================================================

    def set_ai_model(self, model):
        self._ai_model = model