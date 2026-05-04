import pandas as pd
from configparser import ConfigParser

# === 設定読み込み ===
score_config = ConfigParser()
score_config.read("score_config.ini", encoding="utf-8")


def _get_score(section: str, key: str, default: int) -> int:
    """INIからスコアを取得（なければデフォルト値）"""
    try:
        return score_config.getint(section, key, fallback=default)
    except Exception:
        return default


REQUIRED_COLS = ["open_price", "close_price", "high_price", "low_price"]


def _validate(df: pd.DataFrame) -> bool:
    """ローソク足の必須カラムが揃っているか検証"""
    return all(col in df.columns for col in REQUIRED_COLS)


# =====================================================
# 🔹 買いパターン（Bullish）
# =====================================================
def detect_bullish_patterns(df: pd.DataFrame):
    """
    買いシグナル系ローソク足パターンを検出
    戻り値: [(パターン名, スコア)] のリスト
    """
    if not _validate(df) or len(df) < 3:
        return []

    results = []
    last3 = df.tail(3).reset_index(drop=True)
    c1, c2, c3 = last3.iloc[0], last3.iloc[1], last3.iloc[2]

    # 出来高・トレンド・MACD など補助データ
    latest_vol = df["volume"].iloc[-1] if "volume" in df.columns else None
    avg_vol = df["volume"].tail(20).mean() if "volume" in df.columns else None
    ma5 = df["ma5"].iloc[-1] if "ma5" in df.columns else None
    ma25 = df["ma25"].iloc[-1] if "ma25" in df.columns else None
    ma75 = df["ma75"].iloc[-1] if "ma75" in df.columns else None
    macd = df["macd"].iloc[-1] if "macd" in df.columns else None
    signal = df["signal"].iloc[-1] if "signal" in df.columns else None

    # --- 大陽線 ---
    if c3["close_price"] > c3["open_price"]:
        results.append(("大陽線", _get_score("pattern_buy", "bull_big", 2)))

    # --- 明けの明星 ---
    if (c1["close_price"] < c1["open_price"] and
        c2["close_price"] < c2["open_price"] and
        c3["close_price"] > c3["open_price"] and
        c3["close_price"] > (c1["open_price"] + c1["close_price"]) / 2):
        results.append(("明けの明星", _get_score("pattern_buy", "morning_star", 2)))

    # --- 捨て子底 ---
    if (abs(c2["close_price"] - c2["open_price"]) < (c2["high_price"] - c2["low_price"]) * 0.1 and
        c1["close_price"] < c1["open_price"] and
        c3["close_price"] > c3["open_price"]):
        results.append(("捨て子底", _get_score("pattern_buy", "abandoned_baby", 2)))

    # --- 毛抜き底 ---
    if abs(c1["low_price"] - c2["low_price"]) < 0.01 * c2["low_price"]:
        results.append(("毛抜き底", _get_score("pattern_buy", "tweezer_bottom", 2)))

    # --- 赤三兵 ---
    if (c1["close_price"] > c1["open_price"] and
        c2["close_price"] > c2["open_price"] and
        c3["close_price"] > c3["open_price"]):
        results.append(("赤三兵", _get_score("pattern_buy", "three_white_soldiers", 2)))

    # --- 包み足（陽線反転） ---
    if (c2["open_price"] > c2["close_price"] and
        c3["open_price"] < c2["close_price"] and
        c3["close_price"] > c2["open_price"] and
        c3["close_price"] > c3["open_price"]):
        results.append(("包み足（陽線反転）", _get_score("pattern_buy", "bullish_engulfing", 2)))

    # --- 陽のはらみ + 大陽線 ---
    if (c2["open_price"] < c2["close_price"] and
        c1["open_price"] < c2["open_price"] and c1["close_price"] > c2["close_price"] and
        c3["close_price"] > c3["open_price"]):
        results.append(("陽のはらみ → 大陽線", _get_score("pattern_buy", "bullish_harami", 2)))

    # --- 強力シグナル: 大陽線 + 出来高 + 上昇トレンド + MACD ---
    if (c3["close_price"] > c3["open_price"] and
        latest_vol and avg_vol and latest_vol > avg_vol * 1.5 and
        ma5 and ma25 and ma75 and ma5 > ma25 > ma75 and
        macd and signal and macd > signal):
        results.append((
            "大陽線 + 出来高 + 上昇トレンド + MACD",
            _get_score("pattern_buy", "bull_big_combo", 5)
        ))

    return results


# =====================================================
# 🔹 売りパターン（Bearish）
# =====================================================
def detect_bearish_patterns(df: pd.DataFrame):
    """
    売りシグナル系ローソク足パターンを検出
    戻り値: [(パターン名, スコア)] のリスト
    """
    if not _validate(df) or len(df) < 3:
        return []

    results = []
    last3 = df.tail(3).reset_index(drop=True)
    c1, c2, c3 = last3.iloc[0], last3.iloc[1], last3.iloc[2]

    # 出来高・トレンド・MACD など補助データ
    latest_vol = df["volume"].iloc[-1] if "volume" in df.columns else None
    avg_vol = df["volume"].tail(20).mean() if "volume" in df.columns else None
    ma5 = df["ma5"].iloc[-1] if "ma5" in df.columns else None
    ma25 = df["ma25"].iloc[-1] if "ma25" in df.columns else None
    ma75 = df["ma75"].iloc[-1] if "ma75" in df.columns else None
    macd = df["macd"].iloc[-1] if "macd" in df.columns else None
    signal = df["signal"].iloc[-1] if "signal" in df.columns else None

    # --- 大陰線 ---
    if c3["close_price"] < c3["open_price"]:
        results.append(("大陰線", _get_score("pattern_sell", "bear_big", -2)))

    # --- 三羽ガラス ---
    if (c1["close_price"] < c1["open_price"] and
        c2["close_price"] < c2["open_price"] and
        c3["close_price"] < c3["open_price"]):
        results.append(("三羽ガラス", _get_score("pattern_sell", "three_black_crows", -2)))

    # --- 夕べの明星 ---
    if (c1["close_price"] > c1["open_price"] and
        c2["close_price"] > c2["open_price"] and
        c3["close_price"] < c3["open_price"] and
        c3["close_price"] < (c1["open_price"] + c1["close_price"]) / 2):
        results.append(("夕べの明星", _get_score("pattern_sell", "evening_star", -2)))

    # --- 毛抜き天井 ---
    if abs(c1["high_price"] - c2["high_price"]) < 0.01 * c2["high_price"]:
        results.append(("毛抜き天井", _get_score("pattern_sell", "tweezer_top", -2)))

    # --- 化け線 ---
    if (c2["close_price"] < c2["open_price"] and
        c3["close_price"] < c3["open_price"] and
        c3["close_price"] < c2["close_price"]):
        results.append(("化け線", _get_score("pattern_sell", "dark_cloud_cover", -2)))

    # --- 二羽ガラス ---
    if (c1["close_price"] < c1["open_price"] and
        c2["close_price"] < c2["open_price"]):
        results.append(("二羽ガラス", _get_score("pattern_sell", "two_crows", -2)))

    # --- 包み足（陰線反転） ---
    if (c2["open_price"] < c2["close_price"] and
        c3["open_price"] > c2["close_price"] and
        c3["close_price"] < c2["open_price"] and
        c3["close_price"] < c3["open_price"]):
        results.append(("包み足（陰線反転）", _get_score("pattern_sell", "bearish_engulfing", -2)))

    # --- 強力シグナル: 大陰線 + 出来高 + 下降トレンド + MACD ---
    if (c3["close_price"] < c3["open_price"] and
        latest_vol and avg_vol and latest_vol > avg_vol * 1.5 and
        ma5 and ma25 and ma75 and ma5 < ma25 < ma75 and
        macd and signal and macd < signal):
        results.append((
            "大陰線 + 出来高 + 下降トレンド + MACD",
            _get_score("pattern_sell", "bear_big_combo", -5)
        ))

    return results
