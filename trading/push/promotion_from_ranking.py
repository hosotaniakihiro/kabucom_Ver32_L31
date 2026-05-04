# ============================================================
# trading/push/promotion_from_ranking.py
# ------------------------------------------------------------
# ✔ Ranking 起点で PUSH 監視に昇格させる候補を抽出
# ✔ ENTRY は絶対に行わない（監視用途のみ）
# ✔ PUSH 50枠を「強い銘柄優先」に知能化
# ✔ Unified MA + 出来高速度を使用
# ============================================================

import logging
import pandas as pd
from typing import List

logger = logging.getLogger(__name__)


def select_push_candidates_from_ranking(
    df_unified: pd.DataFrame,
    *,
    max_candidates: int = 10,
    min_volume_speed: float = 300.0,
    min_push_ratio: float = 0.0,
) -> List[str]:
    """
    Ranking-only 銘柄のうち、PUSH 監視へ昇格させる候補を選定する。

    Parameters
    ----------
    df_unified : DataFrame
        summary + unified MA がマージ済みの DataFrame
        必須カラム:
          - symbol
          - source
          - volume_speed
          - ma25
          - ma75
          - ma25_push_ratio

    max_candidates : int
        最大昇格数（PUSHローテーション補充枠）

    min_volume_speed : float
        出来高速度の最低条件

    min_push_ratio : float
        既に PUSH が混ざっている銘柄を除外する閾値
        通常は 0.0（完全 Ranking-only）

    Returns
    -------
    List[str]
        PUSH 登録候補の symbol リスト
    """

    if df_unified is None or df_unified.empty:
        return []

    REQUIRED = {
        "symbol",
        "source",
        "volume_speed",
        "ma25",
        "ma75",
        "ma25_push_ratio",
    }

    if not REQUIRED.issubset(df_unified.columns):
        logger.debug(
            "[promotion_from_ranking] missing columns: %s",
            REQUIRED - set(df_unified.columns),
        )
        return []

    df = df_unified.copy()

    # --------------------------------------------------------
    # 条件フィルタ
    # --------------------------------------------------------
    cond = (
        (df["source"] == "RANKING") &
        (df["volume_speed"] >= min_volume_speed) &
        (df["ma25_push_ratio"] <= min_push_ratio) &
        (df["ma25"] > df["ma75"])
    )

    df = df[cond]

    if df.empty:
        return []

    # --------------------------------------------------------
    # 優先度付け
    # ・出来高速度が最優先
    # ・MA25 と MA75 の乖離が大きいほど良い
    # --------------------------------------------------------
    df = df.assign(
        ma_gap=(df["ma25"] - df["ma75"]).abs()
    )

    df = df.sort_values(
        ["volume_speed", "ma_gap"],
        ascending=[False, False],
    )

    symbols = (
        df["symbol"]
        .astype(str)
        .drop_duplicates()
        .head(max_candidates)
        .tolist()
    )

    logger.info(
        "[PUSH PROMOTION] candidates=%s",
        symbols,
    )

    return symbols
