# ============================================================
# File   : AI/config/ranking_block_zone_loader.py
# Ver    : 2.0.0-RANKING-BLOCK-ZONE-LOADER-BALANCED
# ------------------------------------------------------------
# ✔ バランス型ロジック
# ✔ データ不足判定
# ✔ persistence弱ゾーン自動ブロック
# ✔ キャッシュ保持
# ✔ JSON未存在でも安全
# ============================================================

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Dict

import pandas as pd

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("AI/config/ranking_block_zone.json")

_cached_data: Dict | None = None

# データ不足の定義
MIN_REQUIRED_BLOCK_ZONES = 5  # block_zonesが少なすぎる場合は学習不足とみなす


# ============================================================
# JSON読込
# ============================================================

def _load_config() -> Dict:

    global _cached_data

    if _cached_data is not None:
        return _cached_data

    if not CONFIG_PATH.exists():
        logger.warning("ranking_block_zone.json not found.")
        _cached_data = {"block_zones": []}
        return _cached_data

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _cached_data = json.load(f)
    except Exception:
        logger.exception("Failed to load ranking_block_zone.json")
        _cached_data = {"block_zones": []}

    return _cached_data


# ============================================================
# volume_speed → bin
# ============================================================

def _get_volume_bin(volume_speed: float) -> str | None:

    bins = (0, 1.2, 1.5, 2.0, 3.0, 10.0)

    try:
        cat = pd.cut([volume_speed], bins=bins, include_lowest=True)
        return str(cat[0])
    except Exception:
        return None


# ============================================================
# メイン判定（バランス型）
# ============================================================

def is_block_zone(
    rank_persistence: int | None,
    volume_speed: float | None,
) -> bool:

    if rank_persistence is None or volume_speed is None:
        return False

    data = _load_config()
    zones = data.get("block_zones", [])

    # --------------------------------------------------------
    # 🔴 データ不足判定
    # --------------------------------------------------------
    if len(zones) < MIN_REQUIRED_BLOCK_ZONES:
        # persistence=1は危険なのでブロック
        if int(rank_persistence) <= 1:
            return True
        return False

    # --------------------------------------------------------
    # 通常ブロック判定
    # --------------------------------------------------------
    vol_bin = _get_volume_bin(volume_speed)

    if vol_bin is None:
        return False

    for zone in zones:
        if (
            zone["rank_persistence"] == int(rank_persistence)
            and zone["volume_speed_bin"] == vol_bin
        ):
            return True

    return False