def compute_fusion_score(prob_up, mtf_score, news_score, sns_score,
                         m1, m3, daily, ranking):
    """
    総合スコアAI（Fusion AI）
    0〜150 くらいのレンジ
    """

    score = 0

    # ==============================
    # ① MTF-AI（最重要）
    # ==============================
    score += mtf_score * 0.6

    # ==============================
    # ② ニュースAI（材料の強さ）
    # ==============================
    score += news_score * 0.2

    # ==============================
    # ③ SNS AI（話題性）
    # ==============================
    score += sns_score * 0.1


    # ==============================
    # ④ 1分足勢い（強化版）
    # ==============================
    if m1["m1_ma5_slope"] > 0:
        score += 5
    if m1["m1_rsi_delta"] > 2:
        score += 5
    if m1["m1_return_1"] > 0.002:
        score += 3


    # ==============================
    # ⑤ 3分足短期トレンド
    # ==============================
    if m3["m3_ma5_slope"] > 0:
        score += 5
    if m3["m3_rsi_delta"] > 1:
        score += 3
    if m3["m3_macd_delta"] > 0:
        score += 3


    # ==============================
    # ⑥ 日足トレンド
    # ==============================
    if daily["day_pos"] > 0.7:
        score += 8
    elif daily["day_pos"] < 0.3:
        score -= 8

    if daily["day_rsi"] < 30:
        score += 4
    if daily["day_rsi"] > 70:
        score -= 4


    # ==============================
    # ⑦ ランキング補正
    # ==============================
    if ranking["rank_gain_top20"] == 1:
        score += 8
    if ranking["rank_vol_top30"] == 1:
        score += 5
    if ranking["rank_gain_speed"] > 0:
        score += 2 * min(ranking["rank_gain_speed"], 5)


    return score
