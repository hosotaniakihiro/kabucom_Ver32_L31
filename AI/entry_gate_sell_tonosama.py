# ============================================================
# File: AI/entry_gate_sell_tonosama.py
# ------------------------------------------------------------
# 殿様イナゴ（SELL）専用 ENTRY ゲート
#
# ✔ LightGBM 60秒分類モデル（下げ）を使用
# ✔ 「当たる確率 × 売り圧 ×（1/出来高低下）」で最終判断
# ✔ 寄り付き直後のSELL禁止
# ✔ スプレッド・踏み上げ事故防止
# ✔ ENTRY 可否のみを返す（副作用なし）
# ============================================================

from __future__ import annotations

import os
import lightgbm as lgb
import numpy as np
from typing import Dict


# ============================================================
# 設定
# ============================================================

MODEL_PATH = os.environ.get(
    "SELL_TONOSAMA_MODEL_PATH",
    "sell_tonosama_lgbm.txt"
)

# 学習時と完全一致（順序厳守）
FEATURES = [
    "price_velocity",
    "volume_drop",
    "rank_fall",
    "sell_pressure",
    "spread_ratio",
    "minute_from_open",
]

# ENTRY 閾値（SELL殿様 固定）
MIN_PROB = 0.55          # モデル信頼度
MIN_SCORE = 1.30         # 統合スコア
MAX_SPREAD_RATIO = 0.004 # 板が荒い銘柄は触らない
NO_SELL_BEFORE_MIN = 10  # 寄り付き10分はSELL禁止


# ============================================================
# モデルロード（プロセス常駐前提）
# ============================================================

_model: lgb.Booster | None = None


def _load_model() -> lgb.Booster:
    global _model

    if _model is None:
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(f"model not found: {MODEL_PATH}")
        _model = lgb.Booster(model_file=MODEL_PATH)

    return _model


# ============================================================
# メイン API
# ============================================================

def allow_sell_tonosama_entry(feature_row: Dict[str, float]) -> bool:
    """
    殿様イナゴ SELL の ENTRY 可否を判定する

    Parameters
    ----------
    feature_row : dict
        sell_tonosama_feature_builder で生成された特徴量

    Returns
    -------
    bool
        True  : ENTRY 許可（SELL）
        False : ENTRY 拒否
    """

    # --------------------------------------------------------
    # safety
    # --------------------------------------------------------
    if not feature_row:
        return False

    for f in FEATURES:
        if f not in feature_row:
            return False

    # 寄り付き直後SELL禁止（踏み上げ事故防止）
    if feature_row["minute_from_open"] < NO_SELL_BEFORE_MIN:
        return False

    # --------------------------------------------------------
    # 特徴量ベクトル化
    # --------------------------------------------------------
    x = np.array(
        [[float(feature_row[f]) for f in FEATURES]],
        dtype=float
    )

    # --------------------------------------------------------
    # 推論
    # --------------------------------------------------------
    model = _load_model()
    prob = float(model.predict(x)[0])

    # --------------------------------------------------------
    # スコア統合（確率 × 売り圧 × 1/出来高低下）
    # --------------------------------------------------------
    sell_pressure = feature_row["sell_pressure"]
    volume_drop = max(feature_row["volume_drop"], 1e-6)
    spread_ratio = feature_row["spread_ratio"]

    score = prob * sell_pressure / volume_drop

    # --------------------------------------------------------
    # 最終ゲート（順序厳守）
    # --------------------------------------------------------

    # ① モデル信頼度
    if prob < MIN_PROB:
        return False

    # ② 勢い（売り圧 × 出来高ピークアウト）
    if score < MIN_SCORE:
        return False

    # ③ 板が荒い銘柄は触らない
    if spread_ratio > MAX_SPREAD_RATIO:
        return False

    return True