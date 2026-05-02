# ============================================================
# File   : AI/analysis/build_ranking_block_zone.py
# Ver    : 1.0.0-RANKING-BLOCK-ZONE-BUILDER
# ------------------------------------------------------------
# ✔ persistence × volume_speed 勝率マトリクス読込
# ✔ win_rate < threshold のゾーン抽出
# ✔ JSON保存
# ✔ 安全設計
# ============================================================

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from AI.analysis.build_ranking_winrate_matrix import build_winrate_matrix

logger = logging.getLogger(__name__)

# ==============================
# 設定
# ==============================

WINRATE_THRESHOLD = 0.50
OUTPUT_PATH = Path("AI/config/ranking_block_zone.json")


# ============================================================
# ブロックゾーン生成
# ============================================================

def build_block_zone():

    logger.info("🧱 Building ranking block zones...")

    matrix = build_winrate_matrix()

    if matrix is None or matrix.empty:
        logger.warning("No winrate matrix available.")
        return None

    block_zones = []

    for persistence in matrix.index:
        for vol_bin in matrix.columns:

            win_rate = matrix.loc[persistence, vol_bin]

            if pd.isna(win_rate):
                continue

            if win_rate < WINRATE_THRESHOLD:
                block_zones.append({
                    "rank_persistence": int(persistence),
                    "volume_speed_bin": str(vol_bin),
                    "win_rate": round(float(win_rate), 3),
                })

    # 保存
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "threshold": WINRATE_THRESHOLD,
            "block_zones": block_zones,
        }, f, indent=4)

    logger.info(f"🧱 Block zones saved → {OUTPUT_PATH}")

    return block_zones


# ============================================================
# 実行
# ============================================================

if __name__ == "__main__":
    zones = build_block_zone()
    print("\n=== BLOCK ZONES ===\n")
    print(zones)