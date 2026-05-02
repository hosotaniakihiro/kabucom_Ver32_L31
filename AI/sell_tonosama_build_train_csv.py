# ============================================================
# File: AI/sell_tonosama_build_train_csv.py
# ------------------------------------------------------------
# 殿様イナゴ（SELL）専用 学習CSV生成スクリプト
#
# ✔ ENTRY 時点の特徴量を保存
# ✔ 60秒以内の最小リターンでラベル生成
# ✔ 本番トレードロジックとは完全分離
# ✔ 夜間バッチ専用
# ============================================================

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import List

import pandas as pd

from AI.sell_tonosama_feature_builder import build_sell_feature_row
from AI.sell_tonosama_label_builder import build_sell_label


# ============================================================
# 設定
# ============================================================

OUT_CSV = Path("sell_tonosama_train.csv")

FIELDS = [
    "price_velocity",
    "volume_drop",
    "rank_fall",
    "sell_pressure",
    "spread_ratio",
    "minute_from_open",
    "label",
]


# ============================================================
# CSV 初期化
# ============================================================

def _ensure_csv() -> None:
    if not OUT_CSV.exists():
        with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()


# ============================================================
# 1レコード生成
# ============================================================

def append_one_row(
    *,
    summary_1m: pd.DataFrame,
    ranking,
    board,
    now: dt.datetime,
    future_prices: List[float],
) -> None:
    """
    SELL殿様 学習用 1 行を CSV に追記する
    """

    # --------------------------------------------------------
    # 特徴量生成
    # --------------------------------------------------------
    feature = build_sell_feature_row(
        summary_1m=summary_1m,
        ranking=ranking,
        board=board,
        now=now,
    )

    if not feature:
        return

    # --------------------------------------------------------
    # ラベル生成
    # --------------------------------------------------------
    entry_price = float(summary_1m.iloc[-1].close)

    label = build_sell_label(
        entry_price=entry_price,
        future_prices=future_prices,
    )

    feature["label"] = label

    # --------------------------------------------------------
    # CSV 追記
    # --------------------------------------------------------
    _ensure_csv()

    with OUT_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writerow(feature)


# ============================================================
# バッチ実行用（例）
# ============================================================

def main():
    """
    ※ 実際の環境では以下を差し替える
      - summary_1m
      - ranking
      - board
      - future_prices
    """
    raise RuntimeError(
        "This script must be called from batch pipeline "
        "with actual market data."
    )


if __name__ == "__main__":
    main()