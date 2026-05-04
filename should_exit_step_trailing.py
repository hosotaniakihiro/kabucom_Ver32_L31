from configparser import ConfigParser
from utils_common import format_hold_duration

def should_exit_step_trailing(symbol, current_price, pos):
    """
    階段式トレーリングストップ判定
    戻り値: (bool, reason, step_level, exit_price)
    """
    conf = ConfigParser()
    conf.read("settings.ini", encoding="utf-8")

    trigger_pct   = conf.getfloat("trailing", "trigger_percent", fallback=0.5)
    step_pct      = conf.getfloat("trailing", "step_percent", fallback=0.5)
    stop_loss_pct = conf.getfloat("trailing", "stop_loss_percent", fallback=-0.5)

    entry_price = pos.avg_price

    # --- 損切りライン ---
    stop_loss_price = entry_price * (1 + stop_loss_pct / 100.0)
    if pos.side == "BUY_CREDIT" and current_price <= stop_loss_price:
        return True, f"損切りライン割れ {stop_loss_price:.2f}円", 0, stop_loss_price
    if pos.side == "SELL_CREDIT" and current_price >= stop_loss_price:
        return True, f"損切りライン割れ {stop_loss_price:.2f}円", 0, stop_loss_price

    # --- 買い ---
    if pos.side == "BUY_CREDIT":
        gain_pct = (current_price - entry_price) / entry_price * 100
        if gain_pct < trigger_pct:
            return False, None, 0, None

        if pos.highest_price is None or current_price > pos.highest_price:
            pos.highest_price = current_price

        step_level = int((pos.highest_price - entry_price) / (entry_price * step_pct / 100.0))
        exit_price = entry_price * (1 + (step_level - 1) * step_pct / 100.0)

        if current_price <= exit_price:
            return True, f"階段トレーリング発動 (BUY {step_level}段目, 利確ライン={exit_price:.2f}円)", step_level, exit_price

    # --- 空売り ---
    elif pos.side == "SELL_CREDIT":
        loss_pct = (entry_price - current_price) / entry_price * 100
        if loss_pct < trigger_pct:
            return False, None, 0, None

        if pos.lowest_price is None or current_price < pos.lowest_price:
            pos.lowest_price = current_price

        step_level = int((entry_price - pos.lowest_price) / (entry_price * step_pct / 100.0))
        exit_price = entry_price * (1 - (step_level - 1) * step_pct / 100.0)

        if current_price >= exit_price:
            return True, f"階段トレーリング発動 (SELL {step_level}段目, 利確ライン={exit_price:.2f}円)", step_level, exit_price

    return False, None, 0, None
