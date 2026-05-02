# ============================================================
# File   : trading/exit/market_state_builder.py
# Version: Ver26.0-FINAL-MARKET-STATE-BUILDER-SAFE
# ------------------------------------------------------------
# ✔ EXIT用 市場状態生成
# ✔ collapse / regime 将来拡張フック
# ✔ NaN / object 完全排除
# ✔ 例外完全握り
# ✔ deterministic
# ✔ 本番例外耐性MAX
# ============================================================

from __future__ import annotations

import logging
import math
from typing import Dict, Any

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# 安全数値化
# ============================================================

def _safe_float(x: Any) -> float:
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return 0.0
        return v
    except Exception:
        return 0.0


# ============================================================
# 市場状態ビルド
# ============================================================

def build_market_state(
    symbol: str,
    side: str,
    price: float,
    pnl: float,
    five_sec_bar: Dict[str, Any] | None = None,
    summary_row: Dict[str, Any] | None = None,
) -> Dict[str, float]:
    """
    EXIT AI / STATE MACHINE 用の市場状態生成

    戻り値は常に数値のみ。
    例外が出ても安全値を返す。
    """

    try:
        state: Dict[str, float] = {}

        # ----------------------------------------------------
        # 基本情報
        # ----------------------------------------------------
        state["price"] = _safe_float(price)
        state["pnl"] = _safe_float(pnl)
        state["side_long"] = 1.0 if str(side).upper() == "BUY" else 0.0
        state["side_short"] = 1.0 if str(side).upper() == "SELL" else 0.0

        # ----------------------------------------------------
        # 5秒足情報
        # ----------------------------------------------------
        if isinstance(five_sec_bar, dict):
            state["fs_close"] = _safe_float(five_sec_bar.get("close"))
            state["fs_high"] = _safe_float(five_sec_bar.get("high"))
            state["fs_low"] = _safe_float(five_sec_bar.get("low"))
            state["fs_volume"] = _safe_float(five_sec_bar.get("volume"))
        else:
            state["fs_close"] = 0.0
            state["fs_high"] = 0.0
            state["fs_low"] = 0.0
            state["fs_volume"] = 0.0

        # ----------------------------------------------------
        # Summary情報（任意）
        # ----------------------------------------------------
        if isinstance(summary_row, dict):
            state["atr"] = _safe_float(summary_row.get("atr"))
            state["vwap"] = _safe_float(summary_row.get("vwap"))
            state["volume_speed"] = _safe_float(summary_row.get("volume_speed"))
            state["price_delta_1m"] = _safe_float(summary_row.get("price_delta_1m"))
        else:
            state["atr"] = 0.0
            state["vwap"] = 0.0
            state["volume_speed"] = 0.0
            state["price_delta_1m"] = 0.0

        # ----------------------------------------------------
        # 簡易ボラ
        # ----------------------------------------------------
        state["fs_range"] = max(
            state["fs_high"] - state["fs_low"],
            0.0,
        )

        # ----------------------------------------------------
        # collapse フック（将来拡張）
        # ----------------------------------------------------
        state["collapse_signal"] = 0.0  # 将来モデル統合用

        # ----------------------------------------------------
        # regime フック
        # ----------------------------------------------------
        state["regime_score"] = 0.0  # 将来統合用

        return state

    except Exception:
        logger.exception("[build_market_state] exception")

        # 完全安全値を返す
        return {
            "price": 0.0,
            "pnl": 0.0,
            "side_long": 0.0,
            "side_short": 0.0,
            "fs_close": 0.0,
            "fs_high": 0.0,
            "fs_low": 0.0,
            "fs_volume": 0.0,
            "atr": 0.0,
            "vwap": 0.0,
            "volume_speed": 0.0,
            "price_delta_1m": 0.0,
            "fs_range": 0.0,
            "collapse_signal": 0.0,
            "regime_score": 0.0,
        }