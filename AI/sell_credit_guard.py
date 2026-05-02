# ============================================================
# File: AI/sell_credit_guard.py
# ------------------------------------------------------------
# 殿様イナゴ（SELL）専用 信用・売禁ガード
#
# ✔ 信用売り可否を最優先で判定
# ✔ 売禁・規制・高保証金率を完全遮断
# ✔ ENTRY ロジックとは完全独立
# ✔ True / False のみを返す純関数
# ============================================================

from __future__ import annotations
from typing import Dict


# ============================================================
# 固定パラメータ（絶対に変更しない）
# ============================================================

# 高保証金率とみなす閾値
MAX_MARGIN_RATE = 2.0   # 200% 以上は危険


# ============================================================
# メイン API
# ============================================================

def can_sell_symbol(symbol_flags: Dict) -> bool:
    """
    SELL 殿様で信用売り可能かを判定する

    Parameters
    ----------
    symbol_flags : dict
        銘柄フラグ情報（DB / API 由来）
        想定キー:
          - short_sellable : bool
          - sell_ban : bool
          - margin_rate : float

    Returns
    -------
    bool
        True  : SELL 可
        False : SELL 禁止
    """

    if not symbol_flags:
        return False

    # --------------------------------------------------------
    # 信用売り不可
    # --------------------------------------------------------
    if not symbol_flags.get("short_sellable", False):
        return False

    # --------------------------------------------------------
    # 売禁
    # --------------------------------------------------------
    if symbol_flags.get("sell_ban", False):
        return False

    # --------------------------------------------------------
    # 高保証金率（踏み上げ地雷）
    # --------------------------------------------------------
    margin_rate = symbol_flags.get("margin_rate")
    if margin_rate is not None:
        try:
            if float(margin_rate) >= MAX_MARGIN_RATE:
                return False
        except (TypeError, ValueError):
            return False

    return True