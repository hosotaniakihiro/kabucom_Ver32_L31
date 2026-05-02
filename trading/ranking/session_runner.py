# ============================================================
# File   : ranking/session_runner.py
# Version: Ver1.0.0-FINAL-RANKING-SESSION-RUNNER
# ------------------------------------------------------------
# ✔ ranking_raw_1min → ranking_session_1min 生成
# ✔ セッションOHLC・順位特徴量・summary乖離
# ✔ STRONG / WEAK / REJECT 判定
# ✔ SQLite / SQLAlchemy / pandas
# ✔ 1分毎 scheduler から安全に呼べる
# ============================================================

import datetime as dt
import logging
import pandas as pd

from database import Session_ranking, Session_summary
from database.models_ranking import RankingRaw1Min
from database.models_ranking_session import RankingSession1Min
from database.models import StockSummary1Min

from ranking.session_builder import build_ranking_sessions
from ranking.session_feature_enricher import attach_summary_gaps
from ranking.session_judge import judge_session

logger = logging.getLogger(__name__)


def _load_latest_summary(session_summary: Session_summary) -> pd.DataFrame:
    """
    銘柄ごとの最新 summary（1分足）を取得
    """
    rows = (
        session_summary.query(StockSummary1Min)
        .order_by(
            StockSummary1Min.symbol,
            StockSummary1Min.dt.desc()
        )
        .all()
    )

    if not rows:
        return pd.DataFrame()

    # symbolごとに最新1行だけ残す
    latest = {}
    for r in rows:
        if r.symbol not in latest:
            latest[r.symbol] = r

    return pd.DataFrame([
        {
            "symbol": str(r.symbol),
            "ma25": r.ma25,
            "ma75": r.ma75,
            "vwap": r.vwap,
            "close_price": r.close_price,
        }
        for r in latest.values()
    ])


def run_ranking_session_pipeline(
    lookback_minutes: int = 30,
    gap_allow: int = 0,
):
    """
    ランキングセッション生成パイプライン（1分毎）

    Parameters
    ----------
    lookback_minutes : int
        何分遡って ranking_raw を再集計するか
    gap_allow : int
        セッション内で許容する欠損分数
    """

    session_rank = Session_ranking()
    session_sum = Session_summary()

    now = dt.datetime.now()
    since = now - dt.timedelta(minutes=lookback_minutes)
    today = now.strftime("%Y%m%d")

    try:
        # ----------------------------------------------------
        # ranking_raw_1min 読み込み
        # ----------------------------------------------------
        rows = (
            session_rank.query(RankingRaw1Min)
            .filter(RankingRaw1Min.dt >= since)
            .all()
        )

        if not rows:
            logger.debug("[ranking_session] no ranking raw data")
            return

        df_rank = pd.DataFrame([
            {
                "dt": r.dt,
                "symbol": str(r.symbol),
                "ranking_type": str(r.ranking_type),
                "rank": r.rank,
                "price": r.price,
            }
            for r in rows
            if r.dt and r.symbol and r.ranking_type
        ])

        if df_rank.empty:
            return

        # ----------------------------------------------------
        # セッション生成
        # ----------------------------------------------------
        df_sessions = build_ranking_sessions(
            df_rank,
            gap_allow=gap_allow
        )

        if df_sessions.empty:
            return

        # ----------------------------------------------------
        # summary 乖離付与
        # ----------------------------------------------------
        df_summary_latest = _load_latest_summary(session_sum)

        df_sessions = attach_summary_gaps(
            df_sessions,
            df_summary_latest
        )

        # ----------------------------------------------------
        # 品質判定
        # ----------------------------------------------------
        df_sessions["quality"] = df_sessions.apply(
            judge_session,
            axis=1
        )

        # ----------------------------------------------------
        # DB 保存（merge で安全）
        # ----------------------------------------------------
        for _, r in df_sessions.iterrows():
            obj = RankingSession1Min(
                date=today,
                symbol=str(r.symbol),
                ranking_type=str(r.ranking_type),
                session_id=int(r.session_id),

                start_dt=r.start_dt,
                end_dt=r.end_dt,
                minutes=int(r.minutes),

                rank_first=int(r.rank_first),
                rank_last=int(r.rank_last),
                rank_best=int(r.rank_best),
                rank_worst=int(r.rank_worst),

                rank_open=float(r.rank_open),
                rank_close=float(r.rank_close),
                rank_high=float(r.rank_high),
                rank_low=float(r.rank_low),

                rank_ret=float(r.rank_ret),
                rank_range=float(r.rank_range),
                rank_improve=int(r.rank_improve),
                rank_slope=float(r.rank_slope),

                d_ma25=r.get("d_ma25"),
                d_ma75=r.get("d_ma75"),
                d_vwap=r.get("d_vwap"),
                d_close=r.get("d_close"),

                quality=str(r.quality),
            )

            session_rank.merge(obj)

        session_rank.commit()

        logger.info(
            "[ranking_session] updated sessions=%d",
            len(df_sessions)
        )

    except Exception as e:
        session_rank.rollback()
        logger.exception(
            "[ranking_session] failed: %s", e
        )
    finally:
        session_rank.close()
        session_sum.close()