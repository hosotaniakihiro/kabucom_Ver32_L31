# ============================================================
# label_builder_intraday.py
# ------------------------------------------------------------
# ・時間足ごとに未来 horizon と閾値を切り替える
# ・BUY方向の2値分類用
# ============================================================

def build_intraday_label(df, interval: str):
    """
    interval:
      "60"  -> 1時間足
      "5"   -> 5分足
      "3"   -> 3分足
      "2"   -> 2分足
      "1"   -> 1分足
      "10s" -> 10秒足
      "5s"  -> 5秒足
      "1s"  -> 1秒足
    """

    # horizon = 何本先を見るか
    # th = 上昇とみなす最小リターン
    if interval == "60":
        horizon, th = 1, 0.003

    elif interval in ("5", "3", "2", "1"):
        horizon, th = 1, 0.002

    else:  # 秒足
        horizon, th = 3, 0.001

    future_ret = df["close"].shift(-horizon) / df["close"] - 1
    return (future_ret > th).astype(int)
