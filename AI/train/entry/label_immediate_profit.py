# ============================================================
# AI/train/entry/label_immediate_profit.py
# ------------------------------------------------------------
# 即益ラベル生成（ENTRY後に含み益になったか）
# ------------------------------------------------------------
# ✔ 学習・検証専用（実運用では使用禁止）
# ✔ tick / 5秒足 / 1分足対応
# ✔ BUY / SELL 両対応
# ✔ NaN / 空データ完全耐性
# ============================================================

import logging
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
def is_immediate_profit(
    trades: pd.DataFrame,
    entry_price: float,
    side: str,
    *,
    threshold: float = 0.002,
    max_lookahead_sec: Optional[int] = None,
) -> bool:
    """
    ENTRY後の価格推移から
    即含み益だったかを判定する（ラベル生成用）

    Args:
        trades (pd.DataFrame):
            ENTRY後の tick / 5秒足 / 1分足
            必須カラム:
              - high / low
              - datetime（任意：時間制限用）
        entry_price (float):
            ENTRY 価格
        side (str):
            "BUY" or "SELL"
        threshold (float):
            含み益判定率（例: 0.002 = +0.2%）
        max_lookahead_sec (int | None):
            ENTRY後この秒数以内のみを評価対象にする
            None の場合は trades 全体を使用

    Returns:
        bool:
            即含み益になったか
    """

    # --------------------------------------------------------
    # 基本ガード
    # --------------------------------------------------------
    if trades is None or trades.empty:
        return False

    if entry_price is None or entry_price <= 0:
        return False

    if side not in ("BUY", "SELL"):
        return False

    # --------------------------------------------------------
    # 時間制限（任意）
    # --------------------------------------------------------
    df = trades

    if max_lookahead_sec is not None and "datetime" in df.columns:
        try:
            df = df.copy()
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            df = df.dropna(subset=["datetime"])

            start_time = df["datetime"].iloc[0]
            end_time = start_time + pd.Timedelta(seconds=max_lookahead_sec)
            df = df[df["datetime"] <= end_time]
        except Exception:
            logger.exception("lookahead filtering failed")
            # フィルタ失敗時は元データを使う

    if df.empty:
        return False

    # --------------------------------------------------------
    # 即益判定
    # --------------------------------------------------------
    try:
        if side == "BUY":
            if "high" not in df.columns:
                return False
            max_price = df["high"].max()
            if pd.isna(max_price):
                return False
            return (max_price - entry_price) / entry_price >= threshold

        else:  # SELL
            if "low" not in df.columns:
                return False
            min_price = df["low"].min()
            if pd.isna(min_price):
                return False
            return (entry_price - min_price) / entry_price >= threshold

    except Exception:
        logger.exception("immediate profit label failed")
        return False


# ============================================================
# 補助：確率ではなく「最大含み益率」を返す版（分析用）
# ============================================================
def calc_max_immediate_return(
    trades: pd.DataFrame,
    entry_price: float,
    side: str,
) -> float:
    """
    ENTRY後の最大含み益率を返す（分析・可視化用）

    Returns:
        float:
            含み益率（負の場合もあり得る）
    """

    if trades is None or trades.empty:
        return 0.0

    if entry_price is None or entry_price <= 0:
        return 0.0

    if side not in ("BUY", "SELL"):
        return 0.0

    try:
        if side == "BUY":
            if "high" not in trades.columns:
                return 0.0
            max_price = trades["high"].max()
            if pd.isna(max_price):
                return 0.0
            return (max_price - entry_price) / entry_price

        else:  # SELL
            if "low" not in trades.columns:
                return 0.0
            min_price = trades["low"].min()
            if pd.isna(min_price):
                return 0.0
            return (entry_price - min_price) / entry_price

    except Exception:
        logger.exception("calc_max_immediate_return failed")
        return 0.0
