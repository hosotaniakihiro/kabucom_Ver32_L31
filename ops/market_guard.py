# ============================================================
# File: ops/market_guard.py
# Ver: FINAL-MARKET-GUARD-SELL-ENABLED-SAFE
# ------------------------------------------------------------
# 市場環境・インフラ異常のガード
#
# ✔ 地合い急変で ENTRY を即停止
# ✔ API / 板更新遅延などの障害検知
# ✔ BUY / SELL 両対応（SELL 緩和）
# ✔ ENTRY 可否のみを返す（副作用なし）
# ✔ 既存呼び出し完全互換
# ============================================================

from __future__ import annotations

import datetime as dt
from typing import Optional


# ============================================================
# 固定パラメータ（絶対に変更しない）
# ============================================================

# 地合い（指数）ガード
MIN_NIKKEI_VELOCITY = -0.002   # -0.2%/min で BUY 停止

# API 障害
MAX_API_429 = 3               # 429 が連続したら停止

# 板更新遅延
MAX_BOARD_DELAY_SEC = 2.0     # 秒

# フラッシュクラッシュ
FLASH_CRASH_DROP = -0.01      # -1%/min（BUY / SELL 共通）


# ============================================================
# メイン API
# ============================================================

def allow_entry_by_market(
    *,
    now: dt.datetime,
    side: Optional[str] = None,   # ★ 追加（"BUY" / "SELL" / None）
    nikkei_velocity: Optional[float] = None,
    api_429_count: int = 0,
    board_update_delay_sec: Optional[float] = None,
) -> bool:
    """
    市場・インフラ状況から ENTRY 可否を判定する

    Parameters
    ----------
    now : datetime
        現在時刻
    side : str, optional
        "BUY" or "SELL"（None の場合は BUY 扱い）
    nikkei_velocity : float, optional
        日経平均の直近1分変化率
    api_429_count : int
        API 429 エラーの連続回数
    board_update_delay_sec : float, optional
        板情報の更新遅延（秒）

    Returns
    -------
    bool
        True  : ENTRY 許可
        False : ENTRY 停止
    """

    # --------------------------------------------------------
    # safety
    # --------------------------------------------------------
    if now is None:
        return False

    side = side or "BUY"

    # --------------------------------------------------------
    # API 障害（最優先・BUY/SELL 共通）
    # --------------------------------------------------------
    if api_429_count >= MAX_API_429:
        return False

    # --------------------------------------------------------
    # 板更新遅延（BUY/SELL 共通）
    # --------------------------------------------------------
    if (
        board_update_delay_sec is not None
        and board_update_delay_sec > MAX_BOARD_DELAY_SEC
    ):
        return False

    # --------------------------------------------------------
    # 地合い悪化
    # --------------------------------------------------------
    if nikkei_velocity is not None:

        # フラッシュクラッシュ（BUY/SELL 共通）
        if nikkei_velocity <= FLASH_CRASH_DROP:
            return False

        # 通常の地合い悪化
        if side == "BUY":
            # BUY は従来どおり厳しく止める
            if nikkei_velocity <= MIN_NIKKEI_VELOCITY:
                return False

        elif side == "SELL":
            # SELL は地合い悪化では止めない
            pass

    return True