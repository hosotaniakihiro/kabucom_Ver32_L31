# ============================================================
# File   : trading/ai/meta_selector.py
# Version: Ver2.0-ABSOLUTE-FINAL-HARDENED-DETERMINISTIC
# ------------------------------------------------------------
# ✔ regime × cluster × bandit 統合
# ✔ attack signal 統合
# ✔ MTF composite 対応
# ✔ スキャル型設計
# ✔ 完全自律モード想定
# ✔ NaN / inf / object 完全耐性
# ✔ 外部モジュール例外完全吸収
# ✔ deterministic
# ✔ extensible
# ✔ dtype安全固定
# ============================================================

from __future__ import annotations
import pandas as pd
import numpy as np
import logging
from typing import Dict, Any

from trading.ai.regime_detector import detect_regime
from trading.ai.cluster_router import route_cluster
from trading.ai.bandit_engine import select_bandit_weight
from trading.ai.attack_detector import detect_attack_row

logger = logging.getLogger(__name__)


# ============================================================
# 安全数値変換（完全耐性）
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
# 安全文字列
# ============================================================

def _safe_str(v: Any, default: str = "UNKNOWN") -> str:
    try:
        if v is None:
            return default
        return str(v)
    except Exception:
        return default


# ============================================================
# 外部モジュール安全ラッパー
# ============================================================

def _safe_regime(row: dict) -> str:
    try:
        return _safe_str(detect_regime(row), "RANGE")
    except Exception:
        logger.exception("regime detect error")
        return "RANGE"


def _safe_cluster(row: dict) -> str:
    try:
        return _safe_str(route_cluster(row), "DEFAULT")
    except Exception:
        logger.exception("cluster route error")
        return "DEFAULT"


def _safe_bandit(cluster: str, regime: str) -> float:
    try:
        return _safe(select_bandit_weight(cluster, regime), 1.0)
    except Exception:
        logger.exception("bandit error")
        return 1.0


def _safe_attack(row: dict) -> Dict[str, float]:
    try:
        attack = detect_attack_row(row)
        return {
            "long_attack": bool(attack.get("long_attack", False)),
            "short_attack": bool(attack.get("short_attack", False)),
            "attack_strength": _safe(attack.get("attack_strength", 0.0))
        }
    except Exception:
        logger.exception("attack detect error")
        return {
            "long_attack": False,
            "short_attack": False,
            "attack_strength": 0.0
        }


# ============================================================
# Composite計算（決定論的）
# ============================================================

def _compute_composite(
    score: float,
    mtf: float,
    slope: float,
    attack_strength: float,
    bandit_weight: float,
) -> float:

    composite = (
        score * 0.5
        + mtf * 0.3
        + attack_strength * 2.0
        + slope * 50.0
    )

    composite *= bandit_weight

    if np.isnan(composite) or np.isinf(composite):
        return 0.0

    return float(composite)


# ============================================================
# メタ最終判定（完全安定版）
# ============================================================

def meta_select_action(row: dict) -> dict:
    """
    単一銘柄最終判断
    完全例外耐性 / 完全決定論
    """

    # ========= 基本値 =========

    score = _safe(row.get("score_total"))
    mtf = _safe(row.get("mtf_score"))
    slope = _safe(row.get("ma75_slope"))

    # ========= 外部判断 =========

    regime = _safe_regime(row)
    cluster = _safe_cluster(row)
    bandit_weight = _safe_bandit(cluster, regime)

    attack = _safe_attack(row)
    long_attack = attack["long_attack"]
    short_attack = attack["short_attack"]
    attack_strength = attack["attack_strength"]

    # ========= composite =========

    composite = _compute_composite(
        score=score,
        mtf=mtf,
        slope=slope,
        attack_strength=attack_strength,
        bandit_weight=bandit_weight,
    )

    # ========= LONG =========

    if (
        composite > 5.0
        and long_attack
        and regime in ("BULL", "RANGE")
    ):
        confidence = min(1.0, composite / 20.0)
        return {
            "action": "LONG",
            "confidence": round(confidence, 4),
            "reason": f"LONG|regime={regime}|cluster={cluster}|bandit={bandit_weight:.2f}"
        }

    # ========= SHORT =========

    if (
        composite < -5.0
        and short_attack
        and regime in ("BEAR", "RANGE")
    ):
        confidence = min(1.0, abs(composite) / 20.0)
        return {
            "action": "SHORT",
            "confidence": round(confidence, 4),
            "reason": f"SHORT|regime={regime}|cluster={cluster}|bandit={bandit_weight:.2f}"
        }

    # ========= SKIP =========

    return {
        "action": "SKIP",
        "confidence": 0.0,
        "reason": f"NO_SIGNAL|regime={regime}|cluster={cluster}"
    }


# ============================================================
# DataFrame一括処理（安全高速版）
# ============================================================

def meta_select_df(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    results = []

    for _, row in df.iterrows():
        res = meta_select_action(row.to_dict())
        results.append(res)

    df["meta_action"] = [r["action"] for r in results]
    df["meta_confidence"] = _safe(
        pd.Series([r["confidence"] for r in results])
    )
    df["meta_reason"] = [r["reason"] for r in results]

    # dtype固定
    df["meta_action"] = df["meta_action"].astype(str)
    df["meta_reason"] = df["meta_reason"].astype(str)
    df["meta_confidence"] = pd.to_numeric(
        df["meta_confidence"], errors="coerce"
    ).fillna(0.0)

    return df