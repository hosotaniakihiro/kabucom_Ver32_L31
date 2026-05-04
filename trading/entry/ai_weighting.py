# ============================================================
# File   : AI/weighting/ai_weighting.py
# ------------------------------------------------------------
# AI 判定結果の重み付け（最終 confidence 算出）
# ------------------------------------------------------------
# ✔ SUMMARY / RANKING / *_AI を統一評価
# ✔ confidence を生で使わない（危険）
# ✔ source × interval × dominant_ratio を合成
# ✔ pending_entries（dict / list 混在）完全耐性
# ✔ entry_controller / ai_enricher 共通利用
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

# ============================================================
# 重み定義（実戦チューニング済み）
# ============================================================

# source ごとの信頼度重み
SOURCE_WEIGHT: Dict[str, float] = {
    "SUMMARY_AI": 1.00,
    "RANKING_AI": 0.85,
    "SUMMARY": 0.70,
    "RANKING": 0.60,
    "UNKNOWN": 0.50,
}

# interval ごとの安定度重み
INTERVAL_WEIGHT: Dict[int, float] = {
    1: 0.80,   # ノイズ多
    3: 0.95,
    5: 1.00,
}

# dominant_ratio の最低許容ライン
DOMINANT_MIN = 0.55


# ============================================================
# dominant_ratio → 係数
# ============================================================
def _dominant_factor(dominant_ratio) -> float:
    """
    dominant_ratio を confidence 係数に変換
    """
    if dominant_ratio is None:
        return 0.70

    try:
        d = float(dominant_ratio)
    except Exception:
        return 0.70

    # 構造が弱い場合は急減衰
    if d < DOMINANT_MIN:
        return max(0.30, d)

    # 0.55〜1.0 → 0.8〜1.1 に圧縮
    return 0.8 + (d - DOMINANT_MIN) * 0.6


# ============================================================
# confidence 正規化
# ============================================================
def _normalize_confidence(conf) -> float:
    """
    AI 生 confidence を 0.0〜1.0 に正規化
    """
    if conf is None:
        return 0.0

    try:
        c = float(conf)
    except Exception:
        return 0.0

    return max(0.0, min(1.0, c))


# ============================================================
# メイン：重み付き confidence 算出
# ============================================================
def calc_weighted_confidence(entry: dict) -> float:
    """
    PendingEntry(dict) から最終 confidence を算出

    Returns:
        float: 0.0〜1.0
    """

    raw_conf = _normalize_confidence(entry.get("confidence"))

    source = entry.get("source", "UNKNOWN")
    interval = entry.get("interval", 1)
    dominant_ratio = entry.get("dominant_ratio")

    source_w = SOURCE_WEIGHT.get(source, SOURCE_WEIGHT["UNKNOWN"])
    interval_w = INTERVAL_WEIGHT.get(interval, 0.80)
    dom_w = _dominant_factor(dominant_ratio)

    weighted = raw_conf * source_w * interval_w * dom_w
    weighted = max(0.0, min(1.0, weighted))

    logger.debug(
        "[AI_WEIGHT] %s src=%s int=%s "
        "raw=%.3f src_w=%.2f int_w=%.2f dom_w=%.2f -> %.3f",
        entry.get("symbol"),
        source,
        interval,
        raw_conf,
        source_w,
        interval_w,
        dom_w,
        weighted,
    )

    return weighted


# ============================================================
# entry に反映（破壊的）
# ============================================================
def apply_weighted_confidence(entry: dict) -> dict:
    """
    entry に weight_confidence を付与
    """
    if not isinstance(entry, dict):
        return entry

    entry["weight_confidence"] = calc_weighted_confidence(entry)
    return entry


# ============================================================
# pending_entries 全体に適用（dict / list 混在耐性）
# ============================================================
def apply_weighted_confidence_bulk(pending_entries):
    """
    global_data.pending_entries 用

    想定構造:
      {
        symbol: PendingEntry(dict)
        symbol: [PendingEntry, PendingEntry]
      }
    """

    if not pending_entries or not isinstance(pending_entries, dict):
        return pending_entries

    for key, value in list(pending_entries.items()):

        # list 型
        if isinstance(value, list):
            for e in value:
                if isinstance(e, dict):
                    apply_weighted_confidence(e)

        # dict 型
        elif isinstance(value, dict):
            apply_weighted_confidence(value)

    return pending_entries
