# ============================================================
# File: AI/sell_tonosama_label_builder.py
# ------------------------------------------------------------
# 殿様イナゴ（SELL）専用 ラベル生成
#
# ✔ 目的：次の60秒以内に -0.4% 以上下落したか
# ✔ 回帰ではなく分類（0 / 1）
# ✔ NaN / 空配列 完全耐性
# ✔ 学習・検証・再学習 共通
# ✔ BUY版と完全対称（方向のみ逆）
# ============================================================

from __future__ import annotations


# ============================================================
# メインAPI
# ============================================================

def build_sell_label(
    *,
    entry_price: float,
    future_prices: list[float],
    threshold: float = -0.004,
) -> int:
    """
    殿様イナゴ SELL 用ラベルを生成する

    Parameters
    ----------
    entry_price : float
        ENTRY 時の約定価格（または直近 close）
    future_prices : list[float]
        ENTRY 以降 60 秒以内の価格配列
        （例：1秒足 / ティック / 疑似1秒）
    threshold : float
        下落判定閾値（default = -0.4%）

    Returns
    -------
    int
        1 : 60秒以内に threshold 以下に到達
        0 : 到達せず
    """

    # --------------------------------------------------------
    # safety
    # --------------------------------------------------------
    if entry_price is None or entry_price <= 0:
        return 0

    if not future_prices:
        return 0

    # --------------------------------------------------------
    # 最小リターン計算（下落を見る）
    # --------------------------------------------------------
    min_ret = 0.0

    for p in future_prices:
        if p is None or p <= 0:
            continue

        ret = (p - entry_price) / entry_price
        if ret < min_ret:
            min_ret = ret

    # --------------------------------------------------------
    # ラベル判定
    # --------------------------------------------------------
    return 1 if min_ret <= threshold else 0