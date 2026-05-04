import logging
from AI.predict_mtf import predict_mtf
from AI.ai_fusion import compute_fusion_score

logger = logging.getLogger(__name__)

EXIT_THRESHOLD = 75  # EXITスコア

def compute_exit_score(prob_up, mtf_score,
                       news_score, sns_score,
                       m1, m3, daily, ranking):

    prob_down = 1 - prob_up
    score = 0

    # ① 下落確率
    score += prob_down * 100 * 0.5

    # ② MTF方向が弱い場合（mtfスコア低下）
    if mtf_score < 50:
        score += 10

    # ③ ニュースAIで悪材料
    if news_score < 40:
        score += 15

    # ④ SNSでネガティブなら加点
    if sns_score < 40:
        score += 10

    # ⑤ 1分足失速
    if m1["m1_ma5_slope"] < 0:
        score += 10
    if m1["m1_rsi_delta"] < -3:
        score += 8

    # ⑥ 3分足失速
    if m3["m3_macd_delta"] < 0:
        score += 10

    # ⑦ 日足が弱い位置
    if daily["day_pos"] < 0.3:
        score += 8

    # ⑧ ランキング低下（前日→今日でランクダウン）
    if ranking["rank_gain_speed"] < 0:
        score += 5

    return score


def exit_decision_ai(symbol, latest_1m, latest_3m, latest_5m,
                      daily, ranking, news_score, sns_score):

    # 5分後上昇確率
    prob_up = predict_mtf(latest_1m, latest_3m, latest_5m, daily)

    # MTFスコア（entry_handlerで使用したもの）
    f_prob = prob_up
    mtf_score = (
        f_prob * 100
        + (daily["day_pos"] - 0.5) * 20
        + (latest_3m["m3_ma5"] > 0) * 10
    )

    # EXITスコア
    exit_score = compute_exit_score(
        prob_up, mtf_score, news_score, sns_score,
        latest_1m, latest_3m, daily, ranking
    )

    logger.info(f"[EXIT AI] {symbol}: EXIT SCORE={exit_score:.1f}")

    if exit_score >= EXIT_THRESHOLD:
        logger.info(f"[EXIT AI] {symbol}: EXIT発火")
        return True, exit_score
    else:
        return False, exit_score
