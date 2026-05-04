# ============================================================
# trading/scoring/patterns/sell/sell_patterns_bonus.py
# ------------------------------------------------------------
# ・SELL トレンド継続・弱体化を評価する補助スコア
# ・イベント型ではなく「継続型（累積OK）」
# ・ENTRY とは完全分離
# ・score_config.ini を唯一の定義源とする
# ============================================================

from scoring.config.score_table import build_score_tables


# ============================================================
# 🔧 スコアテーブル（ini → dict）
# ============================================================
TABLES = build_score_tables()
SELL_BONUS_TABLE = TABLES["sell_bonus"]   # {signal_key: negative_score}


# ------------------------------------------------------------
# 🔧 安全 bool 判定
# ------------------------------------------------------------
def _b(v) -> bool:
    """None / NaN / 数値 / 文字列 を安全に bool 化"""
    try:
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        return str(v).strip().lower() in ("1", "true", "t", "yes", "y")
    except Exception:
        return False


# ------------------------------------------------------------
# 🔥 SELL BONUS（継続型）
# ------------------------------------------------------------
def get_sell_bonus_score(row):
    """
    SELL 側の継続ボーナススコアを算出

    Parameters
    ----------
    row : pd.Series | dict

    Returns
    -------
    score : int    # 常に負
    labels : list[str]
    """

    score = 0
    labels: list[str] = []

    # ========================================================
    # 🔴 SELL BONUS 判定（ini 定義ベース）
    # ========================================================
    for signal_key, signal_score in SELL_BONUS_TABLE.items():

        # RSI 系など数値判定が必要なものはここで拡張可能
        if signal_key.startswith("rsi_"):
            rsi = row.get("rsi")
            try:
                if rsi is None:
                    continue
                # rsi_falling / rsi_trend_weak などは
                # upstream でフラグ化されている前提
            except Exception:
                continue

        # 通常フラグ
        if not _b(row.get(signal_key)):
            continue

        score += int(signal_score)   # score は ini 側で必ず負
        labels.append(signal_key)

    return score, labels
