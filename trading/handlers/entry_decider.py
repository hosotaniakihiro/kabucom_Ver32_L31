# ============================================================
# trading/handlers/entry_decider.py
# Ver1.0-SCORE-ONLY-ENTRY
# ============================================================

from trading.scoring.config.score_config import (
    ENTRY_THRESHOLD,
    SELL_THRESHOLD,
)

def decide_entry_from_score(score_total: int) -> str:
    """
    ENTRY 判定は score_total のみ
    """
    if score_total >= ENTRY_THRESHOLD:
        return "BUY"
    if score_total <= SELL_THRESHOLD:
        return "SELL"
    return "NONE"
