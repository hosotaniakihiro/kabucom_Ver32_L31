# ============================================================
# File   : trading/entry/final_entry_judge.py
# Version: Ver25-PRODUCTION-FINAL-ENTRY-JUDGE-ULTRA
# ------------------------------------------------------------
# ✔ Ver24 完全保持（削除ゼロ）
# ✔ summary / ranking / AI 統合
# ✔ expire制御維持
# ✔ acceleration / trend / momentum 追加
# ✔ direction整合性チェック
# ✔ volume / liquidityフィルタ
# ✔ スコア安定化（soft guard）
# ✔ fallback安全
# ✔ production safe（絶対落とさない）
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional, Dict, Any

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# helpers
# ============================================================

def _safe(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        v = float(v)
        if np.isnan(v) or np.isinf(v):
            return default
        return v
    except Exception:
        return default


def _clip(v: float, low: float = -1000.0, high: float = 1000.0) -> float:
    try:
        return max(low, min(high, v))
    except Exception:
        return 0.0


# ============================================================
# main
# ============================================================

def judge_final_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    pending_entries の1件を評価して ENTRY 可否を判定
    """

    try:

        now = dt.datetime.now()

        # ----------------------------------------------------
        # expire（完全保持）
        # ----------------------------------------------------
        if entry.get("expire_at") and entry["expire_at"] < now:
            return None

        # ----------------------------------------------------
        # 元スコア（完全保持）
        # ----------------------------------------------------
        summary_score = _safe(entry.get("summary_score", 0.0))
        ranking_score = _safe(entry.get("ranking_score", 0.0))
        ai_prob = _safe(entry.get("ai_prob", 0.0))

        # ----------------------------------------------------
        # 🔥 追加特徴量（新規）
        # ----------------------------------------------------
        trend = _safe(entry.get("_score_trend", 0.0))
        momentum = _safe(entry.get("_score_momentum", 0.0))
        acceleration = _safe(entry.get("_score_acceleration", 0.0))
        velocity = _safe(entry.get("_score_velocity", 0.0))

        volume = _safe(entry.get("volume", 0.0))
        price = _safe(entry.get("price", 0.0))
        vwap = _safe(entry.get("vwap", 0.0))

        # ----------------------------------------------------
        # direction（超重要）
        # ----------------------------------------------------
        direction = trend * momentum

        direction_penalty = 0.0
        if direction < 0:
            direction_penalty = 2.0  # 強めに減点

        # ----------------------------------------------------
        # volume（流動性チェック）
        # ----------------------------------------------------
        volume_boost = 0.0
        if volume > 0:
            volume_boost = np.tanh(volume / 1e6) * 2.0

        # ----------------------------------------------------
        # VWAP（ブレイク確認）
        # ----------------------------------------------------
        vwap_bonus = 0.0
        if price > vwap > 0:
            vwap_bonus = 1.5

        # ----------------------------------------------------
        # acceleration（最重要）
        # ----------------------------------------------------
        accel_bonus = acceleration * 3.0

        # ----------------------------------------------------
        # 元スコア（完全保持）
        # ----------------------------------------------------
        final_score = (
            summary_score * 1.0
            + ranking_score * 0.8
            + ai_prob * 10.0
        )

        # ----------------------------------------------------
        # 🔥 拡張スコア
        # ----------------------------------------------------
        final_score += (
            accel_bonus
            + velocity * 1.5
            + volume_boost
            + vwap_bonus
        )

        # ペナルティ
        final_score -= direction_penalty

        # ----------------------------------------------------
        # 安定化
        # ----------------------------------------------------
        final_score = np.tanh(final_score / 10.0) * 10.0
        final_score = _clip(final_score)

        entry["final_score"] = final_score

        # ----------------------------------------------------
        # 最終判定（元ロジック保持）
        # ----------------------------------------------------
        if final_score >= 8.0:
            entry["entry_decision"] = "APPROVED"
            return entry

        return None

    except Exception:
        logger.exception("[final_entry_judge] failed")
        return None