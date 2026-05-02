# ============================================================
# File   : AI/train/exit/build_collapse_dataset.py
# Version: V32-FINAL-COLLAPSE-DATASET-BUILDER
# ------------------------------------------------------------
# ✔ long / short 両対応
# ✔ 未来リターンベース collapse_target生成
# ✔ NaN完全吸収
# ✔ config.paths 連携
# ✔ UNC安全
# ✔ 例外安全
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from pathlib import Path

from config.paths import get_path, ensure_dirs

logger = logging.getLogger(__name__)


# ============================================================
# 設定
# ============================================================

HORIZON = 3              # 何本先を見るか（1min足）
COLLAPSE_THRESHOLD = -0.02   # -2%でcollapse
SIDE_COLUMN = "side"


# ============================================================
# Target生成
# ============================================================

def build_collapse_target(
    df: pd.DataFrame,
    horizon: int = HORIZON,
    threshold: float = COLLAPSE_THRESHOLD,
) -> pd.DataFrame:

    df = df.copy()

    # 将来最安値
    df["future_min"] = (
        df["close_price"]
        .rolling(horizon)
        .min()
        .shift(-horizon)
    )

    future_return = (
        df["future_min"] / df["close_price"] - 1
    )

    df["collapse_target"] = (
        future_return <= threshold
    ).astype(int)

    return df


# ============================================================
# 特徴量生成（最低限版）
# ============================================================

def add_basic_features(df: pd.DataFrame) -> pd.DataFrame:

    df = df.copy()

    # MFE from peak
    df["mfe_from_peak"] = (
        df["high_price"].rolling(10).max() / df["close_price"] - 1
    )

    # volume_decay
    df["volume_decay"] = (
        df["volume"] /
        df["volume"].rolling(5).mean()
    )

    # spread_expansion
    df["spread_expansion"] = (
        (df["high_price"] - df["low_price"]) /
        df["close_price"]
    )

    # ATR ratio（仮）
    df["atr_ratio"] = (
        df.get("atr_1min", 0.0) /
        df["close_price"]
    )

    # NaN吸収
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.fillna(0.0)

    return df


# ============================================================
# Main Builder
# ============================================================

def build_dataset(
    input_csv: Path,
    side: str = "LONG",
    save: bool = True
):

    try:
        logger.info("[DATASET] Loading %s", input_csv)

        df = pd.read_csv(input_csv)

        if SIDE_COLUMN in df.columns:
            df = df[df[SIDE_COLUMN] == side]

        if df.empty:
            logger.warning("[DATASET] No data for side=%s", side)
            return None

        # 特徴量追加
        df = add_basic_features(df)

        # collapseターゲット生成
        df = build_collapse_target(df)

        # 最終整形
        df = df.replace([np.inf, -np.inf], np.nan)
        df = df.fillna(0.0)

        logger.info(
            "[DATASET] Built collapse dataset (%s) rows=%d",
            side,
            len(df)
        )

        if save:
            _save_dataset(df, side)

        return df

    except Exception:
        logger.exception("[DATASET] build failed")
        raise


# ============================================================
# Save
# ============================================================

def _save_dataset(df: pd.DataFrame, side: str):

    ensure_dirs()

    output_dir = get_path("ai_train_exit")
    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"collapse_train_{side.lower()}.csv"
    output_path = output_dir / filename

    df.to_csv(output_path, index=False)

    logger.info("[DATASET] Saved → %s", output_path)


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":

    train_dir = get_path("ai_train_exit")
    input_csv = train_dir / "raw_exit_events.csv"

    build_dataset(input_csv, side="LONG")
    build_dataset(input_csv, side="SHORT")