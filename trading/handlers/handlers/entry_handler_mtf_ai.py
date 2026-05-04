import logging
import pandas as pd
from AI.predict_mtf import predict_mtf
from AI.feature_importance import get_feature_importance_top50

logger = logging.getLogger(__name__)

AI_THRESHOLD = 0.65        # 5分後上昇確率フィルタ
MTF_SCORE_THRESHOLD = 70   # 日足 × 短期補正のスコア

def compute_mtf_score(prob_up, m1, m3, daily, rank):
    """
    MTF-AI を使った総合スコア
    prob_up : 5分後上昇確率
    m1, m3, daily, rank : 1分 / 3分 / 日足 / ランキング特徴量
    """

    score = prob_up * 100      # AI核スコア（最大100）

    # ==== 日足補正 ====
    if daily["day_pos"] > 0.7:
        score += 10
    elif daily["day_pos"] < 0.3:
        score -= 10

    if daily["day_rsi"] < 30:
        score += 5
    elif daily["day_rsi"] > 70:
        score -= 5

    # ==== 3分足補正 ====
    if m3["m3_ma5"] > 0 and m3["m3_rsi"] > 50:
        score += 5
    if m3["m3_macd"] > 0:
        score += 5

    # ==== 1分足補正 ====
    if m1["m1_rsi"] > 60:
        score += 5
    if m1["m1_ma5"] > 0:
        score += 3

    # ==== ランキング補正 ====
    if rank["rank_gain_top20"] == 1:
        score += 10
    if rank["rank_vol_top30"] == 1:
        score += 5
    if rank["rank_gain_speed"] > 0:
        score += 3 * min(rank["rank_gain_speed"], 3)

    return score


# ====================================================
# ★ ENTRY MAIN
# ====================================================
def ai_entry_decision(symbol, latest_1m, latest_3m, latest_5m, daily, ranking):
    """
    symbol: 銘柄コード
    latest_1m, latest_3m, latest_5m: 各TFの最新dict
    daily: 日足 dict
    ranking: ランキング dict
    """

    # ① 5分後AI確率
    prob_up = predict_mtf(latest_1m, latest_3m, latest_5m, daily)

    if prob_up < AI_THRESHOLD:
        logger.info(f"[AI] {symbol} : 上昇確率 {prob_up:.3f} < {AI_THRESHOLD} → ENTRY拒否")
        return False, prob_up, None

    # ② MTFスコア算出
    score = compute_mtf_score(prob_up, latest_1m, latest_3m, daily, ranking)

    if score < MTF_SCORE_THRESHOLD:
        logger.info(f"[AI] {symbol} : MTF SCORE {score:.1f} < {MTF_SCORE_THRESHOLD} → ENTRY拒否")
        return False, prob_up, score

    # ③ 特徴量重要度ログ
    top50 = get_feature_importance_top50()
    logger.info("========= AI Feature Importance TOP50 =========")
    for name, imp in top50:
        logger.info(f"{name:25s} : {imp:.5f}")
    logger.info("================================================")

    logger.info(f"[AI] {symbol} : ENTRY許可 ★ (確率={prob_up:.3f}, SCORE={score:.1f})")

    return True, prob_up, score
