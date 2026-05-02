# ============================================================
# File: AI/sell_tonosama_feature_builder.py
# ------------------------------------------------------------
# 殿様イナゴ（SELL）専用 特徴量ビルダー
#
# ✔ 1分足 summary 前提
# ✔ ranking / board / time を統合
# ✔ 下げ方向（踏み上げ前）に最適化
# ✔ NaN / ゼロ割 完全耐性
# ✔ 学習 / 推論 両対応
# ✔ 超短期（60秒）最適化
# ============================================================

from __future__ import annotations

import datetime as dt
import pandas as pd


# ============================================================
# メインAPI
# ============================================================

def build_sell_feature_row(
    *,
    summary_1m: pd.DataFrame,
    ranking,
    board,
    now: dt.datetime,
) -> dict:
    """
    殿様イナゴ SELL 用 特徴量を1行 dict で生成する

    Parameters
    ----------
    summary_1m : DataFrame
        1分足 summary（最低2本必要）
        columns: close, volume
    ranking :
        ranking オブジェクト
        必要属性: rank_now, rank_prev
    board :
        板情報オブジェクト
        必要属性: buy_qty, sell_qty, spread
    now : datetime
        現在時刻

    Returns
    -------
    dict
        LightGBM 用 feature row（SELL）
    """

    # --------------------------------------------------------
    # safety
    # --------------------------------------------------------
    if summary_1m is None or len(summary_1m) < 2:
        return {}

    prev = summary_1m.iloc[-2]
    cur  = summary_1m.iloc[-1]

    row: dict[str, float] = {}

    # ========================================================
    # price 系（下げ方向）
    # ========================================================

    prev_close = float(prev.close)
    cur_close  = float(cur.close)

    # 1分リターン（下落方向を見る）
    row["price_velocity"] = (
        (cur_close - prev_close) / prev_close
        if prev_close > 0 else 0.0
    )

    # ========================================================
    # volume 系（ピークアウト検知）
    # ========================================================

    recent_vol = summary_1m.volume.tail(5)
    vol_peak = float(recent_vol.max()) if len(recent_vol) > 0 else 0.0

    # 出来高低下率（ピーク比）
    row["volume_drop"] = (
        float(cur.volume) / vol_peak
        if vol_peak > 0 else 0.0
    )

    # ========================================================
    # ranking 系（順位悪化）
    # ========================================================

    rank_now  = getattr(ranking, "rank_now", None)
    rank_prev = getattr(ranking, "rank_prev", None)

    if rank_now is None or rank_prev is None:
        row["rank_fall"] = 0.0
    else:
        # ランク下落量（大きいほど弱い）
        row["rank_fall"] = float(rank_now - rank_prev)

    # ========================================================
    # board（板）系（売り優勢）
    # ========================================================

    buy_qty  = float(getattr(board, "buy_qty", 0.0))
    sell_qty = float(getattr(board, "sell_qty", 0.0))
    spread   = float(getattr(board, "spread", 0.0))

    # 売り板圧力
    row["sell_pressure"] = (
        sell_qty / buy_qty
        if buy_qty > 0 else sell_qty
    )

    # スプレッド比率
    row["spread_ratio"] = (
        spread / cur_close
        if cur_close > 0 else 0.0
    )

    # ========================================================
    # time 系
    # ========================================================

    market_open = now.replace(
        hour=9, minute=0, second=0, microsecond=0
    )

    row["minute_from_open"] = max(
        int((now - market_open).total_seconds() // 60),
        0
    )

    # ========================================================
    # final safety（NaN 排除）
    # ========================================================

    for k, v in row.items():
        if v != v or v is None:   # NaN check
            row[k] = 0.0

    return row