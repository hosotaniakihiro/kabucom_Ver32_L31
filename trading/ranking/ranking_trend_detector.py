# ============================================================
# trading/ranking/ranking_trend_detector.py
# ------------------------------------------------------------
# ✔ ranking_ma_1min 専用トレンド判定
# ✔ MA 配列 + 傾き + 価格位置
# ✔ ranking / tonosama / 初動 共通
# ============================================================

from typing import Dict, List


# ============================================================
# ユーティリティ
# ============================================================
def _is_increasing(seq: List[float]) -> bool:
    """
    単調増加（ノイズ1回まで許容）
    """
    if len(seq) < 2:
        return False
    downs = sum(seq[i] <= seq[i - 1] for i in range(1, len(seq)))
    return downs <= 1


# ============================================================
# メイン
# ============================================================
def calc_trend_score(ma_rows: List[Dict]) -> int:
    """
    ranking_ma_1min rows からトレンドスコアを算出

    ma_rows: [
        {
            "symbol": str,
            "datetime": str,
            "close": float,
            "ma5": float,
            "ma25": float,
            "ma75": float,
            "is_valid": int,
            ...
        },
        ...
    ]
    """
    if not ma_rows:
        return 0

    # 最新が最後に来る想定
    last = ma_rows[-1]

    if not last.get("is_valid"):
        return 0

    score = 0

    close = last.get("close")
    ma5 = last.get("ma5")
    ma25 = last.get("ma25")
    ma75 = last.get("ma75")

    if not all(v is not None for v in (close, ma5, ma25, ma75)):
        return 0

    # ========================================================
    # ① MA 配列
    # ========================================================
    if ma5 > ma25 > ma75:
        score += 3

    # ========================================================
    # ② MA 傾き
    # ========================================================
    if len(ma_rows) >= 3:
        ma5_seq = [r["ma5"] for r in ma_rows[-3:] if r.get("ma5") is not None]
        ma25_seq = [r["ma25"] for r in ma_rows[-3:] if r.get("ma25") is not None]

        if len(ma5_seq) >= 3 and _is_increasing(ma5_seq):
            score += 2

        if len(ma25_seq) >= 3 and _is_increasing(ma25_seq):
            score += 1

    # ========================================================
    # ③ 価格位置
    # ========================================================
    if close > ma5:
        score += 1

    return score
