# ============================================================
# AI/capital_ai.py
# ============================================================

def calc_position_size(confidence: float, drawdown: float) -> float:
    if confidence <= 0:
        return 0.0
    return round(confidence * max(0.3, 1.0 - drawdown), 2)
