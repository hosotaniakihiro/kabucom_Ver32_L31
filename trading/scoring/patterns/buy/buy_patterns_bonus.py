# ============================================================
# trading/scoring/patterns/buy/buy_patterns_bonus.py
# ------------------------------------------------------------
# ・BUY トレンド継続・勢いの補助加点
# ・ENTRY 後に効かせる BONUS 用シグナル
# ・累積 OK（イベント型ではない）
# ・sell_patterns_bonus.py と完全対称
# ・score_config.ini を唯一の定義源とする
# ============================================================

from scoring.config.score_table import build_score_tables


# ============================================================
# 🔧 スコアテーブル（ini → dict）
# ============================================================
TABLES = build_score_tables()
BUY_BONUS_TABLE = TABLES["buy_bonus"]


# ------------------------------------------------------------
# 🔧 安全 bool 判定
# ------------------------------------------------------------
def _b(v) -> bool:
    """None / NaN / 数値 を安全に bool 化"""
    try:
        return bool(v)
    except Exception:
        return False


# ------------------------------------------------------------
# 🔥 BUY BONUS（継続型）
# ------------------------------------------------------------
def get_buy_bonus_score(row):
    """
    BUY ボーナススコア算出（トレンド継続・勢い）

    Parameters
    ----------
    row : pd.Series | dict

    Returns
    -------
    score : int
    labels : list[str]
    """

    score = 0
    labels: list[str] = []

    # ========================================================
    # 🔵 BUY BONUS 判定（ini 定義ベース）
    # ========================================================
    for signal_key, signal_score in BUY_BONUS_TABLE.items():

        # --- RSI 系のみ数値判定を許可 ---
        if signal_key.startswith("rsi_"):
            rsi = row.get("rsi")
            try:
                if rsi is None:
                    continue
                # rsi_trend_strong などは ini 側で意味を定義
                score += signal_score
                labels.append(signal_key)
            except Exception:
                continue
            continue

        # --- 通常フラグ ---
        if not _b(row.get(signal_key)):
            continue

        score += signal_score
        labels.append(signal_key)

    return score, labels
