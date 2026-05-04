# candlestick_patterns.py
# ────────────────────────────────────────────────
# 日本株ローソク足パターン検出モジュール
# （買いシグナル／売りシグナルを網羅）
# ────────────────────────────────────────────────

import pandas as pd
from typing import List

# =========================================================
# 🔹 ユーティリティ関数
# =========================================================
REQUIRED_COLS = ["open_price", "close_price", "high_price", "low_price"]

def _validate_cols(df: pd.DataFrame) -> bool:
    """必要なカラムが揃っているか確認"""
    return all(col in df.columns for col in REQUIRED_COLS)

def _get_candle(df: pd.DataFrame, idx: int) -> dict:
    """指定した行を dict 形式で取得"""
    row = df.iloc[idx]
    return {
        "open_price": float(row["open_price"]),
        "close_price": float(row["close_price"]),
        "high_price": float(row["high_price"]),
        "low_price": float(row["low_price"]),
    }

def _is_bullish(candle) -> bool:
    return candle["close_price"] > candle["open_price"]

def _is_bearish(candle) -> bool:
    return candle["close_price"] < candle["open_price"]

def _body_length(candle) -> float:
    return abs(candle["close_price"] - candle["open_price"])

def _candle_range(candle) -> float:
    return candle["high_price"] - candle["low_price"]

def _upper_wick(candle) -> float:
    return candle["high_price"] - max(candle["open_price"], candle["close_price"])

def _lower_wick(candle) -> float:
    return min(candle["open_price"], candle["close_price"]) - candle["low_price"]


# =========================================================
# 🔹 買いシグナル系パターン（20種類）
# =========================================================
def is_three_white_soldiers(df):  # 赤三兵
    if len(df) < 3: return False
    c1, c2, c3 = _get_candle(df, -3), _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bullish(c1) and _is_bullish(c2) and _is_bullish(c3) and
            c2["open_price"] > c1["open_price"] and
            c3["open_price"] > c2["open_price"] and
            c3["close_price"] > c2["close_price"])

def is_morning_star(df):  # 明けの明星
    if len(df) < 3: return False
    c1, c2, c3 = _get_candle(df, -3), _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bearish(c1) and
            _body_length(c2) < _body_length(c1) * 0.5 and
            _is_bullish(c3) and
            c3["close_price"] > (c1["open_price"] + c1["close_price"]) / 2)

def is_bullish_engulfing(df):  # 抱き陽線
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bearish(c1) and _is_bullish(c2) and
            c2["open_price"] < c1["close_price"] and
            c2["close_price"] > c1["open_price"])

def is_piercing_line(df):  # 切り込み線
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bearish(c1) and _is_bullish(c2) and
            c2["close_price"] > (c1["open_price"] + c1["close_price"]) / 2)

def is_hammer(df):  # たくり線
    if len(df) < 1: return False
    c = _get_candle(df, -1)
    return _lower_wick(c) > _body_length(c) * 2 and _is_bullish(c)

def is_window_up(df):  # 窓開け（上窓）
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return c2["low_price"] > c1["high_price"]

def is_bullish_belt_hold(df):  # 捨て子底
    if len(df) < 3: return False
    c1, c2, c3 = _get_candle(df, -3), _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bearish(c1) and
            abs(c2["open_price"] - c2["close_price"]) < _body_length(c1) * 0.1 and
            _is_bullish(c3) and
            c3["close_price"] > (c1["open_price"] + c1["close_price"]) / 2)

def is_inverted_hammer(df):  # 勢力線
    if len(df) < 1: return False
    c = _get_candle(df, -1)
    return _upper_wick(c) > _body_length(c) * 2 and _is_bullish(c)

def is_dragonfly_doji(df):  # やぐら底
    if len(df) < 1: return False
    c = _get_candle(df, -1)
    return (_lower_wick(c) > _candle_range(c) * 0.7 and
            abs(c["open_price"] - c["close_price"]) <= _body_length(c) * 0.1)

def is_bullish_harami(df):  # 陽のはらみ
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bearish(c1) and _is_bullish(c2) and
            c2["open_price"] > c1["open_price"] and
            c2["close_price"] < c1["close_price"])

def is_rising_three_methods(df):  # 上げ三法
    if len(df) < 5: return False
    c1, c5 = _get_candle(df, -5), _get_candle(df, -1)
    middle = [_get_candle(df, -4), _get_candle(df, -3), _get_candle(df, -2)]
    return (_is_bullish(c1) and _is_bullish(c5) and
            all(_is_bearish(c) for c in middle) and
            c5["close_price"] > c1["close_price"])

def is_bullish_kicker(df):  # 逆襲線
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return _is_bearish(c1) and _is_bullish(c2) and c2["open_price"] > c1["open_price"]

def is_bullish_tasuki(df):  # 上げタスキ
    if len(df) < 3: return False
    c1, c2, c3 = _get_candle(df, -3), _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bullish(c1) and _is_bearish(c2) and _is_bullish(c3) and
            c3["close_price"] > c1["close_price"])

def is_lower_shadow_bullish(df):  # 下げの下ひげ
    if len(df) < 1: return False
    c = _get_candle(df, -1)
    return _is_bullish(c) and _lower_wick(c) > _body_length(c) * 2

def is_bullish_tweezer_bottom(df):  # 毛抜き底
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return (abs(c1["low_price"] - c2["low_price"]) <= _candle_range(c1) * 0.1 and
            _is_bullish(c2))

def is_bullish_breakaway(df):  # 窓開け後の陽線継続
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return c2["open_price"] > c1["high_price"] and _is_bullish(c2)

def is_bullish_counterattack(df):  # 押え込み線
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bearish(c1) and _is_bullish(c2) and
            c2["close_price"] == c1["close_price"])

def is_bullish_side_by_side(df):  # 並び赤
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bullish(c1) and _is_bullish(c2) and
            abs(c1["close_price"] - c2["close_price"]) < _body_length(c1) * 0.1)

def is_bullish_mat_hold(df):  # 上伸途上の連続タスキ
    if len(df) < 5: return False
    c1, c5 = _get_candle(df, -5), _get_candle(df, -1)
    middle = [_get_candle(df, -4), _get_candle(df, -3), _get_candle(df, -2)]
    return (_is_bullish(c1) and _is_bullish(c5) and
            all(_is_bearish(c) for c in middle) and
            c5["close_price"] > c1["close_price"])

# =========================================================
# 🔹 売りシグナル系パターン（20種類）
# =========================================================
def is_three_black_crows(df):  # 三羽烏
    if len(df) < 3: return False
    c1, c2, c3 = _get_candle(df, -3), _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bearish(c1) and _is_bearish(c2) and _is_bearish(c3) and
            c2["open_price"] < c1["open_price"] and
            c3["open_price"] < c2["open_price"] and
            c3["close_price"] < c2["close_price"])

def is_evening_star(df):  # 宵の明星
    if len(df) < 3: return False
    c1, c2, c3 = _get_candle(df, -3), _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bullish(c1) and
            _body_length(c2) < _body_length(c1) * 0.5 and
            _is_bearish(c3) and
            c3["close_price"] < (c1["open_price"] + c1["close_price"]) / 2)

def is_bearish_engulfing(df):  # 抱き陰線
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bullish(c1) and _is_bearish(c2) and
            c2["open_price"] > c1["close_price"] and
            c2["close_price"] < c1["open_price"])

def is_dark_cloud_cover(df):  # カブセ線
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bullish(c1) and _is_bearish(c2) and
            c2["close_price"] < (c1["open_price"] + c1["close_price"]) / 2)

def is_hanging_man(df):  # 首吊り線
    if len(df) < 1: return False
    c = _get_candle(df, -1)
    return _lower_wick(c) > _body_length(c) * 2 and _is_bearish(c)

def is_window_down(df):  # 窓開け（下窓）
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return c2["high_price"] < c1["low_price"]

def is_bearish_belt_hold(df):  # 化け線
    if len(df) < 1: return False
    c = _get_candle(df, -1)
    return _is_bearish(c) and abs(c["open_price"] - c["close_price"]) > _candle_range(c) * 0.7

def is_shooting_star(df):  # 上げの上ひげ
    if len(df) < 1: return False
    c = _get_candle(df, -1)
    return _upper_wick(c) > _body_length(c) * 2 and _is_bearish(c)

def is_bearish_tweezer_top(df):  # 毛抜き天井
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return (abs(c1["high_price"] - c2["high_price"]) <= _candle_range(c1) * 0.1 and
            _is_bearish(c2))

def is_upside_gap_two_crows(df):  # 下方窓開け（上昇後の陰線2本）
    if len(df) < 3: return False
    c1, c2, c3 = _get_candle(df, -3), _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bullish(c1) and _is_bearish(c2) and _is_bearish(c3) and
            c2["open_price"] > c1["close_price"] and
            c3["open_price"] > c2["close_price"])

def is_bearish_kicker(df):  # 新値八手利食い線
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return _is_bullish(c1) and _is_bearish(c2) and c2["open_price"] < c1["open_price"]

def is_bearish_counterattack(df):  # 行き詰まり線
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return _is_bullish(c1) and _is_bearish(c2) and c2["close_price"] == c1["close_price"]

def is_bearish_side_by_side(df):  # 放れ三手（陰線並び）
    if len(df) < 3: return False
    c1, c2, c3 = _get_candle(df, -3), _get_candle(df, -2), _get_candle(df, -1)
    return _is_bearish(c1) and _is_bearish(c2) and _is_bearish(c3)

def is_falling_three_methods(df):  # 下げ三法
    if len(df) < 5: return False
    c1, c5 = _get_candle(df, -5), _get_candle(df, -1)
    middle = [_get_candle(df, -4), _get_candle(df, -3), _get_candle(df, -2)]
    return (_is_bearish(c1) and _is_bearish(c5) and
            all(_is_bullish(c) for c in middle) and
            c5["close_price"] < c1["close_price"])

def is_bearish_mat_hold(df):  # 下落途上の連続タスキ
    if len(df) < 5: return False
    c1, c5 = _get_candle(df, -5), _get_candle(df, -1)
    middle = [_get_candle(df, -4), _get_candle(df, -3), _get_candle(df, -2)]
    return (_is_bearish(c1) and _is_bearish(c5) and
            all(_is_bullish(c) for c in middle) and
            c5["close_price"] < c1["close_price"])

def is_bearish_harami(df):  # 陰のはらみ
    if len(df) < 2: return False
    c1, c2 = _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bullish(c1) and _is_bearish(c2) and
            c2["open_price"] < c1["open_price"] and
            c2["close_price"] > c1["close_price"])

def is_bearish_doji_star(df):  # 天井圏の陰線
    if len(df) < 1: return False
    c = _get_candle(df, -1)
    return _is_bearish(c) and _body_length(c) < _candle_range(c) * 0.1

def is_bearish_tasuki(df):  # 下げタスキ
    if len(df) < 3: return False
    c1, c2, c3 = _get_candle(df, -3), _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bearish(c1) and _is_bullish(c2) and _is_bearish(c3) and
            c3["close_price"] < c1["close_price"])

def is_bearish_breakaway(df):  # 下放れ三手
    if len(df) < 3: return False
    c1, c2, c3 = _get_candle(df, -3), _get_candle(df, -2), _get_candle(df, -1)
    return (_is_bearish(c1) and _is_bearish(c2) and _is_bearish(c3) and
            c3["close_price"] < c1["close_price"])


# =========================================================
# 🔹 一括検出関数
# =========================================================
def detect_bullish_patterns(df: pd.DataFrame) -> List[str]:
    """
    ローソク足の強気パターンを一括で検出する。
    """
    if df is None or df.empty or len(df) < 2:
        return []

    patterns = []
    if is_three_white_soldiers(df): patterns.append("赤三兵")
    if is_morning_star(df): patterns.append("明けの明星")
    if is_bullish_engulfing(df): patterns.append("抱き陽線")
    if is_piercing_line(df): patterns.append("切り込み線")
    if is_hammer(df): patterns.append("たくり線")
    if is_window_up(df): patterns.append("上窓")
    if is_bullish_belt_hold(df): patterns.append("捨て子底")
    if is_inverted_hammer(df): patterns.append("勢力線")
    if is_dragonfly_doji(df): patterns.append("やぐら底")
    if is_bullish_harami(df): patterns.append("陽のはらみ")
    if is_rising_three_methods(df): patterns.append("上げ三法")
    if is_bullish_kicker(df): patterns.append("逆襲線")
    if is_bullish_tasuki(df): patterns.append("上げタスキ")
    if is_lower_shadow_bullish(df): patterns.append("下げの下ひげ")
    if is_bullish_tweezer_bottom(df): patterns.append("毛抜き底")
    if is_bullish_breakaway(df): patterns.append("窓開け後の陽線継続")
    if is_bullish_counterattack(df): patterns.append("押え込み線")
    if is_bullish_side_by_side(df): patterns.append("並び赤")
    if is_bullish_mat_hold(df): patterns.append("上伸途上の連続タスキ")

    # 独自のカスタムロジック
    candles = df.tail(1).reset_index(drop=True)
    if len(candles) >= 1:
        c = _get_candle(candles, -1)
        # 大陽線
        body_length = _body_length(c)
        if _is_bullish(c) and body_length > (_candle_range(c) * 0.6):
            patterns.append("大陽線")

    return patterns

def detect_bearish_patterns(df: pd.DataFrame) -> List[str]:
    """
    ローソク足の弱気パターンを一括で検出する。
    """
    if df is None or df.empty or len(df) < 2:
        return []

    patterns = []
    if is_three_black_crows(df): patterns.append("三羽烏")
    if is_evening_star(df): patterns.append("宵の明星")
    if is_bearish_engulfing(df): patterns.append("抱き陰線")
    if is_dark_cloud_cover(df): patterns.append("かぶせ線")
    if is_hanging_man(df): patterns.append("首吊り線")
    if is_window_down(df): patterns.append("下窓")
    if is_bearish_belt_hold(df): patterns.append("化け線")
    if is_shooting_star(df): patterns.append("上ヒゲ陰線")
    if is_bearish_tweezer_top(df): patterns.append("毛抜き天井")
    if is_upside_gap_two_crows(df): patterns.append("二羽ガラス")
    if is_bearish_kicker(df): patterns.append("弱気キッカー")
    if is_bearish_counterattack(df): patterns.append("行き詰まり線")
    if is_bearish_side_by_side(df): patterns.append("陰線並び")
    if is_falling_three_methods(df): patterns.append("下げ三法")
    if is_bearish_mat_hold(df): patterns.append("下落途上の連続タスキ")
    if is_bearish_harami(df): patterns.append("陰のはらみ")
    if is_bearish_doji_star(df): patterns.append("陰のコマ天井")
    if is_bearish_tasuki(df): patterns.append("下げタスキ")
    if is_bearish_breakaway(df): patterns.append("下放れ三手")

    # 独自のカスタムロジック
    candles = df.tail(1).reset_index(drop=True)
    if len(candles) >= 1:
        c = _get_candle(candles, -1)
        # 大陰線
        body_length = _body_length(c)
        if _is_bearish(c) and body_length > (_candle_range(c) * 0.6):
            patterns.append("大陰線")

    return patterns
