# ============================================================
# trading/summary/yahoo/ma_builder.py
# ------------------------------------------------------------
# ✔ Yahoo Finance 補完1分足から MA を生成
# ✔ close のみ使用（OHLC 不要）
# ✔ PUSH / ENTRY には一切関与しない
# ✔ 表示・監視・MA維持専用
# ✔ duplicate列完全防御
# ✔ close DataFrame化完全防御
# ✔ scheduler停止防止
# ============================================================

import logging
import pandas as pd
from typing import Tuple

logger = logging.getLogger(__name__)


def build_yahoo_ma_1min(
    df_1min: pd.DataFrame,
    windows: Tuple[int, ...] = (5, 25, 75),
) -> pd.DataFrame:
    """
    Yahoo 補完 1分足 DataFrame から MA を生成する

    Parameters
    ----------
    df_1min : pd.DataFrame
        必須カラム:
          - symbol
          - datetime
          - close

    windows : tuple
        MA期間（例: (5, 25, 75)）

    Returns
    -------
    pd.DataFrame
        symbol / datetime / close / ma5 / ma25 / ma75
    """

    try:
        # ----------------------------------------------------
        # 入力チェック
        # ----------------------------------------------------
        if df_1min is None or df_1min.empty:
            logger.debug("[yahoo_ma_builder] empty input df")
            return pd.DataFrame()

        if not isinstance(df_1min, pd.DataFrame):
            logger.warning("[yahoo_ma_builder] input is not DataFrame")
            return pd.DataFrame()

        # defensive copy
        df = df_1min.copy()

        # ----------------------------------------------------
        # 🔥 duplicate列完全防御
        # ----------------------------------------------------
        dup_cols = list(df.columns[df.columns.duplicated()])
        if dup_cols:
            logger.warning(
                "[yahoo_ma_builder] duplicate columns removed: %s",
                dup_cols,
            )
            df = df.loc[:, ~df.columns.duplicated()]

        # ----------------------------------------------------
        # 必須カラム確認
        # ----------------------------------------------------
        required_cols = {"symbol", "datetime", "close"}
        if not required_cols.issubset(df.columns):
            logger.warning(
                "[yahoo_ma_builder] missing columns: %s",
                required_cols - set(df.columns),
            )
            return pd.DataFrame()

        # ----------------------------------------------------
        # 🔥 closeがDataFrame化していた場合の最終防御
        # ----------------------------------------------------
        try:
            col = df["close"]
            if isinstance(col, pd.DataFrame):
                logger.warning(
                    "[yahoo_ma_builder] close was DataFrame → using first column"
                )
                df["close"] = col.iloc[:, 0]
        except Exception:
            logger.warning(
                "[yahoo_ma_builder] close extraction failed"
            )
            return pd.DataFrame()

        # ----------------------------------------------------
        # 型正規化
        # ----------------------------------------------------
        df["symbol"] = df["symbol"].astype(str)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")

        # 無効行除去
        df = df.dropna(subset=["symbol", "datetime", "close"])
        if df.empty:
            return pd.DataFrame()

        # ----------------------------------------------------
        # ソート（MA計算の前提）
        # ----------------------------------------------------
        df = df.sort_values(["symbol", "datetime"])

        # ----------------------------------------------------
        # MA計算
        # ----------------------------------------------------
        for w in windows:
            col = f"ma{w}"
            try:
                df[col] = (
                    df.groupby("symbol")["close"]
                    .rolling(window=w, min_periods=w)
                    .mean()
                    .reset_index(level=0, drop=True)
                )
            except Exception:
                logger.exception(
                    "[yahoo_ma_builder] MA calculation failed (window=%s)",
                    w,
                )
                return pd.DataFrame()

        # ----------------------------------------------------
        # 必要最小限のみ返却
        # ----------------------------------------------------
        ma_cols = ["symbol", "datetime", "close"] + [
            f"ma{w}" for w in windows
        ]

        df = df[ma_cols]

        return df

    except Exception:
        # Schedulerを絶対止めない
        logger.exception("[yahoo_ma_builder] fatal error")
        return pd.DataFrame()