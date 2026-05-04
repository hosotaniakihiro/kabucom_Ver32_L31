def build_scalp_score(symbol, bar_5s, summary_1m, orderflow):
    """
    スキャルピング用 BUY / SELL スコアと理由を返す（最適化済み）
    return: (buy_scalp_score, sell_scalp_score, buy_reasons, sell_reasons)
    """

    buy = 0
    sell = 0
    buy_reasons = []
    sell_reasons = []

    price = bar_5s["close"]
    open_ = bar_5s["open"]
    vwap = summary_1m.get("vwap", None)
    avg_1min_vol = summary_1m.get("volume", 0)

    # ======================================================
    # ① 5秒足勢い（＋連続判定）
    # ======================================================
    # 単発陽線/陰線は加点 → OK
    if price > open_:
        buy += 2
        buy_reasons.append("5秒陽線")
    elif price < open_:
        sell += 2
        sell_reasons.append("5秒陰線")

    # 5秒足の連続勢い（five_sec_up_count / down_count）
    up_count = global_data.five_sec_up_count.get(symbol, 0)
    if up_count >= 2:
        buy += 1
        buy_reasons.append(f"5秒陽線連続({up_count})")

    # ======================================================
    # ② VWAP乖離（拡大検出 with threshold）
    # ======================================================
    prev_gap = global_data.prev_vwap_gap.get(symbol)
    if vwap:
        gap = (price - vwap) / vwap

        if gap > 0:
            buy += 1
            buy_reasons.append("VWAP上")

            # 拡大が 0.05% 以上なら本物
            if prev_gap is not None and (gap - prev_gap) >= 0.0005:
                buy += 1
                buy_reasons.append("VWAP乖離拡大(強)")

        elif gap < 0:
            sell += 1
            sell_reasons.append("VWAP下")

            if prev_gap is not None and (prev_gap - gap) >= 0.0005:
                sell += 1
                sell_reasons.append("VWAP乖離拡大(強)")

    # ======================================================
    # ③ 出来高急増（5秒 vs 1分）
    # ======================================================
    if avg_1min_vol > 0:
        if bar_5s["volume"] >= avg_1min_vol * 2:
            buy += 1
            sell += 1
            buy_reasons.append("出来高急増")
            sell_reasons.append("出来高急増")

    # ======================================================
    # ④ 成行連続（出来高により threshold 可変）
    # ======================================================
    def _calc_of_threshold(vol):
        if vol >= 50000:     # 大型・出来高多い銘柄
            return 3
        elif vol >= 20000:   # 中型
            return 4
        else:                # 小型・閑散銘柄
            return 5

    threshold = _calc_of_threshold(avg_1min_vol)

    buy_cnt = orderflow.get("buy_count_3s", 0)
    sell_cnt = orderflow.get("sell_count_3s", 0)

    if buy_cnt >= threshold:
        buy += 2
        buy_reasons.append(f"成行買い連続({buy_cnt})")

    if sell_cnt >= threshold:
        sell += 2
        sell_reasons.append(f"成行売り連続({sell_cnt})")

    # ======================================================
    # ⑤ 板厚み（板優位、1.6倍に最適化）
    # ======================================================
    bid = orderflow.get("best_bid_size", 1)
    ask = orderflow.get("best_ask_size", 999999)

    # BUY優勢
    if bid >= ask * 1.6:
        buy += 1
        buy_reasons.append("板買い優勢")

    # SELL優勢
    if ask >= bid * 1.6:
        sell += 1
        sell_reasons.append("板売り優勢")

    return buy, sell, buy_reasons, sell_reasons
