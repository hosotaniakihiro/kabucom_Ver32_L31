# ============================================================
# File   : AI/risk/drawdown_guard.py
# Ver1.1-PRODUCTION-DRAWDOWN-RISK-GUARD
# ------------------------------------------------------------
# ✔ 累積損益からドローダウン率を算出
# ✔ MAX_RISK_YEN に掛ける scale を返す
# ✔ ENTRY / EXIT から独立
# ✔ DBアクセスキャッシュ（高速化）
# ✔ crash safe
# ✔ NaN safe
# ✔ realtime対応
# ============================================================

from __future__ import annotations

import logging
import time
import math

from database.models import TradeHistory
from config import global_config
from database.session import Session_position as Session_trade


logger = logging.getLogger(__name__)


# ============================================================
# cache
# ============================================================

_LAST_UPDATE = 0.0
_CACHED_SCALE = 1.0

CACHE_SECONDS = 5


# ============================================================
# config helper
# ============================================================

def _cfg(key: str, default):

    try:
        return global_config.get(key, default)
    except Exception:
        return default


# ============================================================
# drawdown calculation
# ============================================================

def _calculate_drawdown(session) -> float:
    """
    累積損益からドローダウン率算出
    """

    try:

        rows = (
            session.query(TradeHistory.realized_pnl)
            .order_by(TradeHistory.trade_time.asc())
            .all()
        )

        if not rows:
            return 0.0

        equity = 0.0
        peak = 0.0

        for (pnl,) in rows:

            try:
                pnl = float(pnl or 0.0)
            except Exception:
                pnl = 0.0

            if math.isnan(pnl) or math.isinf(pnl):
                pnl = 0.0

            equity += pnl

            if equity > peak:
                peak = equity

        if peak <= 0:
            return 0.0

        dd = (peak - equity) / peak

        return max(0.0, dd)

    except Exception:

        logger.exception("[DRAWDOWN] calculation failed")

        return 0.0


# ============================================================
# risk scale
# ============================================================

def get_risk_scale() -> float:
    """
    現在のドローダウン状況からリスク係数を返す
    """

    global _LAST_UPDATE
    global _CACHED_SCALE

    now = time.time()

    # --------------------------------------------------------
    # cache
    # --------------------------------------------------------

    if now - _LAST_UPDATE < CACHE_SECONDS:
        return _CACHED_SCALE

    session = None

    try:

        session = Session_trade()

        dd = _calculate_drawdown(session)

        # ----------------------------------------------------
        # risk curve
        # ----------------------------------------------------

        if dd < _cfg("RISK_DD_LV1", 0.03):

            scale = 1.0

        elif dd < _cfg("RISK_DD_LV2", 0.06):

            scale = 0.70

        elif dd < _cfg("RISK_DD_LV3", 0.10):

            scale = 0.40

        else:

            scale = 0.0   # ENTRY STOP

        _CACHED_SCALE = float(scale)
        _LAST_UPDATE = now

        return _CACHED_SCALE

    except Exception:

        logger.exception("[RISK] get_risk_scale failed")

        return 0.5

    finally:

        if session:
            session.close()