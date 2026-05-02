# ============================================================
# ranking_entry_event_saver.py
# Ver1.0-NIGHTAI-EVENT-SAVER
# ------------------------------------------------------------
# ✔ RankingEntryEvent 保存専用
# ✔ 例外完全吸収
# ✔ セッション自動管理
# ✔ None安全
# ✔ 副作用ゼロ
# ============================================================

from __future__ import annotations

import logging
import datetime as dt

from database.session import Session_position
from database.models import RankingEntryEvent

logger = logging.getLogger(__name__)


def save_ranking_entry_event(
    *,
    symbol: str,
    symbolname: str | None,
    side: str,
    interval: int,
    summary_row: dict | None,
    ranking_row: dict | None,
    ai_reason: str | None = None,
):

    try:
        session = Session_position()

        now = dt.datetime.now()

        summary_row = summary_row or {}
        ranking_row = ranking_row or {}

        event = RankingEntryEvent(
            # -----------------------
            # 基本
            # -----------------------
            symbol=str(symbol),
            symbolname=symbolname,
            event_time=now,
            interval=int(interval),
            side=str(side),

            # -----------------------
            # ranking
            # -----------------------
            rank_type=ranking_row.get("rank_type"),
            rank_position=ranking_row.get("rank_position"),
            rank_strength=ranking_row.get("rank_strength"),
            rank_persistence=ranking_row.get("rank_persistence"),
            volume_speed=ranking_row.get("volume_speed"),
            change_rate=ranking_row.get("change_rate"),

            # -----------------------
            # summary
            # -----------------------
            close_price=summary_row.get("close_price"),
            volume=summary_row.get("volume"),
            vwap=summary_row.get("vwap"),

            ma25=summary_row.get("ma25"),
            ma75=summary_row.get("ma75"),
            ma25_conf=summary_row.get("ma25_conf"),
            ma75_conf=summary_row.get("ma75_conf"),

            slope_atr_scaled=summary_row.get("slope_atr_scaled"),
            volume_slope=summary_row.get("volume_slope"),

            rsi=summary_row.get("rsi"),
            atr=summary_row.get("atr"),

            final_score=summary_row.get("score"),

            # -----------------------
            # AI
            # -----------------------
            ai_reason=ai_reason,
        )

        session.add(event)
        session.commit()
        session.close()

        logger.debug(
            f"[RankingEntryEvent SAVED] {symbol} {side} {interval}min"
        )

    except Exception:
        logger.exception("❌ ranking_entry_event save failed")