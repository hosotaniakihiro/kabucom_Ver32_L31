# ============================================================
# trading/scoring/patterns/sell/sell_patterns_entry.py
# ------------------------------------------------------------
# ・SELL（ショート）エントリーを発生させる主シグナル
# ・天井・転換・失速を重視（イベント型）
# ・初回ヒットを強く評価、累積は bonus 側で処理
# ・BUY entry と完全対称
# ・score_config.ini を唯一の定義源とする
# ============================================================

from scoring.utils.state_tracker import signal_state
from scoring.config.score_table import build_score_tables


# ============================================================
# 🔧 スコアテーブル（ini → dict）
# ============================================================
TABLES = build_score_tables()
SELL_ENTRY_TABLE = TABLES["sell_entry"]  # {signal_key: negative_score}


# ------------------------------------------------------------
# 🔧 安全 bool 判定
# ------------------------------------------------------------
def _b(v) -> bool:
    """
    None / NaN / 数値 / 文字列 を安全に bool 化
    """
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
# 🔥 SELL ENTRY（イベント型）
# ------------------------------------------------------------
def get_sell_entry_score(row, symbol: str, state=None):
    """
    SELL エントリー用スコア算出（イベント型・初回 or 再許可）

    Parameters
    ----------
    row : pd.Series | dict
        シグナル・インジケータ行
    symbol : str
        銘柄コード
    state : Any
        将来拡張用（再発火条件など）

    Returns
    -------
    score : int    # 常に負
    labels : list[str]
    """

    score: int = 0
    labels: list[str] = []

    if not symbol:
        return score, labels

    # ========================================================
    # 🔴 SELL ENTRY 判定（ini 定義ベース）
    # ========================================================
    for signal_key, signal_score in SELL_ENTRY_TABLE.items():

        # ----------------------------
        # シグナルが立っていない
        # ----------------------------
        if not _b(row.get(signal_key)):
            continue

        # ----------------------------
        # 🔁 再ショート許可条件
        # ----------------------------
        # デフォルト：完全イベント型（再ENTRY不可）
        allow_reentry = False
        reentry_condition = False

        # --- 天井系：戻り高値失敗で再ショート許可 ---
        if signal_key in {
            "double_top",
            "upper_wick_series",
            "bear_big_combo",
        }:
            allow_reentry = True
            reentry_condition = _b(row.get("lower_high"))

        # --- MA / MACD 系：トレンドリセット後のみ再許可 ---
        elif signal_key in {
            "ma_dead_cross",
            "macd_dead_cross",
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
        # スコア加算（ini 側で必ず負）
        # ----------------------------
        try:
            score += int(signal_score)
        except Exception:
            continue

        labels.append(signal_key)

    return score, labels
