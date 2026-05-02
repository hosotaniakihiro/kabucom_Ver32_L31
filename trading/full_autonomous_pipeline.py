# ============================================================
# File   : trading/full_autonomous_pipeline.py
# Version: Ver2.0-ABSOLUTE-FINAL-AUTONOMOUS-ORCHESTRATOR
# ------------------------------------------------------------
# ✔ Metaモデル選択
# ✔ AI予測
# ✔ RL統合
# ✔ Sharpe最適化
# ✔ 自律リスク配分
# ✔ regime × cluster × bandit統合
# ✔ NaN / inf 完全耐性
# ✔ dtype固定
# ✔ 本番運用耐性
# ✔ deterministic
# ============================================================

from __future__ import annotations
import numpy as np
import pandas as pd
import logging
from typing import Any

from trading.ai.meta_selector import meta_select_df
from trading.ai.regime_detector import detect_regime
from trading.ai.cluster_router import route_cluster
from trading.ai.bandit_engine import select_bandit_weight
from trading.ai.attack_detector import detect_attack_row

from trading.ai.rl_agent import SimpleRLAgent
from trading.ai.sharpe_optimizer import optimize_weight
from trading.portfolio.risk_allocator import allocate_positions

logger = logging.getLogger(__name__)

rl_agent = SimpleRLAgent(state_dim=12)


# ============================================================
# 安全数値変換
# ============================================================

def _safe(v: Any, default: float = 0.0) -> float:
    try:
        v = pd.to_numeric(v, errors="coerce")
        if pd.isna(v):
            return float(default)
        v = float(v)
        if np.isinf(v):
            return float(default)
        return v
    except Exception:
        return float(default)


# ============================================================
# AI予測安全ラッパ
# ============================================================

def _ai_predict_row(row, model_manager):

    try:
        cluster = route_cluster(row)
        models = model_manager.registry.get(cluster, {})

        if not models:
            return 0.0

        model_name = list(models.keys())[0]
        X = (
            pd.Series(row)
            .select_dtypes(include=["float", "int"])
            .values
            .astype("float64")
            .reshape(1, -1)
        )

        pred = model_manager.predict(cluster, model_name, X)

        return _safe(pred)

    except Exception:
        return 0.0


# ============================================================
# RL安全ラッパ
# ============================================================

def _rl_act_row(row):

    try:
        state = (
            pd.Series(row)
            .select_dtypes(include=["float", "int"])
            .values
            .astype("float64")
        )

        action = rl_agent.act(state)

        return int(action)

    except Exception:
        return 0


# ============================================================
# フル自律実行
# ============================================================

def run_autonomous_system(
    df: pd.DataFrame,
    *,
    model_manager,
    capital: float,
    max_risk_per_trade: float = 0.01,
):

    if df is None or df.empty:
        return df

    df = df.copy()

    # ========================================================
    # regime / cluster 付与
    # ========================================================

    df["regime"] = df.apply(lambda r: detect_regime(r.to_dict()), axis=1)
    df["cluster"] = df.apply(lambda r: route_cluster(r.to_dict()), axis=1)

    # ========================================================
    # Meta選択
    # ========================================================

    df = meta_select_df(df)

    # ========================================================
    # AIスコア
    # ========================================================

    df["ai_score"] = df.apply(
        lambda r: _ai_predict_row(r.to_dict(), model_manager),
        axis=1,
    )

    # ========================================================
    # RL行動
    # ========================================================

    df["rl_action"] = df.apply(
        lambda r: _rl_act_row(r.to_dict()),
        axis=1,
    )

    # ========================================================
    # bandit weight
    # ========================================================

    df["bandit_weight"] = df.apply(
        lambda r: select_bandit_weight(
            r["cluster"],
            r["regime"]
        ),
        axis=1,
    )

    # ========================================================
    # attack strength
    # ========================================================

    def _attack_strength(row):
        atk = detect_attack_row(row.to_dict())
        return _safe(atk.get("attack_strength", 0.0))

    df["attack_strength"] = df.apply(_attack_strength, axis=1)

    # ========================================================
    # 最終スコア合成
    # ========================================================

    df["final_score"] = (
        _safe(df.get("ai_score", 0))
        + _safe(df.get("rl_action", 0))
        + _safe(df.get("attack_strength", 0)) * 2
    )

    df["final_score"] = (
        pd.to_numeric(df["final_score"], errors="coerce")
        .replace([np.inf, -np.inf], 0.0)
        .fillna(0.0)
    )

    # ========================================================
    # Sharpe最適化（重み調整）
    # ========================================================

    weights = optimize_weight(df["final_score"].values)

    df["optimized_weight"] = weights

    # ========================================================
    # ポジション配分（リスク連動）
    # ========================================================

    df = allocate_positions(
        df,
        equity=capital,
        max_risk_per_trade=max_risk_per_trade,
    )

    # 最終ポジション調整
    df["position_size"] = (
        df["position_size"]
        * df["optimized_weight"]
    ).astype(int)

    # dtype固定
    numeric_cols = [
        "ai_score",
        "rl_action",
        "attack_strength",
        "final_score",
        "optimized_weight",
        "position_size",
    ]

    for c in numeric_cols:
        if c in df.columns:
            df[c] = (
                pd.to_numeric(df[c], errors="coerce")
                .replace([np.inf, -np.inf], 0.0)
                .fillna(0.0)
            )

    return df