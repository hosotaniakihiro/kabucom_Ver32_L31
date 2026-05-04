# ============================================================
# rebuild_3m_5m_from_1m.py（Ver24-FINAL）
# ------------------------------------------------------------
# ✔ stock_summary_1min から 3min / 5min を派生生成
# ✔ 当日・前日・前々日を対象（MA整合）
# ✔ end_time / datetime 完全整合
# ✔ 直近 YAHOO_DELAY 分は再生成しない（PUSH保護）
# ✔ DB UPSERT + global_data cache 連動
# ✔ heavy_calculator と完全互換
# ============================================================

import datetime as dt
import logging
import pandas as pd

from sqlalchemy.orm import Session

from database.session import summary_engine
from database.models import (
    StockSummary1Min,
    StockSummary3Min,
    StockSummary5Min,
)

from trading.summary.summary_loader import (
    ensure_datetime,
    get_prev_trading_days,
)
from trading.summary.summary_updater import upsert_all_summaries

logger = logging.getLogger(__name__)

YAHOO_DELAY_MIN = 20


# ============================================================
# 1min サマリーを MultiDay でロード
# ============================================================
def _load_multiday_1m(session: Session, trade_date: dt.date) -> pd.DataFrame:
    """
    当日 + 前日 + 前々日の 1min summary をロード
    """

    prev1, prev2 = get_prev_trading_days(trade_date, 2)
    target_dates = {trade_date, prev1, prev2}

    rows = (
        session.query(StockSummary1Min)
        .filter(StockSummary1Min.date.in_(target_dates))
        .all()
    )

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([
        {
            "symbol": r.symbol,
            "date": r.date,
            "time": r.time,
            "open_price": r.open_price,
            "high_price": r.high_price,
            "low_price": r.low_price,
            "close_price": r.close_price,
            "volume": r.volume,
        }
        for r in rows
    ])

    df = ensure_datetime(df)
    return df.sort_values(["symbol", "datetime"]).reset_index(drop=True)


# ============================================================
# resample（end_time 基準）
# ============================================================
def _resample_from_1m(df_1m: pd.DataFrame, tf_min: int) -> pd.DataFrame:
    """
    1min → tf_min 分足へ派生生成
    end_time = バーの終了時刻
    """

    if df_1m.empty:
        return pd.DataFrame()

    df = df_1m.set_index("datetime")

    rule = f"{tf_min}T"

    df_tf = (
        df
        .groupby("symbol", group_keys=False)
        .resample(rule, label="right", closed="right")
        .agg({
            "open_price": "first",
            "high_price": "max",
            "low_price": "min",
            "close_price": "last",
            "volume": "sum",
        })
    )

    df_tf = df_tf.dropna(subset=["open_price"]).reset_index()

    # end_time / date / time 整備
    df_tf["end_time"] = df_tf["datetime"].dt.time
    df_tf["date"] = df_tf["datetime"].dt.date
    df_tf["time"] = None   # 3m / 5m は time 未使用（summary_loader 側で end_time 利用）

    return df_tf.sort_values(["symbol", "datetime"]).reset_index(drop=True)


# ============================================================
# メイン：3m / 5m 再構築
# ============================================================
def rebuild_3m_5m_from_1m(
    *,
    trade_date: dt.date | None = None,
):
    """
    stock_summary_1min を元に
    3min / 5min summary を再生成する
    """

    if trade_date is None:
        trade_date = dt.date.today()

    now = dt.datetime.now()
    cutoff = now - dt.timedelta(minutes=YAHOO_DELAY_MIN)

    rebuilt_3m = 0
    rebuilt_5m = 0

    with Session(summary_engine) as session:

        # ----------------------------------------------------
        # ① MultiDay 1min ロード
        # ----------------------------------------------------
        df_1m = _load_multiday_1m(session, trade_date)

        if df_1m.empty:
            logger.warning("⚠ rebuild_3m_5m: no 1min data")
            return

        # ----------------------------------------------------
        # ② 直近 YAHOO_DELAY 分は除外（PUSH保護）
        # ----------------------------------------------------
        df_1m = df_1m[df_1m["datetime"] <= cutoff]

        if df_1m.empty:
            logger.warning("⚠ rebuild_3m_5m: 1min all filtered by cutoff")
            return

        # ----------------------------------------------------
        # ③ 3min / 5min 派生生成
        # ----------------------------------------------------
        df_3m = _resample_from_1m(df_1m, 3)
        df_5m = _resample_from_1m(df_1m, 5)

        # ----------------------------------------------------
        # ④ DB UPSERT + cache 更新
        # ----------------------------------------------------
        summary_dict = {
            "3min": df_3m,
            "5min": df_5m,
        }

        upsert_all_summaries(summary_dict, update_cache=True)

        rebuilt_3m = len(df_3m)
        rebuilt_5m = len(df_5m)

    logger.info(
        f"🟢 rebuild_3m_5m_from_1m 完了 | "
        f"3min={rebuilt_3m} | 5min={rebuilt_5m}"
    )
