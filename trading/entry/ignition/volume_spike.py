# ============================================================
# volume_spike.py（Ver6.0）
# ------------------------------------------------------------
# ・ランキングDBから 1分出来高差分 / 売買代金差分 を取得
# ・3分平均出来高を計算
# ・スパイク判定用の特徴量を返す
# ============================================================

import pandas as pd
#from global_state import global_data


# ------------------------------------------------------------
# 1分差分（trading_volume / turnover）
# ------------------------------------------------------------
def _get_1min_volume(df_rank, idx):
    """ランキングDFから1分出来高・売買代金差分を取得"""
    if idx == 0:
        return 0, 0

    cur = df_rank.iloc[idx]
    prev = df_rank.iloc[idx - 1]

    # 銘柄違いの場合は差分が取れない
    if cur["symbol"] != prev["symbol"]:
        return 0, 0

    vol = max(cur["trading_volume"] - prev["trading_volume"], 0)
    val = max(cur["turnover"] - prev["turnover"], 0)
    return vol, val


# ------------------------------------------------------------
# 3分平均出来高
# ------------------------------------------------------------
def _get_3min_avg_volume(df_rank, idx):
    """直近3件から平均出来高を取る（0除算防止）"""
    begin = max(0, idx - 3)
    rows = df_rank.iloc[begin:idx+1]

    if len(rows) < 2:
        return 1  # fallback（0除算防止）

    v_min = rows["trading_volume"].iloc[0]
    v_max = rows["trading_volume"].iloc[-1]

    real_3m = max(v_max - v_min, 0)
    avg3m = real_3m / (len(rows) - 1)

    return max(avg3m, 1)


# ------------------------------------------------------------
# 公開API：ENTRY判定で呼ぶ関数
# ------------------------------------------------------------
def calc_volume_features(symbol):
    """
    ENTRY判定用の出来高特徴量セットを返す。
    return:
        vol_1m   : 1分出来高
        val_1m   : 1分売買代金
        avg3m    : 3分平均出来高
        ridx     : ランキングDFでの当該行 index
    """

    df_rank = global_data.get_latest_ranking_df()
    if df_rank is None or df_rank.empty:
        return 0, 0, 1, None

    ridx_list = df_rank.index[df_rank["symbol"] == symbol].tolist()
    if not ridx_list:
        return 0, 0, 1, None

    ridx = ridx_list[-1]

    # 1分差分
    vol_1m, val_1m = _get_1min_volume(df_rank, ridx)

    # 3分平均
    avg3m = _get_3min_avg_volume(df_rank, ridx)

    return vol_1m, val_1m, avg3m, ridx
