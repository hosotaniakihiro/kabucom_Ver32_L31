# ============================================================
# trading/scoring/patterns/buy/buy_patterns_entry.py
# ------------------------------------------------------------
# ・BUY エントリーを発生させる主シグナル
# ・イベント型（初回のみ加点）
# ・SELL entry（sell_patterns_entry.py）と完全対称
# ・score_config.ini を唯一の定義源とする
# ============================================================

from scoring.utils.state_tracker import signal_state
from scoring.config.score_table import build_score_tables


# ============================================================
# 🔧 スコアテーブル（ini → dict）
# ============================================================
TABLES = build_score_tables()
BUY_ENTRY_TABLE = TABLES["buy_entry"]  # {signal_key: score}


# ------------------------------------------------------------
# 🔧 安全 bool 判定
# ------------------------------------------------------------
def _b(v) -> bool:
    """
    None / NaN / 数値 / bool を安全に bool 化
    """
    try:
        if v is None:
            return False
        return bool(v)
    except Exception:
        return False


# ------------------------------------------------------------
# 🔥 BUY ENTRY（イベント型）
# ------------------------------------------------------------
def get_buy_entry_score(row, symbol: str, state=None):
    """
    BUY エントリー用スコア算出（イベント型）

    Parameters
    ----------
    row : pd.Series | dict
        シグナル・インジケータ行
    symbol : str
        銘柄コード
    state : Any
        将来拡張用（未使用・SELL 側と対称）

    Returns
    -------
    score : int
    labels : list[str]
    """

    score: int = 0
    labels: list[str] = []

    if not symbol:
        return score, labels

    # ========================================================
    # 🔵 BUY ENTRY 判定（ini 定義ベース）
    # ========================================================
    for signal_key, signal_score in BUY_ENTRY_TABLE.items():

        # ----------------------------
        # シグナルが立っていない
        # ----------------------------
        if not _b(row.get(signal_key)):
            continue

        # ----------------------------
        # 🔁 再エントリー許可条件
        # ----------------------------
        # デフォルト：完全イベント型（再ENTRY不可）
        allow_reentry = False
        reentry_condition = False

        # --- 高値更新型：高値更新で再ENTRY許可 ---
        if signal_key in {
            "breakout_high",
            "bull_big_combo",
            "window_up",
        }:
            allow_reentry = True
            reentry_condition = _b(row.get("higher_high"))

        # --- クロス系：トレンドリセット後のみ再許可 ---
        elif signal_key in {
            "ma5_ma25_cross",
            "macd_gc",
        }:
            allow_reentry = True
            reentry_condition = _b(row.get("trend_reset"))

        # ----------------------------
        # 🔥 初回 or 再許可判定
        # ----------------------------
        if not signal_state.is_first(
            symbol,
            signal_key,
            allow_reentry=allow_reentry,
            reentry_condition=reentry_condition,
        ):
            continue

        # ----------------------------
        # スコア加算
        # ----------------------------
        try:
            score += int(signal_score)
        except Exception:
            continue

        labels.append(signal_key)

    return score, labels
