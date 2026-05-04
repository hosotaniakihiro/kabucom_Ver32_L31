# ============================================================
# AI/train/entry/immediate_quality_check.py
# ------------------------------------------------------------
# 即益ラベル品質チェック（学習・検証専用）
# ------------------------------------------------------------
# ✔ 実運用コードからは絶対に呼ばない
# ✔ 学習データの健全性チェック専用
# ✔ BUY / SELL 別の偏り確認
# ✔ threshold / lookahead の妥当性検証
# ============================================================

import logging
from typing import Dict, Any

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
def immediate_quality_check(
    df: pd.DataFrame,
    *,
    label_col: str = "label_immediate_profit",
    side_col: str = "side",
    min_samples: int = 200,
    min_hit_rate: float = 0.05,
    max_hit_rate: float = 0.95,
) -> Dict[str, Any]:
    """
    即益ラベルの品質チェック

    Args:
        df (pd.DataFrame):
            学習用データ
            必須列:
              - label_col（0/1）
              - side_col（BUY / SELL）
        label_col (str):
            即益ラベル列名
        side_col (str):
            売買方向列名
        min_samples (int):
            学習に最低限必要なサンプル数
        min_hit_rate (float):
            即益率の下限（低すぎ＝意味なし）
        max_hit_rate (float):
            即益率の上限（高すぎ＝未来リーク疑い）

    Returns:
        dict:
            {
              "ok": bool,
              "reason": str,
              "stats": {...},
            }
    """

    # --------------------------------------------------------
    # 基本ガード
    # --------------------------------------------------------
    if df is None or df.empty:
        return _fail("no_data")

    if label_col not in df.columns:
        return _fail(f"missing_column:{label_col}")

    if side_col not in df.columns:
        return _fail(f"missing_column:{side_col}")

    # --------------------------------------------------------
    # ラベル正規化
    # --------------------------------------------------------
    labels = df[label_col].dropna()

    total = len(labels)
    if total < min_samples:
        return _fail(f"too_few_samples({total}<{min_samples})")

    # bool / int 混在耐性
    labels = labels.astype(int)

    hit_rate = labels.mean()

    # --------------------------------------------------------
    # 全体即益率チェック
    # --------------------------------------------------------
    if hit_rate < min_hit_rate:
        return _fail(f"hit_rate_too_low({hit_rate:.3f})")

    if hit_rate > max_hit_rate:
        return _fail(f"hit_rate_too_high({hit_rate:.3f})")

    # --------------------------------------------------------
    # BUY / SELL 別チェック
    # --------------------------------------------------------
    side_stats = {}

    for side in ("BUY", "SELL"):
        df_side = df[df[side_col] == side]
        if df_side.empty:
            side_stats[side] = {
                "samples": 0,
                "hit_rate": None,
            }
            continue

        lbl = df_side[label_col].dropna().astype(int)
        side_stats[side] = {
            "samples": len(lbl),
            "hit_rate": lbl.mean() if len(lbl) > 0 else None,
        }

    # --------------------------------------------------------
    # 片側極端チェック
    # --------------------------------------------------------
    buy_n = side_stats.get("BUY", {}).get("samples", 0)
    sell_n = side_stats.get("SELL", {}).get("samples", 0)

    if buy_n == 0 or sell_n == 0:
        logger.warning("⚠ BUY/SELL imbalance detected BUY=%d SELL=%d", buy_n, sell_n)

    # --------------------------------------------------------
    # OK
    # --------------------------------------------------------
    stats = {
        "total_samples": total,
        "hit_rate": hit_rate,
        "by_side": side_stats,
    }

    logger.info(
        "✅ immediate_quality_check OK total=%d hit_rate=%.3f BUY=%s SELL=%s",
        total,
        hit_rate,
        side_stats.get("BUY"),
        side_stats.get("SELL"),
    )

    return {
        "ok": True,
        "reason": "ok",
        "stats": stats,
    }


# ============================================================
# 内部ユーティリティ
# ============================================================
def _fail(reason: str) -> Dict[str, Any]:
    logger.warning("❌ immediate_quality_check FAILED: %s", reason)
    return {
        "ok": False,
        "reason": reason,
        "stats": {},
    }
