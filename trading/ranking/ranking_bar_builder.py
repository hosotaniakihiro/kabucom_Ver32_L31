# ============================================================
# trading/ranking/ranking_bar_builder.py
# Ver1.0-FINAL-RANKING-SNAPSHOT-TO-BAR
# ------------------------------------------------------------
# ✔ ranking_snapshot_1min → 疑似1分足 bar 生成
# ✔ runtime 起動時 force rebuild 対応
# ✔ DB 欠損・空テーブル耐性
# ✔ MA 計算の前段専用
# ============================================================

import logging
import datetime as dt
from collections import defaultdict

import pandas as pd

from database import Session_ranking
from database.models import RankingSnapshot1Min

logger = logging.getLogger(__name__)


# ============================================================
# メイン API
# ============================================================

def build_ranking_bar_1min(*, force: bool = False) -> None:
    """
    ranking_snapshot_1min を疑似1分足 bar に変換する
    - bar は DataFrame として global_data.summary_cache に保存
    - MA 計算の前段として使用
    """

    from global_state import global_data

    session = Session_ranking()

    try:
        rows = session.query(RankingSnapshot1Min).all()
        if not rows:
            logger.warning("⚠ ranking_snapshot_1min empty → skip bar build")
            return

        records = []
        for r in rows:
            records.append({
                "symbol": str(r.symbol),
                "rank_type": r.rank_type,
                "market": r.market,
                "rank_position": r.rank_position,
                "rank_strength": r.rank_strength,
                "volume_speed": r.volume_speed,
                "snapshot_time": r.snapshot_time,
            })

        df = pd.DataFrame(records)
        if df.empty:
            logger.warning("⚠ ranking_snapshot DataFrame empty")
            return

        # ----------------------------------------------------
        # 疑似 1分足に正規化
        # ----------------------------------------------------
        df["datetime"] = pd.to_datetime(
            df["snapshot_time"],
            errors="coerce",
            format="mixed",
        )
        df["minute"] = df["datetime"].dt.floor("T")


        bars = []

        grouped = df.groupby(["symbol", "minute"])
        for (symbol, minute), g in grouped:
            bars.append({
                "symbol": symbol,
                "datetime": minute,
                "rank_position": g["rank_position"].mean(),
                "rank_strength": g["rank_strength"].mean(),
                "volume_speed": g["volume_speed"].mean(),
                "count": len(g),
            })

        bar_df = pd.DataFrame(bars)
        if bar_df.empty:
            logger.warning("⚠ ranking_bar empty after grouping")
            return

        # ----------------------------------------------------
        # global cache へ保存
        # ----------------------------------------------------
        if not hasattr(global_data, "ranking_bar_cache") or force:
            global_data.ranking_bar_cache = {}

        global_data.ranking_bar_cache["1min"] = bar_df

        logger.info(
            "🧱 ranking_bar_1min built rows=%d symbols=%d",
            len(bar_df),
            bar_df["symbol"].nunique(),
        )

    except Exception:
        logger.exception("❌ build_ranking_bar_1min failed")

    finally:
        session.close()
