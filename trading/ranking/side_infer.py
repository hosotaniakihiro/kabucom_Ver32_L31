# ============================================================
# trading/ranking/side_infer.py
# Ver1.0.0-MINIMAL-RANKING-SIDE
# ------------------------------------------------------------
# ✔ ranking_type → BUY / SELL / None を決定する唯一の責務
# ✔ DB / snapshot / AI / MA を一切見ない（純粋関数）
# ✔ 不明・危険なものは必ず None（SIDE_NONE）
# ✔ 将来ロジック拡張前提の独立モジュール
# ============================================================

from __future__ import annotations
from typing import Optional


def infer_side_from_rank_type(rank_type: Optional[str]) -> Optional[str]:
    """
    ranking_type から side を推定する（最小・安全版）

    Parameters
    ----------
    rank_type : str | None
        ランキング種別（例: "値上がり率", "売買代金急増"）

    Returns
    -------
    "BUY" | "SELL" | None
        - BUY  : 買い方向のランキング
        - SELL : 売り方向のランキング
        - None : 判定不能（＝SIDE_NONE → ENTRY不可）

    設計方針
    --------
    - ここでは「方向」だけを決める
    - 価格・スコア・dominant_ratio・AI は一切見ない
    - 危険な推測はしない（迷ったら None）
    """

    if not rank_type:
        return None

    # --------------------------------------------------------
    # BUY 系ランキング
    # --------------------------------------------------------
    BUY_TYPES = {
        "値上がり率",
        "売買高急増",
        "売買代金急増",
        "TICK回数",
    }

    # --------------------------------------------------------
    # SELL 系ランキング
    # --------------------------------------------------------
    SELL_TYPES = {
        "値下がり率",
    }

    if rank_type in BUY_TYPES:
        return "BUY"

    if rank_type in SELL_TYPES:
        return "SELL"

    # --------------------------------------------------------
    # 判定不能（安全側）
    # --------------------------------------------------------
    return None