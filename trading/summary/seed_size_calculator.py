# ============================================================
# seed_size_calculator.py
# FINAL-MA75-SEED-SIZE-AUTO
# ------------------------------------------------------------
# ✔ MA75 から seed 本数を逆算
# ✔ symbol 単位で最低本数を保証
# ✔ 欠損・上場直後銘柄を自動吸収
# ============================================================

import logging
from sqlalchemy import func

from database import Session_summary
from database.models import StockSummary1Min

logger = logging.getLogger(__name__)

MA_PERIOD = 75
SAFETY_MARGIN = 20   # 欠損・欠落吸収用


# ============================================================
def calc_seed_limit_for_ma75(
    *,
    max_symbols: int | None = None,
) -> int:
    """
    MA75 が全銘柄で安定するために必要な seed 本数を逆算する
    """

    session = Session_summary()
    try:
        # symbol ごとの本数
        q = (
            session.query(
                StockSummary1Min.symbol,
                func.count(StockSummary1Min.id).label("cnt"),
            )
            .group_by(StockSummary1Min.symbol)
        )

        if max_symbols:
            q = q.limit(max_symbols)

        rows = q.all()
        if not rows:
            logger.warning("[SEED_SIZE] no summary rows found")
            return MA_PERIOD + SAFETY_MARGIN

        min_cnt = min(r.cnt for r in rows)

        # MA75 が成立する最低本数
        need = max(MA_PERIOD, min_cnt)

        limit = need + SAFETY_MARGIN

        logger.info(
            "[SEED_SIZE] symbols=%d min_cnt=%d seed_limit=%d",
            len(rows),
            min_cnt,
            limit,
        )

        return limit

    finally:
        session.close()
