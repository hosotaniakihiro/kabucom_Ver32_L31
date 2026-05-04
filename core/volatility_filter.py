# ============================================================
# volatility_filter.py（ボラ＆出来高フィルター）
# ============================================================

THRESH_CHANGE = 0.003     # 0.3%
THRESH_VOLUME = 10000     # 5分足として十分な出来高
MAX_INACTIVE = 5          # 5回連続で除外


def update_volatility_filter(df_5m, summary_row, global_data):
    """
    df_5m       : 5分サマリーDataFrame（全銘柄）
    summary_row : 今回更新された1銘柄の行（最新5分足）
    global_data : global_state
    """
    symbol = summary_row["symbol"]
    op = summary_row.get("opening_price")
    close = summary_row.get("close_price")
    vol = summary_row.get("volume", 0)

    # 始値・終値不足 → 判定不可
    if op is None or close is None:
        return

    # 当日ボラ（0.3%）
    change_rate = abs(close - op) / op

    # デフォルト値
    cnt = global_data.symbol_inactive_count.get(symbol, 0)

    # --------------------------------------------------------
    # ボラ不足 or 出来高不足 → カウントアップ
    # --------------------------------------------------------
    if change_rate < THRESH_CHANGE or vol < THRESH_VOLUME:
        cnt += 1
    else:
        cnt = 0  # リセット（今日は動いている）

    global_data.symbol_inactive_count[symbol] = cnt

    # --------------------------------------------------------
    # 5回連続でボラ不足 → 監視解除
    # --------------------------------------------------------
    if cnt >= MAX_INACTIVE:
        try:
            global_data.symbols.remove(symbol)
            print(f"⛔ 監視解除（5回連続ボラ不足）: {symbol}")
        except:
            pass
