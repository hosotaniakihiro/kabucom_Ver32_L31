# ============================================================
# File   : AI/features/feature_builder.py
# Ver12.4-FINAL-SYMBOLS0-DEBUG-HARDENED
# ------------------------------------------------------------
# ✔ 必須カラム判定バグ修正（df.columns 基準）
# ✔ close / close_price 自動吸収
# ✔ symbol None / NaN を明示 reject
# ✔ skip 理由を必ずログ出力
# ✔ symbols=0 の原因が100%特定可能
# ✔ entry_decision / source 安全正規化
# ============================================================

import logging
import pandas as pd
from typing import Dict

logger = logging.getLogger(__name__)

# ============================================================
# 必須カラム（MTF最低条件）
# ============================================================
REQUIRED_COLS = [
    "symbol",
    "close_price",
    "ma5",
    "ma25",
    "rsi",
    "macd",
    "score_total",
    "entry_decision",
    "source",
]


# ============================================================
def build_ai_features(df: pd.DataFrame) -> Dict[str, dict]:
    """
    AI用特徴量生成
    return: dict[symbol] = feature_dict
    """

    features: Dict[str, dict] = {}

    # --------------------------------------------------------
    # DF存在チェック
    # --------------------------------------------------------
    if df is None or df.empty:
        logger.error("[AI FEATURES] input df is EMPTY or None")
        return features

    logger.info(
        "[AI FEATURES] input rows=%d cols=%s",
        len(df),
        list(df.columns),
    )

    df = df.copy()

    # --------------------------------------------------------
    # close / close_price 吸収
    # --------------------------------------------------------
    if "close_price" not in df.columns:
        if "close" in df.columns:
            logger.warning(
                "[AI FEATURES] close_price missing → using close"
            )
            df["close_price"] = df["close"]
        else:
            logger.error(
                "[AI FEATURES][FATAL] neither close_price nor close exists"
            )
            return features

    # --------------------------------------------------------
    # 必須カラム存在チェック（DFレベル）
    # --------------------------------------------------------
    missing_cols = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing_cols:
        logger.error(
            "[AI FEATURES][FATAL] missing required columns=%s",
            missing_cols,
        )
        return features

    # --------------------------------------------------------
    # 行ごとの feature 構築
    # --------------------------------------------------------
    for idx, row in df.iterrows():
        symbol = row.get("symbol")

        # ------------------------------
        # symbol チェック
        # ------------------------------
        if symbol is None or pd.isna(symbol):
            logger.warning(
                "[AI FEATURES][SKIP] idx=%s symbol is None/NaN",
                idx,
            )
            continue

        symbol = str(symbol)

        # ------------------------------
        # NaN チェック（必須項目）
        # ------------------------------
        nan_cols = [c for c in REQUIRED_COLS if pd.isna(row[c])]
        if nan_cols:
            logger.warning(
                "[AI FEATURES][SKIP] %s NaN_cols=%s",
                symbol,
                nan_cols,
            )
            continue

        # ------------------------------
        # source / entry_decision 正規化
        # ------------------------------
        source = row.get("source")
        if source is None or pd.isna(source):
            logger.warning(
                "[AI FEATURES][SKIP] %s source is None/NaN",
                symbol,
            )
            continue

        entry_decision = row.get("entry_decision")
        if entry_decision is None or pd.isna(entry_decision):
            entry_decision = "NONE"

        # ------------------------------
        # 型変換 + feature 構築
        # ------------------------------
        try:
            feature = {
                "symbol": symbol,
                "close_price": float(row["close_price"]),
                "ma5": float(row["ma5"]),
                "ma25": float(row["ma25"]),
                "rsi": float(row["rsi"]),
                "macd": float(row["macd"]),
                "score_total": float(row["score_total"]),
                "entry_decision": str(entry_decision),
                "source": str(source),
            }

            if symbol in features:
                logger.warning(
                    "[AI FEATURES][OVERWRITE] duplicate symbol=%s idx=%s",
                    symbol,
                    idx,
                )

            features[symbol] = feature

        except Exception as e:
            logger.exception(
                "[AI FEATURES][ERROR] %s build failed: %s",
                symbol,
                e,
            )

    # --------------------------------------------------------
    # 完了ログ
    # --------------------------------------------------------
    logger.info(
        "[AI FEATURES] built symbols=%d",
        len(features),
    )

    if not features:
        logger.error(
            "[AI FEATURES][RESULT] symbols=0 → ALL ROWS SKIPPED"
        )

    return features
