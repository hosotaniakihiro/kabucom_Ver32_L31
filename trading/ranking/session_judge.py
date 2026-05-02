# ============================================================
# File   : ranking/session_judge.py
# Version: Ver1.0.0-FINAL-RANKING-SESSION-JUDGE
# ------------------------------------------------------------
# ✔ ranking セッションの品質判定
# ✔ STRONG / WEAK / REJECT の3値分類
# ✔ 価格×順位×滞在時間 を総合評価
# ✔ summary 未準備でも安全
# ✔ ATS / entry_gate / AI 学習 用途
# ============================================================

import math


def judge_session(row) -> str:
    """
    ランキングセッションの品質を判定する

    Parameters
    ----------
    row : pd.Series
        ranking/session_builder + enricher の1行

    Returns
    -------
    str
        "STRONG" / "WEAK" / "REJECT"
    """

    # --------------------------------------------------------
    # 必須チェック（最低限）
    # --------------------------------------------------------
    try:
        minutes = int(row.get("minutes", 0))
        rank_best = int(row.get("rank_best", 999))
        rank_ret = float(row.get("rank_ret", 0.0))
    except Exception:
        return "REJECT"

    # --------------------------------------------------------
    # STAGE0: 即REJECT（ゴミ排除）
    # --------------------------------------------------------
    # 1回だけの出現はノイズ
    if minutes < 2:
        return "REJECT"

    # TOP20にも入らないのは弱すぎる
    if rank_best > 20:
        return "REJECT"

    # --------------------------------------------------------
    # STAGE1: 基本評価（WEAK/STRONG 分岐）
    # --------------------------------------------------------
    # セッション中に下げ続けた場合は WEAK
    if rank_ret < 0:
        return "WEAK"

    # --------------------------------------------------------
    # STAGE2: STRONG 条件（価格×順位×乖離）
    # --------------------------------------------------------
    # 安全に取得（NaN耐性）
    d_vwap = row.get("d_vwap")
    d_ma75 = row.get("d_ma75")

    def _valid(x):
        return x is not None and not (
            isinstance(x, float) and math.isnan(x)
        )

    has_price_strength = False

    # VWAP または MA75 のどちらかを上回っていれば良し
    if _valid(d_vwap) and d_vwap >= 0:
        has_price_strength = True
    elif _valid(d_ma75) and d_ma75 >= 0:
        has_price_strength = True

    # STRONG 最終条件
    if (
        rank_best <= 10
        and rank_ret >= 0.003  # +0.3%以上
        and has_price_strength
    ):
        return "STRONG"

    # --------------------------------------------------------
    # デフォルト
    # --------------------------------------------------------
    return "WEAK"