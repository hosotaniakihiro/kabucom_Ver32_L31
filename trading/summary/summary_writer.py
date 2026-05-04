# ============================================================
# trading/summary/summary_writer.py
# (Ver26-FINAL-PASS-THROUGH)
# ------------------------------------------------------------
# ✔ calculator 出力をそのまま DB 保存
# ✔ symbolname / indicator_ready を改変しない
# ✔ 再計算・補完・復元 一切禁止
# ============================================================

import logging
import pandas as pd
from sqlalchemy.orm import Session

from database.session import summary_engine
from database.models import (
    StockSummary1Min,
    StockSummary3Min,
    StockSummary5Min,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# interval → Model
# ------------------------------------------------------------
def _get_model(interval: int):
    if interval == 1:
        return StockSummary1Min
    if interval == 3:
        return StockSummary3Min
    if interval == 5:
        return StockSummary5Min
    raise ValueError(f"invalid interval: {interval}")


# ------------------------------------------------------------
# 🔥 メイン：summary 保存
# ------------------------------------------------------------
def save_summary_df(
    df: pd.DataFrame,
    interval: int,
):
    """
    calculator で生成された summary DF を
    そのまま DB に保存する
    """

    if df is None or df.empty:
        logger.info("[SUMMARY-WRITER] empty df")
        return

    Model = _get_model(interval)

    # 保存対象カラム（Model と完全一致）
    cols = [
        "symbol",
        "symbolname",
        "datetime",
        "date",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "vwap",
        "interval",
        "start_time",
        "end_time",
        "time_range",
        "ma_ready_5",
        "ma_ready_25",
        "ma_ready_75",
        "rsi_ready",
        "indicator_ready",
        "score_buy",
        "score_sell",
        "buy_reasons",
        "sell_reasons",
        "buy_reason_scores",
        "sell_reason_scores",
    ]

    missing = set(cols) - set(df.columns)
    if missing:
        logger.error(f"[SUMMARY-WRITER] missing columns: {missing}")
        return

    records = df[cols].to_dict(orient="records")

    with Session(summary_engine) as session:
        for r in records:
            session.merge(Model(**r))
        session.commit()

    logger.info(
        f"[SUMMARY-WRITER] saved rows={len(records)} interval={interval}"
    )
