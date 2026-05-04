# ============================================================
# AI/features/exit_feature_builder.py
# Ver1.0.1-FINAL-EXIT-FEATURE-BUILDER
# ------------------------------------------------------------
# ✔ EXIT AI 用特徴量生成（唯一の場所）
# ✔ ExitContext から安全に抽出
# ✔ state_machine / inference 両対応
# ✔ DB / global_state 非依存
# ✔ 失敗しても EXIT ロジックを壊さない設計
# ============================================================

from __future__ import annotations

import datetime as dt
from typing import Dict

from trading.exit.exit_context import ExitContext


# ============================================================
# メイン
# ============================================================

def build_exit_features(
    ctx: ExitContext,
    price: float,
    now: dt.datetime,
) -> Dict[str, float]:
    """
    EXIT AI 推論用特徴量を生成する
    """

    if price <= 0:
        raise ValueError("price must be > 0")

    holding_seconds = ctx.holding_seconds(now)

    # --------------------------------------------------------
    # 方向別損益
    # --------------------------------------------------------
    if ctx.side == "BUY":
        pnl = price - ctx.entry_price
        pnl_pct = pnl / ctx.entry_price
    else:  # SELL
        pnl = ctx.entry_price - price
        pnl_pct = pnl / ctx.entry_price

    # --------------------------------------------------------
    # ATR 正規化
    # --------------------------------------------------------
    atr_norm = ctx.atr_1min / ctx.entry_price if ctx.entry_price else 0.0

    # --------------------------------------------------------
    # ストップ距離
    # --------------------------------------------------------
    stop_dist = 0.0
    stop_dist_pct = 0.0

    if ctx.stop_price is not None:
        if ctx.side == "BUY":
            stop_dist = price - ctx.stop_price
        else:
            stop_dist = ctx.stop_price - price

        stop_dist_pct = stop_dist / ctx.entry_price

    # --------------------------------------------------------
    # state one-hot
    # --------------------------------------------------------
    features: Dict[str, float] = {
        # --- side ---
        "side_buy": 1.0 if ctx.side == "BUY" else 0.0,
        "side_sell": 1.0 if ctx.side == "SELL" else 0.0,

        # --- time ---
        "holding_seconds": float(holding_seconds),
        "holding_minutes": holding_seconds / 60.0,

        # --- price ---
        "entry_price": ctx.entry_price,
        "current_price": price,

        # --- pnl ---
        "pnl": pnl,
        "pnl_pct": pnl_pct,

        # --- MFE / MAE ---
        "mfe": ctx.mfe,
        "mae": ctx.mae,
        "mfe_pct": ctx.mfe_pct,
        "mae_pct": ctx.mae_pct,

        # --- ATR ---
        "atr_1min": ctx.atr_1min,
        "atr_norm": atr_norm,

        # --- stop ---
        "stop_price": ctx.stop_price or 0.0,
        "stop_dist": stop_dist,
        "stop_dist_pct": stop_dist_pct,

        # --- extremes ---
        "highest": ctx.highest,
        "lowest": ctx.lowest,

        # --- state ---
        "state_entered": 1.0 if ctx.state == "ENTERED" else 0.0,
        "state_breakeven": 1.0 if ctx.state == "BREAKEVEN" else 0.0,
        "state_trailing": 1.0 if ctx.state == "TRAILING" else 0.0,
    }

    return features
