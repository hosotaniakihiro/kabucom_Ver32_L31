def entry_handler_fusion(symbol, latest_1m, latest_3m, latest_5m,
                         daily, ranking, news_score, sns_score):

    # 5分後AI確率
    prob = predict_mtf(latest_1m, latest_3m, latest_5m, daily)

    # MTFスコア
    mtf_score = compute_mtf_score(prob, latest_1m, latest_3m, daily, ranking)

    # Fusionスコア
    fusion = compute_fusion_score(
        prob, mtf_score, news_score, sns_score,
        latest_1m, latest_3m, daily, ranking
    )

    if fusion >= 80:
        return True, prob, mtf_score, fusion
    else:
        return False, prob, mtf_score, fusion
