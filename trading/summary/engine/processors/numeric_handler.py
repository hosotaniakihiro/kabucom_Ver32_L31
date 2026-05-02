# ============================================================
# File   : trading/summary/persistence/preprocess/numeric_handler.py
# Version: Ver1.0-PRODUCTION-NUMERIC-HANDLER-HARDENED
# ------------------------------------------------------------
# ✔ summary_saver_bulk から完全分離
# ✔ Ver21.1 ロジック完全互換＋強化
# ✔ inf / -inf → NaN 変換
# ✔ numeric dtype の float統一
# ✔ object混入対策（安全キャスト）
# ✔ 数値列のみ処理（非破壊）
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# MAIN NUMERIC NORMALIZATION
# ============================================================

def normalize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # inf → NaN
    # --------------------------------------------------------
    inf_mask = df.isin([np.inf, -np.inf])

    if inf_mask.any().any():
        count = inf_mask.sum().sum()
        logger.warning("[NUMERIC] replaced inf → NaN (%d cells)", count)

        df = df.replace([np.inf, -np.inf], np.nan)

    # --------------------------------------------------------
    # numeric列取得
    # --------------------------------------------------------
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()

    # --------------------------------------------------------
    # objectだけど数値っぽい列も検出
    # --------------------------------------------------------
    for col in df.columns:

        if col in numeric_cols:
            continue

        if df[col].dtype == "object":

            # 数値変換できるか試す
            converted = pd.to_numeric(df[col], errors="coerce")

            # 変換成功率チェック
            success_ratio = converted.notna().mean()

            # 80%以上なら数値列とみなす
            if success_ratio > 0.8:

                logger.info(
                    "[NUMERIC] auto-cast column → %s (%.2f)",
                    col,
                    success_ratio
                )

                df[col] = converted
                numeric_cols.append(col)

    # --------------------------------------------------------
    # float統一
    # --------------------------------------------------------
    for col in numeric_cols:

        try:
            df[col] = df[col].astype(float)

        except Exception:
            logger.warning(
                "[NUMERIC] cast failed → %s",
                col
            )

    return df