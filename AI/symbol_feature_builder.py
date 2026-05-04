# ============================================================
# symbol_feature_builder.py
# ------------------------------------------------------------
# ・銘柄ごとの「性格特徴量」を作る
# ・日足 / 5分足 / 1分足 すべて対応
# ・AI 用の「銘柄固定特徴量」（時間非依存）
# ・欠損 / 行不足 / dtype 事故を完全防止
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# メイン関数
# ============================================================
def build_symbol_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    銘柄ごとの性格特徴量を生成する

    Parameters
    ----------
    df : pd.DataFrame
        必須列:
            - symbol
            - datetime
            - open
            - high
            - low
            - close
            - volume

        推奨:
            日足（最低 30 行以上 / symbol）

    Returns
    -------
    pd.DataFrame
        columns:
            - symbol
            - price_level
            - avg_volume_20
            - atr_20
            - atr_ratio
            - volatility_std_20
            - ret_std_20
            - ret_mean_20
            - max_return_20
            - min_return_20
    """

    required_cols = {
        "symbol",
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }

    missing = required_cols - set(df.columns)
    if missing:
        logger.error(f"[symbol_feature_builder] missing columns: {missing}")
        return pd.DataFrame()

    if df.empty:
        logger.warning("[symbol_feature_builder] input df is empty")
        return pd.DataFrame()

    # dtype 正規化（事故防止）
    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    feats: list[dict] = []

    # ========================================================
    # 銘柄ごと処理
    # ========================================================
    for symbol, g in df.groupby("symbol", sort=False):

        g = g.sort_values("datetime").reset_index(drop=True)

        # 行不足ガード
        if len(g) < 25:
            logger.debug(
                f"[symbol_feature_builder] skip symbol={symbol} rows={len(g)}"
            )
            continue

        # 最新価格
        last_close = g["close"].iloc[-1]
        if not np.isfinite(last_close) or last_close <= 0:
            continue

        # ----------------------------------------------------
        # 価格レベル（log価格）
        # ----------------------------------------------------
        price_level = float(np.log(last_close))

        # ----------------------------------------------------
        # 出来高特徴
        # ----------------------------------------------------
        avg_volume_20 = float(
            g["volume"].tail(20).mean(skipna=True)
        )

        # ----------------------------------------------------
        # ATR（20）
        # ----------------------------------------------------
        prev_close = g["close"].shift()

        tr = np.maximum(
            g["high"] - g["low"],
            np.maximum(
                (g["high"] - prev_close).abs(),
                (g["low"] - prev_close).abs(),
            ),
        )

        atr_20 = float(tr.rolling(20).mean().iloc[-1])
        atr_ratio = float(atr_20 / last_close) if last_close > 0 else np.nan

        # ----------------------------------------------------
        # リターン系
        # ----------------------------------------------------
        returns = g["close"].pct_change()

        ret_std_20 = float(returns.tail(20).std())
        ret_mean_20 = float(returns.tail(20).mean())

        max_return_20 = float(returns.tail(20).max())
        min_return_20 = float(returns.tail(20).min())

        # ----------------------------------------------------
        # ボラティリティ（終値標準偏差）
        # ----------------------------------------------------
        volatility_std_20 = float(
            returns.rolling(20).std().iloc[-1]
        )

        feats.append(
            {
                "symbol": symbol,

                # 価格水準
                "price_level": price_level,

                # 出来高
                "avg_volume_20": avg_volume_20,

                # ATR 系
                "atr_20": atr_20,
                "atr_ratio": atr_ratio,

                # ボラティリティ
                "volatility_std_20": volatility_std_20,

                # リターン統計
                "ret_std_20": ret_std_20,
                "ret_mean_20": ret_mean_20,
                "max_return_20": max_return_20,
                "min_return_20": min_return_20,
            }
        )

    result = pd.DataFrame(feats)

    if result.empty:
        logger.warning("[symbol_feature_builder] no symbol features generated")

    return result
