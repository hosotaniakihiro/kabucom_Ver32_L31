# ============================================================
# File   : trading/filters/mtf_filter.py
# Version: FINAL-MTF-FILTER-STABLE-V1
# ------------------------------------------------------------
# ✔ 1min主軸 + 3min/5min方向フィルタ
# ✔ BUY / SELL 両対応
# ✔ None / 欠損 完全耐性
# ✔ slope未存在安全処理
# ✔ スコア非依存（軽量高速）
# ✔ 将来拡張用 alignment score 対応
# ============================================================

from __future__ import annotations

from typing import Dict, Any


# ============================================================
# 安全取得ユーティリティ
# ============================================================

def _safe_get_slope(summary_dict: Dict[str, Dict[str, Any]], symbol: str) -> float:
    """
    summary_xxx_latest 形式:
        {
            "7203": {"ma75_slope": 0.012, ...},
            ...
        }
    """
    if summary_dict is None:
        return 0.0

    row = summary_dict.get(symbol)
    if not row:
        return 0.0

    slope = row.get("ma75_slope")

    if slope is None:
        return 0.0

    try:
        return float(slope)
    except Exception:
        return 0.0


# ============================================================
# MTF 方向一致判定
# ============================================================

def mtf_entry_filter(
    symbol: str,
    side: str,
    summary_1min_latest: Dict[str, Dict[str, Any]],
    summary_3min_latest: Dict[str, Dict[str, Any]],
    summary_5min_latest: Dict[str, Dict[str, Any]],
    *,
    require_alignment: bool = True,
) -> bool:
    """
    1minは主判断（AI score）
    3min / 5min は方向フィルタのみ使用

    BUY:
        3min & 5min の ma75_slope > 0

    SELL:
        3min & 5min の ma75_slope < 0
    """

    # --- 1min存在チェック ---
    if summary_1min_latest is None:
        return False

    if symbol not in summary_1min_latest:
        return False

    # --- 上位足 slope取得 ---
    slope3 = _safe_get_slope(summary_3min_latest, symbol)
    slope5 = _safe_get_slope(summary_5min_latest, symbol)

    # --- BUY判定 ---
    if side == "BUY":
        if require_alignment:
            return slope3 > 0 and slope5 > 0
        return slope3 > 0 or slope5 > 0

    # --- SELL判定 ---
    if side == "SELL":
        if require_alignment:
            return slope3 < 0 and slope5 < 0
        return slope3 < 0 or slope5 < 0

    return False


# ============================================================
# 方向一致度スコア（将来AI拡張用）
# ============================================================

def mtf_alignment_score(
    symbol: str,
    summary_3min_latest: Dict[str, Dict[str, Any]],
    summary_5min_latest: Dict[str, Dict[str, Any]],
) -> float:
    """
    -1.0 ～ +1.0 の連続値で方向一致度を返す
    将来AI特徴量として使用可能
    """

    slope3 = _safe_get_slope(summary_3min_latest, symbol)
    slope5 = _safe_get_slope(summary_5min_latest, symbol)

    score = 0.0

    if slope3 > 0:
        score += 0.5
    elif slope3 < 0:
        score -= 0.5

    if slope5 > 0:
        score += 0.5
    elif slope5 < 0:
        score -= 0.5

    return score


# ============================================================
# 逆行検知（EXIT用）
# ============================================================

def mtf_reversal_detected(
    symbol: str,
    side: str,
    summary_3min_latest: Dict[str, Dict[str, Any]],
    summary_5min_latest: Dict[str, Dict[str, Any]],
) -> bool:
    """
    ポジション保有中の逆行検知
    """

    slope3 = _safe_get_slope(summary_3min_latest, symbol)
    slope5 = _safe_get_slope(summary_5min_latest, symbol)

    if side == "BUY":
        return slope3 < 0 and slope5 < 0

    if side == "SELL":
        return slope3 > 0 and slope5 > 0

    return False