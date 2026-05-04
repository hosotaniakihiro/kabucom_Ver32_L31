# ============================================================
# trading/entry/warmup.py
# ------------------------------------------------------------
# ✔ PUSH登録直後のENTRY誤爆防止
# ✔ ローテーション前提（10秒/差し替え）対応
# ✔ EXITロジックとは完全分離
# ✔ global_data に依存（軽量・安全）
# ============================================================

from datetime import datetime
from global_state import global_data


# ------------------------------------------------------------
# ENTRY ウォームアップ係数
# ------------------------------------------------------------
def get_entry_warmup_factor(symbol: str) -> float:
    """
    PUSH登録直後の銘柄は ENTRY スコアを弱める

    戻り値:
        0.3 : 登録直後（0-5秒）
        0.7 : 準安定（5-10秒）
        1.0 : 安定（10秒以上 or 不明）
    """

    if not symbol:
        return 1.0

    # 登録時刻取得
    t0 = global_data.push_registered_at.get(str(symbol))
    if not t0:
        # 登録時刻不明 → 通常評価
        return 1.0

    try:
        elapsed = (datetime.now() - t0).total_seconds()
    except Exception:
        return 1.0

    if elapsed < 0:
        # 時刻逆転は無視
        return 1.0

    if elapsed < 5:
        return 0.3
    elif elapsed < 10:
        return 0.7
    else:
        return 1.0


# ------------------------------------------------------------
# デバッグ用（任意）
# ------------------------------------------------------------
def get_entry_warmup_state(symbol: str) -> str:
    """
    ログ・可視化用
    """
    t0 = global_data.push_registered_at.get(str(symbol))
    if not t0:
        return "UNKNOWN"

    elapsed = (datetime.now() - t0).total_seconds()

    if elapsed < 5:
        return "WARMUP_STRONG"
    elif elapsed < 10:
        return "WARMUP_WEAK"
    else:
        return "READY"
