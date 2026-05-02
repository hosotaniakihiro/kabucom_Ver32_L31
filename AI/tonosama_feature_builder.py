# ============================================================
# File: AI/tonosama_feature_builder.py
# ------------------------------------------------------------
# 殿様イナゴ（BUY）専用 特徴量ビルダー
#
# ✔ 1分足 summary 前提
# ✔ ranking / board / time を統合
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

def build_feature_row(
    *,
    summary_1m: pd.DataFrame,
    ranking,
    board,
    now: dt.datetime,
) -> dict:
    """
    殿様イナゴ BUY 用 特徴量を1行 dict で生成する

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
        LightGBM 用 feature row
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
    # price 系
    # ========================================================

    # 1分リターン（初動検知の核）
    prev_close = float(prev.close)
    cur_close  = float(cur.close)

    row["price_velocity"] = (
        (cur_close - prev_close) / prev_close
        if prev_close > 0 else 0.0
    )

    # ========================================================
    # volume 系
    # ========================================================

    # 出来高スピード（直近5本平均との差）
    recent_vol = summary_1m.volume.tail(5)
    vol_ma5 = float(recent_vol.mean()) if len(recent_vol) > 0 else 0.0

    row["volume_speed"] = (
        float(cur.volume) / vol_ma5
        if vol_ma5 > 0 else 0.0
    )

    # ========================================================
    # ranking 系
    # ========================================================

    rank_now  = getattr(ranking, "rank_now", None)
    rank_prev = getattr(ranking, "rank_prev", None)

    if rank_now is None or rank_prev is None:
        row["rank_jump"] = 0.0
        row["rank_strength"] = 0.0
    else:
        # ランク上昇量（大きいほど良い）
        row["rank_jump"] = float(rank_prev - rank_now)

        # ランク強度（上位ほど強い）
        row["rank_strength"] = 1.0 / max(float(rank_now), 1.0)

    # ========================================================
    # board（板）系
    # ========================================================

    buy_qty  = float(getattr(board, "buy_qty", 0.0))
    sell_qty = float(getattr(board, "sell_qty", 0.0))
    spread   = float(getattr(board, "spread", 0.0))

    # 買い板優勢度
    row["dominant_ratio"] = (
        buy_qty / sell_qty
        if sell_qty > 0 else buy_qty
    )

    # スプレッド比率（薄板・荒い板の除外）
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