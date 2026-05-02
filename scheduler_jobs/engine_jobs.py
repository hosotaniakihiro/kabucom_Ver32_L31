# ============================================================
# File   : scheduler_jobs/engine_jobs.py
# Version: Ver1.0-ENGINE-JOBS-PRODUCTION
# ------------------------------------------------------------
# ✔ incremental 1m engine
# ✔ hybrid backup engine
# ✔ higher timeframe resample
# ✔ global_data summary cache更新
# ✔ API互換（process / run / finalize）
# ✔ 例外完全防御
# ✔ logger
# ✔ production safe
# ============================================================

from __future__ import annotations

import logging

from global_state import global_data

# ============================================================
# Engines
# ============================================================

from trading.aggregation.incremental_1m_engine import (
    get_incremental_1m_engine,
)

from trading.aggregation.hybrid_1m_engine import (
    get_hybrid_1m_engine,
)

from trading.aggregation.higher_tf.incremental_higher_tf_engine import (
    incremental_higher_tf_engine,
)

logger = logging.getLogger(__name__)


# ============================================================
# Incremental 1M Engine
# ============================================================

def job_incremental_1m():
    """
    incremental 1m engine 実行

    engine API互換

    engine.process()
    engine.run()
    engine.force_time_based_finalize()
    """

    try:

        engine = get_incremental_1m_engine()

        if engine is None:
            logger.error("[incremental_1m] engine not initialized")
            return

        if hasattr(engine, "process"):

            engine.process()

        elif hasattr(engine, "run"):

            engine.run()

        elif hasattr(engine, "force_time_based_finalize"):

            engine.force_time_based_finalize()

        else:

            logger.error("[incremental_1m] no executable API")

    except Exception:

        logger.exception("[job_incremental_1m]")


# ============================================================
# Hybrid Backup
# ============================================================

def job_hybrid_backup():
    """
    hybrid 1m engine backup

    incremental engine failure時の保険
    """

    try:

        engine = get_hybrid_1m_engine()

        if engine is None:
            logger.error("[hybrid_backup] engine not initialized")
            return

        df = engine.build_hybrid_1m()

        if df is None or df.empty:
            return

        existing = global_data.get_merged_summary(1)

        if existing is not None and not existing.empty:

            try:

                last_existing = existing["datetime"].max()
                last_hybrid = df["datetime"].max()

                if last_existing >= last_hybrid:

                    logger.info(
                        "[HYBRID BACKUP] skipped (incremental newer)"
                    )
                    return

            except Exception:

                logger.warning("[HYBRID BACKUP] datetime check failed")

        global_data.set_merged_summary(1, df)

        logger.info(
            "[HYBRID BACKUP] applied rows=%s",
            len(df),
        )

    except Exception:

        logger.exception("[job_hybrid_backup]")


# ============================================================
# Higher Timeframe Resample
# ============================================================

def job_htf_resample_backup():
    """
    higher timeframe resample

    1m → 3m / 5m
    """

    try:

        if incremental_higher_tf_engine is None:

            logger.error("[HTF] engine missing")
            return

        if hasattr(incremental_higher_tf_engine, "force_resample"):

            incremental_higher_tf_engine.force_resample()

        elif hasattr(incremental_higher_tf_engine, "run"):

            incremental_higher_tf_engine.run()

        else:

            logger.error("[HTF] no resample API")

    except Exception:

        logger.exception("[job_htf_resample_backup]")