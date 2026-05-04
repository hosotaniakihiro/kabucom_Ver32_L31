#score_config_loader.py

import configparser
from types import SimpleNamespace

def load_score_config(path="score_config.ini"):
    """
    score_config.ini を読み込み
    - [trade] は SimpleNamespace として返す（定数風アクセス可能）
    - [scoring], [short_scoring] は辞書として返す
    例:
        trade.threshold
        scoring_dict['gap_up_breakout']
    """
    config = configparser.ConfigParser()
    config.read(path, encoding="utf-8")

    # --- [trade] を SimpleNamespace に変換
    trade_ns = SimpleNamespace()
    if "trade" in config:
        for key in config.options("trade"):
            value = config.get("trade", key)
            # 数値なら int または float に変換
            if value.replace(".", "", 1).isdigit() or (value.startswith('-') and value[1:].replace(".", "", 1).isdigit()):
                if "." in value:
                    setattr(trade_ns, key, float(value))
                else:
                    setattr(trade_ns, key, int(value))
            else:
                setattr(trade_ns, key, value)

    # --- [scoring] を辞書化
    scoring_dict = {}
    if "scoring" in config:
        for key in config.options("scoring"):
            scoring_dict[key] = config.getfloat("scoring", key)

    # --- [short_scoring] を辞書化
    short_scoring_dict = {}
    if "short_scoring" in config:
        for key in config.options("short_scoring"):
            short_scoring_dict[key] = config.getfloat("short_scoring", key)

    return trade_ns, scoring_dict, short_scoring_dict


# ===== 使用例 =====
if __name__ == "__main__":
    trade, scoring_dict, short_scoring_dict = load_score_config("score_config.ini")

    # 定数風アクセス
    print("threshold:", trade.threshold)
    print("budget:", trade.budget)
    print("default_exit_order_type:", trade.default_exit_order_type)

    # 辞書アクセス
    print("gap_up_breakout:", scoring_dict["gap_up_breakout"])
    print("gap_down_breakdown:", short_scoring_dict["gap_down_breakdown"])
