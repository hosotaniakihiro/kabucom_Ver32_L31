# ============================================================
# File : trading/ats/ats_optimizer.py
# Ver  : NIGHT-AI-ATS-OPTIMIZER-V1
# ------------------------------------------------------------
# ✔ night_weighted_score 使用
# ✔ regime / collapse考慮
# ✔ 流動性補正
# ✔ 100銘柄選定
# ✔ 副作用ゼロ
# ============================================================

import pandas as pd
import numpy as np
import logging
from pathlib import Path
import json
import datetime as dt

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# パラメータ
# ------------------------------------------------------------

MAX_SYMBOLS = 100
COLLAPSE_THRESHOLD = 30
MIN_VOLUME = 1000


# ------------------------------------------------------------
# 最適化メイン
# ------------------------------------------------------------

def optimize_ats_symbols(df: pd.DataFrame) -> list[str]:

    if df is None or df.empty:
        logger.warning("[ATS OPT] input empty")
        return []

    df = df.copy()

    required_cols = [
        "symbol",
        "night_weighted_score",
        "regime_label",
        "collapse_risk",
        "volume",
    ]

    for col in required_cols:
        if col not in df.columns:
            logger.warning(f"[ATS OPT] missing column: {col}")
            return []

    # --------------------------------------------------------
    # ① collapse除外
    # --------------------------------------------------------
    df = df[df["collapse_risk"] < COLLAPSE_THRESHOLD]

    # --------------------------------------------------------
    # ② regime調整
    # --------------------------------------------------------
    regime_bonus = {
        "BULL": 1.2,
        "NEUTRAL": 1.0,
        "BEAR": 0.6,
    }

    df["regime_factor"] = df["regime_label"].map(regime_bonus).fillna(1.0)

    # --------------------------------------------------------
    # ③ 流動性補正
    # --------------------------------------------------------
    df["liquidity_factor"] = (
        df["volume"]
        .fillna(0)
        .clip(lower=1)
        .apply(lambda x: min(np.log10(x + 1), 5))
    )

    # --------------------------------------------------------
    # ④ 最終ATSスコア
    # --------------------------------------------------------
    df["ats_score"] = (
        df["night_weighted_score"]
        * df["regime_factor"]
        * df["liquidity_factor"]
    )

    df["ats_score"] = (
        pd.to_numeric(df["ats_score"], errors="coerce")
        .replace([np.inf, -np.inf], 0)
        .fillna(0)
    )

    # --------------------------------------------------------
    # ⑤ 上位抽出
    # --------------------------------------------------------
    df = df.sort_values("ats_score", ascending=False)

    top_df = df.head(MAX_SYMBOLS)

    symbols = top_df["symbol"].astype(str).tolist()

    logger.info(f"[ATS OPT] selected {len(symbols)} symbols")

    return symbols


# ------------------------------------------------------------
# 保存
# ------------------------------------------------------------

def save_next_day_watchlist(symbols: list[str]):

    today = dt.datetime.now().strftime("%Y%m%d")

    path = Path("ai/ats_next_day")
    path.mkdir(parents=True, exist_ok=True)

    file = path / f"ats_watchlist_{today}.json"

    with open(file, "w", encoding="utf-8") as f:
        json.dump(symbols, f, indent=2)

    logger.info(f"[ATS OPT] saved → {file}")