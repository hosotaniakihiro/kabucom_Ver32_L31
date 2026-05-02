# ============================================================
# File: AI/entry_gate_tonosama.py
# ------------------------------------------------------------
# 殿様イナゴ（BUY）専用 ENTRY ゲート
#
# ✔ LightGBM 60秒分類モデルを使用
# ✔ 「当たる確率 × 勢い」で最終判断
# ✔ 閾値は保守的・事故防止重視
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
    "TONOSAMA_MODEL_PATH",
    "tonosama_lgbm.txt"
)

# 学習時と完全一致させる（順序厳守）
FEATURES = [
    "price_velocity",
    "volume_speed",
    "rank_jump",
    "rank_strength",
    "dominant_ratio",
    "spread_ratio",
    "minute_from_open",
]

# ENTRY 閾値（殿様固定値）
MIN_PROB  = 0.55
MIN_SCORE = 1.20
MAX_SPREAD_RATIO = 0.003


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

def allow_tonosama_entry(feature_row: Dict[str, float]) -> bool:
    """
    殿様イナゴ BUY の ENTRY 可否を判定する

    Parameters
    ----------
    feature_row : dict
        tonosama_feature_builder で生成された特徴量

    Returns
    -------
    bool
        True  : ENTRY 許可
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
    # スコア統合（確率 × 勢い）
    # --------------------------------------------------------
    volume_speed   = feature_row["volume_speed"]
    dominant_ratio = feature_row["dominant_ratio"]
    spread_ratio   = feature_row["spread_ratio"]

    score = prob * volume_speed * dominant_ratio

    # --------------------------------------------------------
    # 最終ゲート（順序が重要）
    # --------------------------------------------------------

    # ① モデル信頼度
    if prob < MIN_PROB:
        return False

    # ② 板・出来高の勢い
    if score < MIN_SCORE:
        return False

    # ③ 板が荒い銘柄は触らない
    if spread_ratio > MAX_SPREAD_RATIO:
        return False

    return True