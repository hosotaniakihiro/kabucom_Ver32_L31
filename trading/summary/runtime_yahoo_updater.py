# ============================================================
# File   : trading/summary/runtime_yahoo_updater.py
# Version: Ver25.9.1-FINAL-RUNTIME-YAHOO-INTEGRATED-NOARG-STABLE
# ------------------------------------------------------------
# ✔ GlobalData 最新設計に完全準拠
# ✔ scheduler 互換（引数なし API）
# ✔ Yahoo 専用 DF を runtime から論理的に分離
# ✔ summary_1min（latest_summary_by_interval[1]）を唯一の正とする
# ✔ 差分検知・上書き更新・ログ出力すべて保持
# ✔ pandas / runtime 安全性を追加補強
# ✔ ENTRY / MA / AI への影響なし
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from typing import Optional

from global_state import global_data

logger = logging.getLogger(__name__)

# ============================================================
# メイン API（★引数なし）
# ============================================================

def update_runtime_with_yahoo() -> None:
    """
    Yahoo 補助データが存在する場合のみ、
    runtime summary_1min に差分マージする。

    【重要思想】
    - runtime では「Yahoo / push / kabu-station」を分離しない
    - summary_1min が唯一の正
    - Yahoo データは“存在すれば使う補助情報”
    """

    try:
        # ----------------------------------------------------
        # Yahoo 補助 DF 取得（存在しなければ即終了）
        # ----------------------------------------------------
        yahoo_df: Optional[pd.DataFrame] = getattr(
            global_data, "yahoo_runtime_df_1min", None
        )

        if yahoo_df is None or yahoo_df.empty:
            logger.debug("[RUNTIME_YAHOO] skip: yahoo df empty or not set")
            return

        # ----------------------------------------------------
        # runtime 側の正（summary_1min）
        # ----------------------------------------------------
        base_df = global_data.latest_summary_by_interval.get(1)

        if base_df is None or base_df.empty:
            logger.info("[RUNTIME_YAHOO] skip: no 1min summary")
            return

        # ----------------------------------------------------
        # 必須カラムチェック
        # ----------------------------------------------------
        for col in ("symbol", "timestamp"):
            if col not in yahoo_df.columns:
                logger.warning(
                    "[RUNTIME_YAHOO] yahoo df missing column: %s", col
                )
                return
            if col not in base_df.columns:
                logger.warning(
                    "[RUNTIME_YAHOO] base summary missing column: %s", col
                )
                return

        # ----------------------------------------------------
        # 差分マージ（symbol × timestamp）
        # ----------------------------------------------------
        try:
            merged = (
                base_df.merge(
                    yahoo_df,
                    on=["symbol", "timestamp"],
                    how="left",
                    suffixes=("", "_yahoo"),
                )
                .copy()  # ★ runtime 安全のため明示コピー
            )
        except Exception:
            logger.exception("[RUNTIME_YAHOO] merge failed")
            return

        # ----------------------------------------------------
        # 上書き対象カラム
        # ----------------------------------------------------
        overwrite_cols = [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]

        # 行単位での更新検知
        updated_mask = pd.Series(False, index=merged.index)

        for col in overwrite_cols:
            yahoo_col = f"{col}_yahoo"
            if yahoo_col not in merged.columns:
                continue

            mask = merged[yahoo_col].notna()
            if mask.any():
                merged.loc[mask, col] = merged.loc[mask, yahoo_col]
                updated_mask |= mask

        updated_rows = int(updated_mask.sum())

        # ----------------------------------------------------
        # Yahoo 側一時カラム削除
        # ----------------------------------------------------
        drop_cols = [c for c in merged.columns if c.endswith("_yahoo")]
        if drop_cols:
            merged.drop(columns=drop_cols, inplace=True)

        # ----------------------------------------------------
        # 並び順を安定化（後段処理用）
        # ----------------------------------------------------
        merged.sort_values(
            by=["symbol", "timestamp"],
            inplace=True,
            kind="stable",
        )

        # ----------------------------------------------------
        # runtime 反映（唯一の正を更新）
        # ----------------------------------------------------
        global_data.latest_summary_by_interval[1] = merged

        logger.info(
            "[RUNTIME_YAHOO] summary_1min updated rows=%d",
            updated_rows,
        )

    except Exception:
        logger.exception("[RUNTIME_YAHOO] update failed")

# ============================================================
# END
# ============================================================