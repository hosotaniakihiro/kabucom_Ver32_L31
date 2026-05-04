import pandas as pd

# === 買い用 ===
def cond_ma5_cross(symbol, curr, prev, alert_data):
    """MA5を下から上に突き抜け（買い用）"""
    cross_counter = alert_data[symbol].get("cross_counter_long", 0)

    if (curr["close_price"] > curr["ma5"]) and (prev["close_price"] <= prev["ma5"]):
        cross_counter += 1
        alert_data[symbol]["cross_counter_long"] = cross_counter
        return True, "MA5ゴールデンクロス発生", 2  # ✅ 加点2
    return False, "", 0


def cond_bullish_4(df_recent, curr):
    """MA75以下で陽線4本連続（買い用）"""
    if len(df_recent) < 4:
        return False, "", 0
    last4 = df_recent.sort_values(by="date").tail(4)
    bullish_count = (last4["close_price"] > last4["open_price"]).sum()
    if bullish_count == 4 and curr["close_price"] <= curr["ma75"]:
        return True, "MA75以下で陽線4本連続", 3  # ✅ 加点3
    return False, "", 0


# === 売り用 ===
def cond_ma5_cross_short(symbol, curr, prev, alert_data):
    """MA5を上から下に突き抜け（売り用）"""
    cross_counter = alert_data[symbol].get("cross_counter_short", 0)

    if (curr["close_price"] < curr["ma5"]) and (prev["close_price"] >= prev["ma5"]):
        cross_counter += 1
        alert_data[symbol]["cross_counter_short"] = cross_counter
        return True, "MA5デッドクロス発生", -2  # ✅ 減点2
    return False, "", 0


def cond_bearish_4(df_recent, curr):
    """MA75以上で陰線4本連続（売り用）"""
    if len(df_recent) < 4:
        return False, "", 0
    last4 = df_recent.sort_values(by="date").tail(4)
    bearish_count = (last4["close_price"] < last4["open_price"]).sum()
    if bearish_count == 4 and curr["close_price"] >= curr["ma75"]:
        return True, "MA75以上で陰線4本連続", -3  # ✅ 減点3
    return False, "", 0


# === 条件リスト ===
buy_conditions = [
    cond_ma5_cross,
    cond_bullish_4,
]

short_conditions = [
    cond_ma5_cross_short,
    cond_bearish_4,
]
