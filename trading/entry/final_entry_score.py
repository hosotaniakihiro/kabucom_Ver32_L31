# ============================================================
# File   : trading/entry/final_entry_score.py
# ------------------------------------------------------------
# ✔ ENTRY 最終スコア算出ロジック
# ✔ raw_score × MA信頼度 × source × AI補正
# ✔ if地獄を避けた連続値制御
# ✔ fallback / ranking を安全に減衰
# ✔ MA75_conf を最終安全弁として使用
# ============================================================

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 設定値（チューニング可能）
# ------------------------------------------------------------
SOURCE_FACTOR = {
    "push": 1.0,
    "ranking": 0.95,
    "fallback": 0.6,
}

AI_GAIN = 0.5          # AI 補正の強さ
MIN_MA75_CONF = 0.6    # 絶対安全弁


# ============================================================
# 最終 ENTRY スコア計算
# ============================================================
def calc_final_entry_score(
    *,
    raw_score: float,
    ma25_conf: Optional[float],
    ma75_conf: Optional[float],
    source: str = "push",
    ai_prob: Optional[float] = None,
) -> float:
    """
    ENTRY 用の最終スコアを算出する

    Parameters
    ----------
    raw_score : float
        scoring_core による生スコア
    ma25_conf : float | None
        MA25 の信頼度
    ma75_conf : float | None
        MA75 の信頼度
    source : str
        'push' / 'ranking' / 'fallback'
    ai_prob : float | None
        AI 予測確率（0〜1）

    Returns
    -------
    float
        最終 ENTRY スコア（0 以下は ENTRY 不可）
    """

    # -----------------------------
    # 基本ガード
    # -----------------------------
    if raw_score is None:
        return 0.0

    if ma75_conf is None or ma75_conf < MIN_MA75_CONF:
        logger.debug(
            "[ENTRY_SCORE] blocked by ma75_conf: %s", ma75_conf
        )
        return 0.0

    # -----------------------------
    # MA 信頼度係数
    # -----------------------------
    ma25_conf = ma25_conf or 0.0
    ma75_conf = ma75_conf or 0.0

    ma_conf_factor = (
        0.2
        + 0.4 * ma25_conf
        + 0.4 * ma75_conf
    )

    # -----------------------------
    # source 係数
    # -----------------------------
    source_factor = SOURCE_FACTOR.get(source, 1.0)

    # -----------------------------
    # スコア合成（AI前）
    # -----------------------------
    final_score = raw_score
    final_score *= ma_conf_factor
    final_score *= source_factor

    # -----------------------------
    # AI 補正（自信がある時のみ）
    # -----------------------------
    if ai_prob is not None and ai_prob >= 0.55:
        ai_boost = 1.0 + (ai_prob - 0.5) * AI_GAIN
        final_score *= ai_boost

    logger.debug(
        "[ENTRY_SCORE] raw=%s ma25_conf=%s ma75_conf=%s "
        "source=%s ai_prob=%s final=%s",
        raw_score,
        ma25_conf,
        ma75_conf,
        source,
        ai_prob,
        final_score,
    )

    return float(final_score)
