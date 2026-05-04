# ============================================================
# trading/ai/ai_gate.py
# Ver1.0-AI-GATE-VISIBLE-ONE-SYMBOL
# ------------------------------------------------------------
# ✔ AI通過の完全可視化
# ✔ 1銘柄1回だけAI判定
# ✔ conf=0.00 = AI未実行を保証
# ============================================================

import logging

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 設定
# ------------------------------------------------------------
AI_GATE_SCORE_BUY  = 6
AI_GATE_SCORE_SELL = -6


def run_ai_gate(
    *,
    symbol: str,
    side: str,            # "BUY" or "SELL"
    score_total: float,
    ai_func,              # 推論関数（callable）
    ai_kwargs: dict,      # AIに渡す特徴量
):
    """
    AI判定の単一エントリーポイント
    戻り値: (passed: bool, conf: float, reason: str)
    """

    # ------------------------------
    # ルール最終ゲート
    # ------------------------------
    if side == "BUY" and score_total < AI_GATE_SCORE_BUY:
        logger.info(
            f"[AI BLOCK] {symbol} BUY score={score_total} "
            f"conf=0.00 reason=rule score too low (AI SKIPPED)"
        )
        return False, 0.0, "rule score too low"

    if side == "SELL" and score_total > AI_GATE_SCORE_SELL:
        logger.info(
            f"[AI BLOCK] {symbol} SELL score={score_total} "
            f"conf=0.00 reason=rule score too low (AI SKIPPED)"
        )
        return False, 0.0, "rule score too low"

    # ------------------------------
    # ★ AI CALL（可視化ポイント）
    # ------------------------------
    logger.info(
        f"[AI CALL] symbol={symbol} side={side} score={score_total}"
    )

    try:
        ai_conf = float(ai_func(**ai_kwargs))
    except Exception:
        logger.exception(f"[AI ERROR] {symbol}")
        return False, 0.0, "ai error"

    # ------------------------------
    # ★ AI DONE（通過確定）
    # ------------------------------
    logger.info(
        f"[AI DONE] symbol={symbol} side={side} conf={ai_conf:.2f}"
    )

    # ------------------------------
    # AI confidence 判定
    # ------------------------------
    if ai_conf < 0.5:
        logger.info(
            f"[AI BLOCK] {symbol} {side} conf={ai_conf:.2f} "
            f"reason=ai confidence too low"
        )
        return False, ai_conf, "ai confidence too low"

    return True, ai_conf, "ai pass"
