# ============================================================
# File   : trading/regime/sector_rotation.py
# Version: FINAL-SECTOR-ROTATION-V1
# ------------------------------------------------------------
# ✔ セクター強弱スコア算出
# ✔ モメンタム型回転検知
# ✔ 攻撃型ブースト倍率出力
# ✔ AI特徴量出力
# ✔ None完全耐性
# ============================================================

from typing import Dict, Any


def _safe(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
    if not row:
        return default
    try:
        return float(row.get(key, default))
    except Exception:
        return default


# ------------------------------------------------------------
# セクター強度スコア
# ------------------------------------------------------------

def sector_strength_score(sector_row: Dict[str, Any]) -> float:
    momentum = _safe(sector_row, "sector_momentum")
    slope = _safe(sector_row, "sector_ma75_slope")
    volume = _safe(sector_row, "sector_volume_slope")

    score = momentum * 0.5 + slope * 0.3 + volume * 0.2
    return score


# ------------------------------------------------------------
# セクター回転ブースト倍率
# ------------------------------------------------------------

def sector_boost_multiplier(
    symbol: str,
    sector_map: Dict[str, str],
    sector_state: Dict[str, Dict[str, Any]],
) -> float:

    sector = sector_map.get(symbol)
    if not sector:
        return 1.0

    sector_row = sector_state.get(sector)
    if not sector_row:
        return 1.0

    strength = sector_strength_score(sector_row)

    return max(0.7, min(1 + strength * 0.6, 1.8))


# ------------------------------------------------------------
# AI特徴量
# ------------------------------------------------------------

def sector_feature(symbol, sector_map, sector_state) -> float:
    sector = sector_map.get(symbol)
    if not sector:
        return 0.0
    return sector_strength_score(sector_state.get(sector))