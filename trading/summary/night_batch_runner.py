# ============================================================
# File   : trading/summary/night_batch_runner.py
# Version: Ver30.0-NIGHT-AI-ORCHESTRATOR-PRODUCTION
# ------------------------------------------------------------
# ✔ Night AI Orchestrator
# ✔ scoring_main(force=True) 実行
# ✔ night_weighted_score 使用
# ✔ regime 推論含む
# ✔ ATS最適化
# ✔ Bandit prior生成
# ✔ interval可変対応
# ✔ 例外完全吸収
# ✔ 副作用ゼロ
# ✔ 本番安全設計
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import datetime as dt
from typing import Optional

from trading.scoring.core.scoring_core import scoring_main
from trading.ats.ats_optimizer import (
    optimize_ats_symbols,
    save_next_day_watchlist,
)
from trading.ai.bandit_prior_updater import (
    build_bandit_prior,
    save_bandit_prior,
)

logger = logging.getLogger(__name__)


# ============================================================
# 内部ユーティリティ
# ============================================================

def _safe_df(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.DataFrame()
    return df.copy()


def _log_header():
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("============================================================")
    logger.info(f"🌙 NIGHT AI BATCH START  @ {now}")
    logger.info("============================================================")


def _log_footer():
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("============================================================")
    logger.info(f"🌙 NIGHT AI BATCH END    @ {now}")
    logger.info("============================================================")


# ============================================================
# 🌙 メイン実行関数
# ============================================================

def run_night_summary_build(
    summary_df: pd.DataFrame,
    *,
    interval: str | int = "3min",
) -> None:
    """
    Night AI batch orchestrator.

    Parameters
    ----------
    summary_df : pd.DataFrame
        最新サマリー（例: 3min確定足）
    interval : str | int
        "1min", "3min", "5min" 等
    """

    try:

        df = _safe_df(summary_df)

        if df.empty:
            logger.warning("[NIGHT BATCH] summary_df empty → skip")
            return

        _log_header()

        logger.info(
            f"[NIGHT BATCH] input rows={len(df)} interval={interval}"
        )

        # --------------------------------------------------------
        # ① scoring（force=True で夜モード）
        # --------------------------------------------------------
        try:

            df_scored = scoring_main(
                df,
                interval=interval,
                force=True,
            )

            if df_scored is None or df_scored.empty:
                logger.warning("[NIGHT BATCH] scoring result empty → abort")
                _log_footer()
                return

            logger.info(
                f"[NIGHT BATCH] scoring completed rows={len(df_scored)}"
            )

        except Exception:
            logger.exception("[NIGHT BATCH] scoring failed")
            _log_footer()
            return

        # --------------------------------------------------------
        # ② ATS 最適化
        # --------------------------------------------------------
        try:

            symbols = optimize_ats_symbols(df_scored)

            if symbols:
                save_next_day_watchlist(symbols)
                logger.info(
                    f"[NIGHT BATCH] ATS selected {len(symbols)} symbols"
                )
            else:
                logger.warning("[NIGHT BATCH] ATS returned empty list")

        except Exception:
            logger.exception("[NIGHT BATCH] ATS optimization failed")

        # --------------------------------------------------------
        # ③ Bandit Prior 生成
        # --------------------------------------------------------
        try:

            prior_dict = build_bandit_prior(df_scored)

            if prior_dict:
                save_bandit_prior(prior_dict)
                logger.info(
                    f"[NIGHT BATCH] Bandit prior size={len(prior_dict)}"
                )
            else:
                logger.warning("[NIGHT BATCH] prior empty")

        except Exception:
            logger.exception("[NIGHT BATCH] prior build failed")

        _log_footer()

    except Exception:
        logger.exception("[NIGHT BATCH] fatal error")