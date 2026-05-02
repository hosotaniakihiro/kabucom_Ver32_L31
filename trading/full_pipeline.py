# ============================================================
# File   : trading/full_pipeline.py
# Version: FINAL-FULL-INTEGRATED-PIPELINE
# ------------------------------------------------------------
# ✔ regime判定
# ✔ meta bandit適用
# ✔ cluster routing
# ✔ AIスコア統合
# ✔ position sizing
# ✔ risk guard連携可能
# ============================================================

from __future__ import annotations
import logging

from trading.regime.regime_engine import detect_market_regime
from trading.bandit.meta_bandit_manager import MetaBanditManager
from trading.ai.cluster_router import route_cluster
from trading.ai.ai_score_engine import apply_ai_model
from trading.risk.position_sizer import calculate_position_size

logger = logging.getLogger(__name__)

meta_bandit = MetaBanditManager()


def run_pipeline(
    df,
    index_df,
    model_manager,
    capital: float,
):

    if df is None or df.empty:
        return df

    # --------------------------------------------------------
    # ① regime
    # --------------------------------------------------------
    regime_info = detect_market_regime(index_df)
    regime = regime_info.get("regime", "neutral")

    # --------------------------------------------------------
    # ② cluster routing
    # --------------------------------------------------------
    df["cluster"] = df.apply(route_cluster, axis=1)

    # --------------------------------------------------------
    # ③ bandit weight
    # --------------------------------------------------------
    df["bandit_weight"] = df.apply(
        lambda row: meta_bandit.get_weight(regime, row["cluster"]),
        axis=1
    )

    # --------------------------------------------------------
    # ④ AI score
    # --------------------------------------------------------
    df = apply_ai_model(df, model_manager.models)

    # --------------------------------------------------------
    # ⑤ final score
    # --------------------------------------------------------
    df["score_final"] = (
        df["score_total"].fillna(0)
        * df["bandit_weight"].fillna(1.0)
        + df["score_ai"].fillna(0)
    )

    # --------------------------------------------------------
    # ⑥ position sizing
    # --------------------------------------------------------
    df["position_size"] = df.apply(
        lambda row: calculate_position_size(
            capital=capital,
            risk_per_trade=0.01,
            atr=row.get("atr"),
        ),
        axis=1
    )

    return df