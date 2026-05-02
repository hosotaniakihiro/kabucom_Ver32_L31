# ============================================================
# trading/ranking/ma75_filter.py
# Ver: MA75-TREND-FILTER-FINAL-NONE-SAFE
# ------------------------------------------------------------
# ✔ ranking ENTRY 前の最終トレンドフィルター
# ✔ close > MA75 かつ MA75 slope > 0 のみ通過
# ✔ summary 正本を前提とした設計
# ✔ summary_row None / 欠損・未準備・NaN に完全耐性
# ✔ dict / pandas.Series 両対応
# ✔ runtime を絶対に止めない
# ============================================================

from typing import Dict, Optional
import math
import logging

logger = logging.getLogger(__name__)


# ============================================================
# 内部ユーティリティ
# ============================================================

def _is_valid_number(v) -> bool:
    """
    数値として有効か（None / NaN / inf 排除）
    """
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return not (math.isnan(v) or math.isinf(v))
    return False


def _safe_get(row, key):
    """
    dict / pandas.Series / その他 Mapping 安全取得
    """
    try:
        if row is None:
            return None
        if hasattr(row, "get"):
            return row.get(key)
        if isinstance(row, dict):
            return row.get(key)
        return None
    except Exception:
        return None


# ============================================================
# MA75 フィルター本体
# ============================================================

def pass_ma75_filter(
    summary_row: Dict,
    *,
    require_slope: bool = True,
    min_slope: float = 0.0,
) -> bool:
    """
    MA75 トレンドフィルター

    Parameters
    ----------
    summary_row : dict | pandas.Series | None
        summary 系の 1 行（1min / 3min / 5min いずれでも可）
        必須キー:
          - close_price
          - ma75
        任意キー:
          - ma75_slope
    require_slope : bool
        True の場合、ma75_slope を必須とする
    min_slope : float
        MA75 slope の最小許容値（0.0 = 上向きのみ）

    Returns
    -------
    bool
        True  : ENTRY 許可
        False : ENTRY 不可
    """

    try:
        # ----------------------------
        # summary 未準備（最重要）
        # ----------------------------
        if summary_row is None:
            return False

        close_price = _safe_get(summary_row, "close_price")
        ma75 = _safe_get(summary_row, "ma75")
        slope = _safe_get(summary_row, "ma75_slope")

        # ----------------------------
        # 基本チェック
        # ----------------------------
        if not _is_valid_number(close_price):
            return False
        if not _is_valid_number(ma75):
            return False

        # ----------------------------
        # 価格位置（必須）
        # ----------------------------
        if close_price <= ma75:
            return False

        # ----------------------------
        # MA75 slope（任意）
        # ----------------------------
        if require_slope:
            if not _is_valid_number(slope):
                return False
            if slope <= min_slope:
                return False

        return True

    except Exception:
        # ★ runtime を絶対に止めない
        logger.exception("[MA75 FILTER] unexpected error")
        return False


# ============================================================
# 将来拡張用（例）
# ============================================================

def pass_ma_multi_filter(
    summary_row: Dict,
    *,
    ma_keys: Optional[list[str]] = None,
) -> bool:
    """
    複数 MA を使ったトレンド判定（将来用）

    Example:
        ma_keys = ["ma25", "ma75", "ma200"]

    条件:
        close > ma25 > ma75 > ma200
    """

    if not ma_keys:
        return True

    try:
        if summary_row is None:
            return False

        close_price = _safe_get(summary_row, "close_price")
        if not _is_valid_number(close_price):
            return False

        prev = close_price
        for k in ma_keys:
            v = _safe_get(summary_row, k)
            if not _is_valid_number(v):
                return False
            if prev <= v:
                return False
            prev = v

        return True

    except Exception:
        logger.exception("[MA MULTI FILTER] unexpected error")
        return False
