# ============================================================
# File   : trading/ranking/tonosama/pipeline.py
# Purpose: ranking_snapshot_1min → 殿様候補抽出
# ============================================================

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import RankingTonosamaConfig, DEFAULT_RANKING_TONOSAMA_CONFIG
from .snapshot_loader import load_ranking_snapshot_1min
from .features import build_ranking_tonosama_features
from .detector import detect_ranking_tonosama

logger = logging.getLogger(__name__)


def build_ranking_tonosama_candidates_from_df(
    df: pd.DataFrame,
    *,
    config: RankingTonosamaConfig = DEFAULT_RANKING_TONOSAMA_CONFIG,
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    feat = build_ranking_tonosama_features(df)
    if feat.empty:
        return pd.DataFrame()

    det = detect_ranking_tonosama(feat, config=config)

    cand = det[det["ranking_tonosama_ok"] == True].copy()

    if cand.empty:
        logger.info("[RANKING TONOSAMA PIPELINE] no candidates rows=%s", len(det))
        return cand

    if "datetime" in cand.columns:
        latest_dt = cand["datetime"].max()
        cand = cand[cand["datetime"] == latest_dt].copy()

    cand = cand.sort_values("ranking_tonosama_score", ascending=False)

    if "symbol" in cand.columns:
        cand = cand.drop_duplicates(subset=["symbol"], keep="first")

    cand = cand.head(config.max_candidates).copy()

    logger.info(
        "[RANKING TONOSAMA PIPELINE] candidates=%s symbols=%s",
        len(cand),
        ",".join(cand["symbol"].astype(str).tolist()) if "symbol" in cand.columns else "-",
    )

    return cand


def build_ranking_tonosama_candidates_from_db(
    db_path: str | Path,
    *,
    now: Optional[datetime] = None,
    config: RankingTonosamaConfig = DEFAULT_RANKING_TONOSAMA_CONFIG,
) -> pd.DataFrame:
    now = now or datetime.now()
    start_dt = now - timedelta(minutes=config.lookback_minutes + 5)

    df = load_ranking_snapshot_1min(
        db_path,
        start_dt=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        end_dt=now.strftime("%Y-%m-%d %H:%M:%S"),
    )

    return build_ranking_tonosama_candidates_from_df(df, config=config)