# ============================================================
# File   : trading/summary/runtime_yahoo_ma_updater.py
# Version: Ver26.1-ABSOLUTE-FINAL-RUNTIME-MA-STABLE
# ------------------------------------------------------------
# ✔ Ver26.0 完全保持（削除ゼロ）
# ✔ duplicate列削除安全化（最初の1本のみ保持）
# ✔ 復元ロジック撤廃（長さ不一致完全排除）
# ✔ close DataFrame化完全防御
# ✔ symbol / datetime 型保証
# ✔ MA前最低構成保証
# ✔ None / object 完全耐性
# ✔ Scheduler絶対停止禁止
# ✔ ENTRY / AI / EXIT 副作用ゼロ
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
from typing import Optional

from global_state import global_data
from trading.yahoo.indicators.ma_builder import build_yahoo_ma_1min

logger = logging.getLogger(__name__)


# ============================================================
# メイン API
# ============================================================

def update_yahoo_ma_cache() -> None:

    try:
        # ----------------------------------------------------
        # 入力取得（唯一の正）
        # ----------------------------------------------------
        latest_map = getattr(global_data, "latest_summary_by_interval", None)

        if latest_map is None:
            logger.info("[RUNTIME_MA] skip: latest_summary_by_interval missing")
            return

        base_df: Optional[pd.DataFrame] = latest_map.get(1)

        if base_df is None or not isinstance(base_df, pd.DataFrame):
            logger.info("[RUNTIME_MA] skip: no 1min summary")
            return

        if base_df.empty:
            logger.info("[RUNTIME_MA] skip: 1min summary empty")
            return

        # ----------------------------------------------------
        # defensive copy（原本絶対保護）
        # ----------------------------------------------------
        base_df = base_df.copy()

        # ----------------------------------------------------
        # ログ
        # ----------------------------------------------------
        try:
            logger.info(
                "[RUNTIME_MA] BASE rows=%d symbols=%d",
                len(base_df),
                base_df["symbol"].nunique()
                if "symbol" in base_df.columns else 0,
            )
        except Exception:
            pass

        # ----------------------------------------------------
        # 🔥 duplicate列安全削除（最初の1本のみ保持）
        # ----------------------------------------------------
        if base_df.columns.duplicated().any():
            dup_cols = list(base_df.columns[base_df.columns.duplicated()])
            logger.warning(
                "[RUNTIME_MA] DUPLICATE COLUMNS DETECTED → dropping duplicates: %s",
                dup_cols,
            )

            # 最初に出現した列のみ保持
            base_df = base_df.loc[:, ~base_df.columns.duplicated()]

        # ----------------------------------------------------
        # close DataFrame防御
        # ----------------------------------------------------
        if "close" not in base_df.columns:
            logger.warning("[RUNTIME_MA] close missing → skip")
            return

        col = base_df["close"]

        if isinstance(col, pd.DataFrame):
            logger.warning(
                "[RUNTIME_MA] close was DataFrame → using first column"
            )
            base_df["close"] = col.iloc[:, 0]

        # ----------------------------------------------------
        # 型保証
        # ----------------------------------------------------
        if "symbol" not in base_df.columns or "datetime" not in base_df.columns:
            logger.warning("[RUNTIME_MA] required columns missing → skip")
            return

        base_df["symbol"] = base_df["symbol"].astype(str)

        base_df["datetime"] = pd.to_datetime(
            base_df["datetime"], errors="coerce"
        )

        base_df = base_df.dropna(subset=["symbol", "datetime"])

        # close 数値保証
        base_df["close"] = pd.to_numeric(
            base_df["close"], errors="coerce"
        )

        base_df = base_df.dropna(subset=["close"])

        if base_df.empty:
            logger.info("[RUNTIME_MA] skip: no valid close rows")
            return

        # ----------------------------------------------------
        # MA 計算
        # ----------------------------------------------------
        df_ma = build_yahoo_ma_1min(base_df)

        if df_ma is None or df_ma.empty:
            logger.info("[RUNTIME_MA] skip: MA result empty")
            return

        # ----------------------------------------------------
        # runtime キャッシュ更新（副作用ゼロ）
        # ----------------------------------------------------
        try:
            global_data.runtime_ma_1min = df_ma
        except Exception:
            setattr(global_data, "runtime_ma_1min", df_ma)

        logger.debug(
            "[RUNTIME_MA] 1min MA cache updated rows=%d",
            len(df_ma),
        )

    except Exception:
        # Scheduler を絶対止めない
        logger.exception("[RUNTIME_MA] MA cache update failed")


# ============================================================
# END
# ============================================================