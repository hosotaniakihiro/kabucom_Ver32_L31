# ============================================================
# AI/retrain/validator.py
# ============================================================

def validate_params(new: dict, old: dict) -> bool:
    """
    危険な急変を防ぐ
    """

    if not old:
        return True

    # cooldown が急増しすぎない
    if new["cooldown_minutes"] > old["cooldown_minutes"] * 2:
        return False

    # DD 許容が緩みすぎない
    if new["max_intraday_dd"] < old["max_intraday_dd"] * 1.5:
        return False

    # 連敗許容が跳ねない
    if abs(new["max_loss_streak"] - old["max_loss_streak"]) > 2:
        return False

    return True
