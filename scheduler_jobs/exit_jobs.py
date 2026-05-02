# ============================================================
# File   : scheduler_jobs/exit_jobs.py
# Version: Ver2.0-PRODUCTION-HARDENED-EXIT-JOBS
# ------------------------------------------------------------
# ✔ 5秒足生成
# ✔ exit_loop実行
# ✔ open_positions連動
# ✔ global_data cache更新
# ✔ Exit AI 統合
# ✔ symbol normalize
# ✔ NaN / inf 防御
# ✔ exception safe
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

from global_state import global_data

# ============================================================
# Exit Engines
# ============================================================

from trading.handlers.exit_handler import (
    build_5s_bar_fast,
)

from trading.exit.exit_loop import (
    exit_loop,
)

from trading.ai.exit_ai import detect_exit_signal

logger = logging.getLogger(__name__)


# ============================================================
# symbol normalize
# ============================================================

def _normalize_symbol(sym):

    try:
        return str(sym).strip()
    except Exception:
        return ""


# ============================================================
# numeric sanitize
# ============================================================

def _sanitize_df(df):

    try:

        if df is None or df.empty:
            return df

        num_cols = df.select_dtypes(include=np.number).columns

        if len(num_cols) > 0:

            df[num_cols] = (
                df[num_cols]
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
            )

        return df

    except Exception:

        logger.exception("[exit_jobs] sanitize failed")

        return df


# ============================================================
# 5秒足生成
# ============================================================

def update_5s_bars():
    """
    open_positions から
    5秒足を生成
    """

    try:

        open_positions = getattr(global_data, "open_positions", None)

        if not isinstance(open_positions, dict) or not open_positions:

            global_data.five_sec_bars = {}

            return

        bars = {}

        for sym in list(open_positions.keys()):

            try:

                symbol = _normalize_symbol(sym)

                if not symbol:
                    continue

                bar = build_5s_bar_fast(symbol)

                if not bar:
                    continue

                bars[symbol] = bar

            except Exception:

                logger.exception(
                    "[update_5s_bars] symbol=%s",
                    sym,
                )

        global_data.five_sec_bars = bars

    except Exception:

        logger.exception("[update_5s_bars]")


# ============================================================
# Exit AI check
# ============================================================

def _apply_exit_ai():

    try:

        open_positions = getattr(global_data, "open_positions", None)

        if not isinstance(open_positions, dict) or not open_positions:
            return

        rows = []

        for sym, pos in open_positions.items():

            try:

                row = dict(pos)

                row["symbol"] = _normalize_symbol(sym)

                rows.append(row)

            except Exception:

                logger.exception(
                    "[exit_ai] row build failed symbol=%s",
                    sym
                )

        if not rows:
            return

        df = pd.DataFrame(rows)

        df = _sanitize_df(df)

        df_exit = detect_exit_signal(df)

        if df_exit is None or df_exit.empty:
            return

        exit_symbols = set(df_exit["symbol"].astype(str))

        global_data.exit_ai_symbols = exit_symbols

        logger.info(
            "[exit_ai] exit candidates=%s",
            len(exit_symbols)
        )

    except Exception:

        logger.exception("[exit_ai]")


# ============================================================
# Exit Job
# ============================================================

def job_exit_5s():
    """
    5秒足更新 + exit判定
    """

    try:

        # --------------------------------
        # 5秒足生成
        # --------------------------------

        update_5s_bars()

        # --------------------------------
        # Exit AI
        # --------------------------------

        try:

            _apply_exit_ai()

        except Exception:

            logger.exception("[job_exit_5s] exit_ai failed")

        # --------------------------------
        # exit loop
        # --------------------------------

        exit_loop()

    except Exception:

        logger.exception("[job_exit_5s]")