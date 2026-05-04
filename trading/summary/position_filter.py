# ============================================================
# position_filter.py
# Ver25.1-FINAL — ENTRY フィルタ最終安定版（LOG可視化強化）
# ------------------------------------------------------------
# ✔ SUMMARY / RANKING でルールを分離（思想維持）
# ✔ SUMMARY は STOP高安フィルタを緩和
# ✔ RANKING は従来どおり厳格
# ✔ 両建て・多重ENTRY完全防止
# ✔ open_positions 正統一本化
# ✔ allow_orders False の原因を必ずログ表示（重要）
# ============================================================

import logging
from datetime import datetime

from global_state import global_data
from utils_common import safe_float

logger = logging.getLogger("position_filter")


# ============================================================
# 共通ヘルパ
# ============================================================
def _get_latest_price(symbol: str):
    tick = global_data.get_latest_tick(symbol)
    if not tick:
        return None
    return safe_float(tick.get("price"))


def _get_limit_prices(symbol: str):
    tick = global_data.get_latest_tick(symbol)
    if not tick:
        return None, None
    return (
        safe_float(tick.get("limit_up")),
        safe_float(tick.get("limit_down")),
    )


def _is_stop_price_area(symbol: str, side: str) -> bool:
    """
    STOP高 / STOP安 の 2% 手前判定
    """
    price = _get_latest_price(symbol)
    limit_up, limit_down = _get_limit_prices(symbol)

    if not price:
        return False

    side = side.upper()

    if side == "BUY" and limit_up:
        return price >= limit_up * 0.98

    if side == "SELL" and limit_down:
        return price <= limit_down * 1.02

    return False


# ============================================================
# ENTRY 可否判定（★中枢）
# ============================================================
# ============================================================
def can_entry_symbol(
    symbol: str,
    side: str,
    *,
    source: str = "SUMMARY",
) -> bool:
    """
    source:
        - "SUMMARY"  : サマリー由来（STOP高安は緩和）
        - "RANKING"  : ランキング由来（STOP高安は厳格）
    """

    symbol = str(symbol)
    side = side.upper()
    source = source.upper()

    # --------------------------------------------------------
    # システム停止中（★最大の詰まりポイント）
    # --------------------------------------------------------
    if not global_data.allow_orders:
        logger.info(
            "⛔ ENTRY禁止（allow_orders=False）: %s side=%s source=%s",
            symbol, side, source,
        )
        return False

    # --------------------------------------------------------
    # クールダウン（再ENTRY禁止）
    # --------------------------------------------------------
    cooldown_until = global_data.entry_cooldown_until.get(symbol)
    if cooldown_until:
        if datetime.now() < cooldown_until:
            logger.info(
                "⛔ ENTRY禁止（クールダウン中）: %s until=%s source=%s",
                symbol, cooldown_until, source,
            )
            return False
        else:
            global_data.entry_cooldown_until.pop(symbol, None)
            logger.info(
                "✅ クールダウン解除: %s source=%s",
                symbol, source,
            )

    # --------------------------------------------------------
    # 既存ポジション（同一・反対方向とも禁止）
    # --------------------------------------------------------
    if symbol in global_data.open_positions:
        logger.info(
            "⛔ ENTRY禁止（既存ポジあり）: %s source=%s",
            symbol, source,
        )
        return False

    # --------------------------------------------------------
    # inflight（二重発注防止）
    # --------------------------------------------------------
    if symbol in global_data.entry_inflight:
        logger.info(
            "⛔ ENTRY禁止（inflight）: %s source=%s",
            symbol, source,
        )
        return False

    # --------------------------------------------------------
    # STOP高安フィルタ
    # --------------------------------------------------------
    if source == "RANKING":
        # 🔥 ランキングは厳格
        if _is_stop_price_area(symbol, side):
            logger.info(
                "⛔ RANKING ENTRY禁止（STOP高安2%%手前）: %s side=%s",
                symbol, side,
            )
            return False
    else:
        # 🔥 SUMMARY はログのみ（許可）
        if _is_stop_price_area(symbol, side):
            logger.info(
                "⚠ SUMMARY STOP高安圏内だが許可: %s side=%s",
                symbol, side,
            )

    # --------------------------------------------------------
    # 最終 OK
    # --------------------------------------------------------
    logger.info(
        "✅ ENTRY許可: %s side=%s source=%s",
        symbol, side, source,
    )
    return True
