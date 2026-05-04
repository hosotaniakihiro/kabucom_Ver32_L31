# ============================================================
# trading/signals/signal_dispatcher.py
# Ver24.6-CANDIDATE-DISPATCHER-BUY-SELL-LOG
# ------------------------------------------------------------
# ・BUY / SELL の一次エントリー候補を選定
# ・条件関数は実行しない（flag を見るだけ）
# ・score / entry 判定は行わない
# ・★ DISPATCH ログを必ず出力
# ============================================================

import pandas as pd
import logging
from collections import Counter

logger = logging.getLogger(__name__)

# ============================================================
# BUY 側 条件キー定義
# ============================================================

BUY_TREND_KEYS = {
    "dir_up",
    "ma_up",
    "ma5_ma25_cross",
    "perfect_order_event",
}

BUY_MOMENTUM_KEYS = {
    "macd_cross",
    "rsi_rebound",
    "vwap_break",
}

BUY_STRONG_KEYS = {
    "bull_big_combo",
    "gap_up_breakout",
}

# ============================================================
# SELL 側 条件キー定義
# ============================================================

SELL_TREND_KEYS = {
    "ma_downtrend",
    "perfect_order_down",
    "ma_dead_cross",
}

SELL_MOMENTUM_KEYS = {
    "macd_dead_cross",
    "rsi_down",
    "vwap_breakdown",
}

SELL_STRONG_KEYS = {
    "bear_big_combo",
    "gap_down_breakdown",
}

# ============================================================
# 🔥 一次候補選定（唯一の公開 API）
# ============================================================

def dispatch_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """
    summary_df に対して一次エントリー候補を付与
    - candidate_flag : "", "BUY", "SELL"
    - candidate_reasons : list[str]
    """

    if df is None or df.empty:
        logger.info("[DISPATCH] skipped (empty df)")
        return df

    logger.info("[DISPATCH] START rows=%d", len(df))

    df = df.copy()
    df["candidate_flag"] = ""
    df["candidate_reasons"] = [[] for _ in range(len(df))]

    buy_count = 0
    sell_count = 0
    reason_counter = Counter()

    for i, row in df.iterrows():
        reasons = []

        # -----------------------------
        # BUY 判定
        # -----------------------------
        buy_trend = [k for k in BUY_TREND_KEYS if row.get(k) == 1]
        buy_momo  = [k for k in BUY_MOMENTUM_KEYS if row.get(k) == 1]
        buy_strong = [k for k in BUY_STRONG_KEYS if row.get(k) == 1]

        is_buy = (
            len(buy_trend) >= 1
            or len(buy_momo) >= 2
            or len(buy_strong) >= 1
        )

        # -----------------------------
        # SELL 判定
        # -----------------------------
        sell_trend = [k for k in SELL_TREND_KEYS if row.get(k) == 1]
        sell_momo  = [k for k in SELL_MOMENTUM_KEYS if row.get(k) == 1]
        sell_strong = [k for k in SELL_STRONG_KEYS if row.get(k) == 1]

        is_sell = (
            len(sell_trend) >= 1
            or len(sell_momo) >= 2
            or len(sell_strong) >= 1
        )

        # -----------------------------
        # 最終判定
        # -----------------------------
        if is_buy and not is_sell:
            df.at[i, "candidate_flag"] = "BUY"
            reasons = buy_trend + buy_momo + buy_strong
            buy_count += 1

        elif is_sell and not is_buy:
            df.at[i, "candidate_flag"] = "SELL"
            reasons = sell_trend + sell_momo + sell_strong
            sell_count += 1

        else:
            df.at[i, "candidate_flag"] = ""

        df.at[i, "candidate_reasons"] = reasons

        for r in reasons:
            reason_counter[r] += 1

    # ========================================================
    # ログ出力
    # ========================================================
    logger.info(
        "[DISPATCH] DONE total=%d BUY=%d SELL=%d",
        len(df),
        buy_count,
        sell_count,
    )

    if reason_counter:
        logger.info("[DISPATCH] top reasons:")
        for k, v in reason_counter.most_common(10):
            logger.info("  - %-30s : %d", k, v)

    return df
