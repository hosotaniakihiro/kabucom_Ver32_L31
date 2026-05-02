# ============================================================
# AI/exit_decision_ai.py
# Ver1.2-FINAL-AI-PRIMARY-EXIT-MTF-COMPAT-STABLE
# ------------------------------------------------------------
# ✔ AI主導 EXIT 判定ロジック
# ✔ KeyError 完全防止（.get使用）
# ✔ predict_mtf 新旧仕様完全吸収
# ✔ dict返却型にも対応（{"prob_up":...}）
# ✔ 例外完全安全フォールバック
# ✔ ログ出力保持
# ✔ 将来の動的閾値拡張に対応
# ============================================================

from __future__ import annotations

import logging
from typing import Dict, Tuple, Any

from AI.predict_mtf import predict_mtf

logger = logging.getLogger(__name__)

# ============================================================
# 基本設定
# ============================================================

EXIT_THRESHOLD = 75  # EXITスコア閾値


# ============================================================
# MTF 呼び出し完全互換ラッパ
# ============================================================

def _safe_predict_mtf(
    symbol: str,
    latest_1m: Dict,
    latest_3m: Dict,
    latest_5m: Dict,
    daily: Dict,
) -> float:
    """
    predict_mtf のシグネチャ差異を完全吸収する安全ラッパ

    対応パターン:
      ① predict_mtf(symbol=..., interval=1)
      ② predict_mtf()  ← 旧引数なし型
      ③ predict_mtf(m1, m3, m5, daily) ← 旧引数型
      ④ dict返却型 / float返却型 両対応
    """

    # ========================================================
    # ① 最新仕様（keyword型）
    # ========================================================
    try:
        result = predict_mtf(symbol=symbol, interval=1)

        if isinstance(result, dict):
            return float(result.get("prob_up", 0.5))
        return float(result)

    except TypeError:
        pass
    except Exception:
        logger.exception("[EXIT_AI] predict_mtf new-style failed")

    # ========================================================
    # ② 引数なし型
    # ========================================================
    try:
        result = predict_mtf()

        if isinstance(result, dict):
            return float(result.get("prob_up", 0.5))
        return float(result)

    except TypeError:
        pass
    except Exception:
        logger.exception("[EXIT_AI] predict_mtf no-arg failed")

    # ========================================================
    # ③ 旧引数型
    # ========================================================
    try:
        result = predict_mtf(
            latest_1m or {},
            latest_3m or {},
            latest_5m or {},
            daily or {},
        )

        if isinstance(result, dict):
            return float(result.get("prob_up", 0.5))
        return float(result)

    except Exception:
        logger.exception("[EXIT_AI] predict_mtf legacy failed")

    # ========================================================
    # 最終フォールバック
    # ========================================================
    return 0.5


# ============================================================
# EXITスコア算出
# ============================================================

def compute_exit_score(
    prob_up: float,
    mtf_score: float,
    news_score: float,
    sns_score: float,
    m1: Dict,
    m3: Dict,
    daily: Dict,
    ranking: Dict,
) -> float:
    """
    AI主導 EXIT スコア算出
    """

    prob_down = 1.0 - float(prob_up or 0.0)
    score = 0.0

    # --------------------------------------------------------
    # ① 下落確率（最重要）
    # --------------------------------------------------------
    score += prob_down * 100.0 * 0.5

    # --------------------------------------------------------
    # ② MTF弱化
    # --------------------------------------------------------
    if float(mtf_score or 0.0) < 50:
        score += 10

    # --------------------------------------------------------
    # ③ ニュース悪化
    # --------------------------------------------------------
    if float(news_score or 50) < 40:
        score += 15

    # --------------------------------------------------------
    # ④ SNSネガティブ
    # --------------------------------------------------------
    if float(sns_score or 50) < 40:
        score += 10

    # --------------------------------------------------------
    # ⑤ 1分足失速
    # --------------------------------------------------------
    if float(m1.get("m1_ma5_slope", 0)) < 0:
        score += 10

    if float(m1.get("m1_rsi_delta", 0)) < -3:
        score += 8

    # --------------------------------------------------------
    # ⑥ 3分足失速
    # --------------------------------------------------------
    if float(m3.get("m3_macd_delta", 0)) < 0:
        score += 10

    # --------------------------------------------------------
    # ⑦ 日足弱位置
    # --------------------------------------------------------
    if float(daily.get("day_pos", 0.5)) < 0.3:
        score += 8

    # --------------------------------------------------------
    # ⑧ ランキング失速
    # --------------------------------------------------------
    if float(ranking.get("rank_gain_speed", 0)) < 0:
        score += 5

    return float(score)


# ============================================================
# EXIT判定（公開API）
# ============================================================

def exit_decision_ai(
    symbol: str,
    latest_1m: Dict,
    latest_3m: Dict,
    latest_5m: Dict,
    daily: Dict,
    ranking: Dict,
    news_score: float,
    sns_score: float,
) -> Tuple[bool, float]:
    """
    AI主導 EXIT判定

    Returns:
        (exit_flag: bool, exit_score: float)
    """

    try:
        # ----------------------------------------------------
        # MTF上昇確率（完全互換ラッパ）
        # ----------------------------------------------------
        prob_up = _safe_predict_mtf(
            symbol,
            latest_1m,
            latest_3m,
            latest_5m,
            daily,
        )

        # ----------------------------------------------------
        # MTFスコア（Entry互換）
        # ----------------------------------------------------
        mtf_score = (
            prob_up * 100.0
            + (float(daily.get("day_pos", 0.5)) - 0.5) * 20.0
            + (1 if float(latest_3m.get("m3_ma5", 0)) > 0 else 0) * 10.0
        )

        # ----------------------------------------------------
        # EXITスコア算出
        # ----------------------------------------------------
        exit_score = compute_exit_score(
            prob_up,
            mtf_score,
            news_score,
            sns_score,
            latest_1m or {},
            latest_3m or {},
            daily or {},
            ranking or {},
        )

        logger.info(
            "[EXIT AI] %s EXIT_SCORE=%.2f (prob_up=%.2f)",
            symbol,
            exit_score,
            prob_up,
        )

        # ----------------------------------------------------
        # 判定
        # ----------------------------------------------------
        if exit_score >= EXIT_THRESHOLD:
            logger.info(
                "[EXIT AI] %s EXIT発火 (score=%.2f)",
                symbol,
                exit_score,
            )
            return True, exit_score

        return False, exit_score

    except Exception:
        logger.exception("[EXIT_AI_FATAL] symbol=%s", symbol)
        return False, 0.0