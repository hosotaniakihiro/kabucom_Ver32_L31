# ============================================================
# pj/trading/market/market_features.py
# 市場全体特徴量生成（DI対応・最終版）
# ============================================================

import numpy as np


def build_market_features(global_data):
    """
    市場全体の状態を数値化（DI方式）

    Args:
        global_data: GlobalData

    Returns:
        dict | None
    """

    # --------------------------------------------------------
    # summary（1分足）
    # --------------------------------------------------------
    summary_cache = getattr(global_data, "summary_cache", None)
    if not isinstance(summary_cache, dict):
        return None

    summary = summary_cache.get("1min")
    if summary is None or summary.empty:
        return None

    # --------------------------------------------------------
    # ranking
    # --------------------------------------------------------
    ranking = getattr(global_data, "latest_ranking", {})
    if not isinstance(ranking, dict):
        ranking = {}

    # --------------------------------------------------------
    # 上昇 / 下落比率
    # --------------------------------------------------------
    if not {"open_price", "close_price"}.issubset(summary.columns):
        return None

    up = summary[summary["close_price"] > summary["open_price"]]
    down = summary[summary["close_price"] < summary["open_price"]]

    total = len(summary)
    up_ratio = len(up) / total if total else 0.0
    down_ratio = len(down) / total if total else 0.0

    # --------------------------------------------------------
    # fast_ret 分布
    # --------------------------------------------------------
    fast_ret_mean = 0.0
    if "fast_ret" in summary.columns:
        fast_rets = summary["fast_ret"].astype(float)
        if not fast_rets.empty:
            fast_ret_mean = float(np.nanmean(fast_rets))

    # --------------------------------------------------------
    # ランキング出来高合計
    # --------------------------------------------------------
    ranking_volume_sum = 0.0
    for df in ranking.values():
        if df is not None and hasattr(df, "columns") and "volume_speed" in df.columns:
            ranking_volume_sum += float(df["volume_speed"].fillna(0).sum())

    return {
        "up_ratio": up_ratio,
        "down_ratio": down_ratio,
        "fast_ret_mean": fast_ret_mean,
        "ranking_volume_sum": ranking_volume_sum,
    }
